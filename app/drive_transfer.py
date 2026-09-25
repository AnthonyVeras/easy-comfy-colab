"""Cópia entre Drives executada no WSL; somente metadados passam pelo computador."""

from __future__ import annotations
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
FIELDS = "id,name,mimeType,size,md5Checksum,modifiedTime,appProperties,capabilities(canCopy),shortcutDetails"


def emit(text):
    print(text, flush=True)


def token_path(profile):
    if profile != "default" and not re.fullmatch(r"[0-9a-f]{16}", profile):
        raise ValueError("Perfil inválido.")
    base = Path.home() / ".local/share/easy-comfy-colab/drive" / profile
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base / "token.json"


def connect(profile):
    from colab_cli import auth
    from google.auth.transport.requests import AuthorizedSession

    auth.TOKEN_CONFIG_PATH = str(token_path(profile))
    auth.PUBLIC_SCOPES = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = auth._get_google_auth_credentials(
        str(Path.home() / ".colab-cli-oauth-config.json")
    )
    os.chmod(auth.TOKEN_CONFIG_PATH, 0o600)
    return Drive(AuthorizedSession(creds))


class Drive:
    def __init__(self, session):
        self.session = session

    def request(self, method, path, **kwargs):
        response = self.session.request(
            method, "https://www.googleapis.com/drive/v3/" + path, timeout=90, **kwargs
        )
        if not response.ok:
            reason = ""
            try:
                reason = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise RuntimeError(
                f"Drive HTTP {response.status_code}: {reason[:240]}. Corrija o acesso/espaço e tente retomar."
            )
        return response.json() if response.content else {}

    def children(self, folder):
        token = None
        while True:
            data = self.request(
                "GET",
                "files",
                params={
                    "q": f"'{folder}' in parents and trashed = false",
                    "fields": f"nextPageToken,files({FIELDS})",
                    "pageSize": 1000,
                    **({"pageToken": token} if token else {}),
                },
            )
            yield from data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                break

    def about(self):
        return self.request(
            "GET", "about", params={"fields": "user(emailAddress),storageQuota"}
        )

    def folder(self, name, parent="root", create=False):
        matches = [
            f
            for f in self.children(parent)
            if f["name"] == name and f["mimeType"] == FOLDER
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Existem várias pastas {name}; use o ID da pasta de origem."
            )
        if matches:
            return matches[0]
        if not create:
            raise ValueError(f"Pasta {name} não encontrada nesta conta.")
        return self.request(
            "POST",
            "files",
            json={"name": name, "mimeType": FOLDER, "parents": [parent]},
            params={"fields": FIELDS},
        )

    def get(self, id):
        return self.request("GET", "files/" + id, params={"fields": FIELDS})


def enumerate_tree(drive, root):
    items = []

    def walk(id, parent_path):
        for f in drive.children(id):
            if f["name"] == ".cache":
                continue
            f = dict(f, path=parent_path + [f["name"]], parent_source=id)
            items.append(f)
            if f["mimeType"] == FOLDER:
                walk(f["id"], f["path"])

    walk(root, [])
    return items


def share(source, folder, email, role="reader"):
    existing = source.request(
        "GET",
        f"files/{folder}/permissions",
        params={"fields": "permissions(id,type,emailAddress,role)"},
    )
    match = next(
        (
            p
            for p in existing.get("permissions", [])
            if p.get("emailAddress", "").lower() == email.lower()
        ),
        None,
    )
    if match:
        if (
            match["role"] in {"owner", "organizer", "fileOrganizer", "writer"}
            or role == "reader"
        ):
            return
        source.request(
            "PATCH", f"files/{folder}/permissions/{match['id']}", json={"role": role}
        )
        return
    source.request(
        "POST",
        f"files/{folder}/permissions",
        params={"sendNotificationEmail": "false"},
        json={"type": "user", "role": role, "emailAddress": email},
    )


def verified(source, target):
    if source["mimeType"] != target.get("mimeType"):
        return False
    if source.get("size") != target.get("size"):
        return False
    if source.get("md5Checksum"):
        return source["md5Checksum"] == target.get("md5Checksum")
    # Sem checksum, só aceita uma cópia marcada desta mesma versão da origem.
    version = hashlib.sha256(
        (source["id"] + "|" + source.get("modifiedTime", "")).encode()
    ).hexdigest()[:32]
    return (
        target.get("appProperties", {}).get("comfySource") == source["id"]
        and target.get("appProperties", {}).get("comfyVersion") == version
    )


def copy_tree(source, dest, root, items, destination_root, email):
    share(source, root["id"], email)
    folders = {root["id"]: destination_root["id"]}
    completed = 0
    cache = {}
    for index, item in enumerate(items, 1):
        parent = folders[item["parent_source"]]
        # Uma marca acompanha o arquivo no próprio Drive: retomar funciona mesmo em outro PC.
        version = hashlib.sha256(
            (item["id"] + "|" + item.get("modifiedTime", "")).encode()
        ).hexdigest()[:32]
        if parent not in cache:
            cache[parent] = list(dest.children(parent))
        existing = cache[parent]
        matched = next(
            (
                f
                for f in existing
                if f.get("appProperties", {}).get("comfySource") == item["id"]
                and (
                    item["mimeType"] == FOLDER
                    or f.get("appProperties", {}).get("comfyVersion") == version
                )
            ),
            None,
        )
        if item["mimeType"] == FOLDER:
            matched = matched or next(
                (
                    f
                    for f in existing
                    if f["mimeType"] == FOLDER and f["name"] == item["name"]
                ),
                None,
            )
            if not matched:
                matched = dest.request(
                    "POST",
                    "files",
                    json={
                        "name": item["name"],
                        "mimeType": FOLDER,
                        "parents": [parent],
                        "appProperties": {"comfySource": item["id"]},
                    },
                    params={"fields": FIELDS},
                )
                existing.append(matched)
            folders[item["id"]] = matched["id"]
        elif item["mimeType"] == SHORTCUT:
            raise ValueError(
                "Há um atalho na origem: "
                + "/".join(item["path"])
                + ". Copie a pasta original pelo ID para incluir seu conteúdo."
            )
        else:
            if matched and not verified(item, matched):
                raise ValueError(
                    "A cópia anterior não passou na verificação: "
                    + item["name"]
                    + ". Revise-a no Drive antes de retomar."
                )
            if not matched:
                same = next(
                    (
                        f
                        for f in existing
                        if f["name"] == item["name"] and verified(item, f)
                    ),
                    None,
                )
                if same:
                    matched = same
                else:
                    name = item["name"]
                    if any(f["name"] == name for f in existing):
                        p = Path(name)
                        name = p.stem + " (cópia " + version[:6] + ")" + p.suffix
                    matched = dest.request(
                        "POST",
                        f"files/{item['id']}/copy",
                        json={
                            "name": name,
                            "parents": [parent],
                            "appProperties": {
                                "comfySource": item["id"],
                                "comfyVersion": version,
                            },
                        },
                        params={"fields": FIELDS},
                    )
                    existing.append(matched)
                if not verified(item, matched):
                    raise ValueError("Verificação da cópia falhou: " + item["name"])
            completed += int(item.get("size", 0))
        emit(f"[{index}/{len(items)}] Verificado: {'/'.join(item['path'])}")
    emit(
        f"Cópia concluída e verificada: {completed / 2**30:.2f} GiB. Originais preservados."
    )


def pending_bytes(dest, root, items):
    """Desconta cópias já existentes para permitir retomada com pouca cota livre."""
    parents = {items[0]["parent_source"]: root["id"]} if items else {}
    # O primeiro item sempre é filho direto da raiz na enumeração em profundidade.
    cache = {}
    needed = 0
    for item in items:
        parent = parents.get(item["parent_source"])
        if parent and parent not in cache:
            cache[parent] = list(dest.children(parent))
        existing = cache.get(parent, [])
        if item["mimeType"] == FOLDER:
            match = next(
                (
                    f
                    for f in existing
                    if f["name"] == item["name"] and f["mimeType"] == FOLDER
                ),
                None,
            )
            if match:
                parents[item["id"]] = match["id"]
        elif not any(f["name"] == item["name"] and verified(item, f) for f in existing):
            version = hashlib.sha256(
                (item["id"] + "|" + item.get("modifiedTime", "")).encode()
            ).hexdigest()[:32]
            if not any(
                f.get("appProperties", {}).get("comfySource") == item["id"]
                and f.get("appProperties", {}).get("comfyVersion") == version
                and verified(item, f)
                for f in existing
            ):
                needed += int(item.get("size", 0))
    return needed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["authorize", "plan", "copy", "share"])
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination")
    parser.add_argument("--folder", default="ComfyColab")
    parser.add_argument("--source-email", default="")
    parser.add_argument("--destination-email", default="")
    args = parser.parse_args()
    emit("Conectando Drive de origem. Confira a conta escolhida no navegador.")
    source = connect(args.source)
    source_info = source.about()
    email = source_info["user"]["emailAddress"]
    if args.source_email and email.lower() != args.source_email.lower():
        token_path(args.source).unlink(missing_ok=True)
        raise ValueError(
            "A autorização foi feita com outra conta. Tente novamente selecionando "
            + args.source_email
        )
    emit("Drive autorizado: " + email)
    if args.action == "authorize":
        return
    if not args.destination or args.destination == args.source:
        raise ValueError("Escolha outra conta de destino.")
    dest = connect(args.destination)
    about = dest.about()
    dest_email = about["user"]["emailAddress"]
    if args.destination_email and dest_email.lower() != args.destination_email.lower():
        token_path(args.destination).unlink(missing_ok=True)
        raise ValueError(
            "Drive de destino pertence a outra conta. Autorize "
            + args.destination_email
        )
    if dest_email.lower() == email.lower():
        raise ValueError("Origem e destino são a mesma conta Google.")
    root = (
        source.folder("ComfyColab")
        if args.folder == "ComfyColab"
        else source.get(args.folder)
    )
    if root["mimeType"] != FOLDER:
        raise ValueError("Selecione uma pasta de origem.")
    items = enumerate_tree(source, root["id"])
    total = sum(int(f.get("size", 0)) for f in items if f["mimeType"] != FOLDER)
    quota = about.get("storageQuota", {})
    free = (
        int(quota["limit"]) - int(quota.get("usage", 0)) if quota.get("limit") else None
    )
    emit(
        f"Origem: {email}\nDestino: {dest_email}\nPasta: {root['name']}\nItens: {len(items)}\nTamanho: {total / 2**30:.2f} GiB"
    )
    emit(
        f"Espaço livre no destino: {free / 2**30:.2f} GiB"
        if free is not None
        else "Cota de armazenamento não informada pelo Google."
    )
    if args.action == "plan":
        return
    if args.action == "copy":
        if any(f["mimeType"] == SHORTCUT for f in items):
            raise ValueError(
                "A origem contém atalhos. Use o ID da pasta original para copiar seu conteúdo."
            )
        target = dest.folder("ComfyColab", create=True)
        pending = pending_bytes(dest, target, items)
        if free is not None and free < pending:
            raise ValueError(
                "Espaço insuficiente para os arquivos ainda não copiados. Libere espaço no destino."
            )
        copy_tree(source, dest, root, items, target, dest_email)
    else:
        models = source.folder("models", root["id"])
        target = dest.folder("ComfyColab", create=True)
        matches = [f for f in dest.children(target["id"]) if f["name"] == "models"]
        if matches:
            if any(
                f.get("shortcutDetails", {}).get("targetId") == models["id"]
                for f in matches
            ):
                emit("Biblioteca compartilhada já configurada.")
                return
            raise ValueError(
                "O destino já tem uma pasta models. Use a cópia independente ou organize essa pasta antes de criar o atalho."
            )
        share(source, models["id"], dest_email, "writer")
        dest.request(
            "POST",
            "files",
            json={
                "name": "models",
                "mimeType": SHORTCUT,
                "parents": [target["id"]],
                "shortcutDetails": {"targetId": models["id"]},
            },
        )
        emit(
            "Biblioteca compartilhada criada. Ambas as contas podem alterar os modelos. Valide a montagem na próxima sessão do destino."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("Não foi possível concluir: " + str(exc).split("https://", 1)[0][:500])
        sys.exit(1)
