"""Downloads de modelos na VM e credenciais protegidas pelo usuário do Windows."""

from __future__ import annotations
from i18n import tr

import base64
import ctypes
import json
import re
from ctypes import wintypes
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from backend import COMFY_URL, app_data_dir


CATEGORIES = (
    "checkpoints",
    "diffusion_models",
    "loras",
    "vae",
    "text_encoders",
    "clip",
    "clip_vision",
    "controlnet",
    "upscale_models",
    "embeddings",
    "unet",
    "LLM",
    "llm_gguf",
)
EXTENSIONS = {".safetensors", ".sft", ".ckpt", ".pth", ".pt", ".gguf", ".bin", ".onnx"}
PROVIDERS = {
    "huggingface.co": "Hugging Face",
    "civitai.com": "Civitai",
    "civitai.red": "Civitai",
}


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _protect(data: bytes, decrypt: bool = False) -> bytes:
    if not hasattr(ctypes, "windll"):
        raise OSError(tr("O armazenamento seguro exige Windows."))
    input_buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _Blob()
    crypt32 = ctypes.windll.crypt32
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.restype = wintypes.BOOL
    if decrypt:
        okay = operation(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
        )
    else:
        okay = operation(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
        )
    if not okay:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


class CredentialStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "model-credentials.json"

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, profile_id: str, provider: str) -> str:
        encoded = self._read().get(f"{profile_id}:{provider}")
        if not isinstance(encoded, str):
            return ""
        try:
            return _protect(
                base64.b64decode(encoded, validate=True), decrypt=True
            ).decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            return ""

    def set(self, profile_id: str, provider: str, token: str) -> None:
        data = self._read()
        key = f"{profile_id}:{provider}"
        if token:
            data[key] = base64.b64encode(_protect(token.encode("utf-8"))).decode(
                "ascii"
            )
        else:
            data.pop(key, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(self.path)


def prepare_download(url: str, name: str, category: str) -> tuple[str, str, str, str]:
    url = url.strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or host not in PROVIDERS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError(
            tr("Use um link HTTPS de arquivo do Hugging Face ou da API do Civitai.")
        )
    if re.search(r"(?:^|&)(?:token|api_key|access_token)=", parts.query, re.IGNORECASE):
        raise ValueError(tr("Coloque a credencial no campo próprio, fora da URL."))
    if host == "huggingface.co":
        path = parts.path.replace("/blob/", "/resolve/", 1)
        if "/resolve/" not in path:
            raise ValueError(
                tr("No Hugging Face, copie o link de um arquivo específico do repositório.")
            )
        url = urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
    elif not re.fullmatch(r"/api/download/models/\d+", parts.path.rstrip("/")):
        raise ValueError(
            tr("No Civitai, use o link de download da versão: /api/download/models/ID.")
        )
    if category not in CATEGORIES:
        raise ValueError(tr("Escolha uma pasta de modelos válida."))
    name = name.strip()
    if not name and host == "huggingface.co":
        from urllib.parse import unquote

        name = unquote(parts.path.rsplit("/", 1)[-1])
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError(tr("Informe o nome do arquivo, incluindo a extensão."))
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise ValueError(
            tr("Formato não aceito. Use um arquivo de modelo, como .safetensors ou .gguf.")
        )
    return url, name, category, PROVIDERS[host]


def _json_request(path: str, payload: dict | None = None) -> dict:
    request = Request(
        COMFY_URL + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as exc:
        try:
            error = json.load(exc).get("error", "")
        except (ValueError, OSError):
            error = ""
        raise RuntimeError(error or tr("Erro HTTP {p0} no ComfyUI.", p0=exc.code)) from None
    except URLError as exc:
        raise RuntimeError(
            tr("Não foi possível acessar o ComfyUI: {p0}", p0=exc.reason)
        ) from None


def start_download(url: str, name: str, category: str, token: str) -> str:
    result = _json_request(
        "comfy-colab/model-download",
        {
            "url": url,
            "name": name,
            "directory": category,
            "token": token,
        },
    )
    return str(result["job_id"])


def download_capable() -> bool:
    try:
        return (
            _json_request("comfy-colab/model-download/capabilities").get("bearer_token")
            is True
        )
    except (RuntimeError, ValueError, OSError):
        return False


def download_status(job_id: str) -> dict:
    return _json_request("comfy-colab/model-download/" + job_id)
