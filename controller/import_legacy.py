"""Explicit, private, read-only migration of credentials; never imports VM state."""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from controller.google import authorized, identity
from controller.state import AppError, protect


def import_legacy(store, auth, *, source_root=None, windows_root=None, all_accounts=True):
    old_windows = Path(windows_root or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ComfyColab")
    old_home = Path(source_root or f"\\\\wsl.localhost\\Ubuntu-24.04\\home\\{Path.home().name}")
    profiles_path = old_windows / "profiles.json"
    if not profiles_path.is_file():
        raise AppError("legacy_profiles_missing", "Legacy profiles were not found. Select their source directory.")
    payload = json.loads(profiles_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AppError("legacy_profiles_invalid", "Legacy profile metadata is invalid. Originals were preserved.")
    profiles = payload.get("profiles", [])
    if not isinstance(profiles, list):
        raise AppError("legacy_profiles_invalid", "Legacy profile metadata is invalid. Originals were preserved.")
    old_secrets_path = old_windows / "model-credentials.json"
    old_secrets = json.loads(old_secrets_path.read_text(encoding="utf-8")) if old_secrets_path.is_file() else {}
    if not isinstance(old_secrets, dict):
        raise AppError("legacy_credentials_invalid", "Legacy credential metadata is invalid. Originals were preserved.")
    result = {"imported": 0, "accounts": [], "errors": [], "sources_preserved": True}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        old_id = profile.get("id", "")
        if old_id != "default" and not re.fullmatch(r"[0-9a-f]{16}", str(old_id)):
            continue
        if not all_accounts and old_id != payload.get("selected", "default"):
            continue
        candidates = ([old_home / ".config/colab-cli/token.json", old_home / ".config/gcloud/application_default_credentials.json"]
                      if old_id == "default" else [old_home / ".local/share/comfy-colab/profiles" / old_id / "token.json"])
        context = who = None
        for path in candidates:
            if not path.is_file():
                continue
            try:
                context = authorized(json.loads(path.read_text(encoding="utf-8")))
                who = identity(context)
                if profile.get("email") and who["email"].casefold() != profile["email"].casefold():
                    raise AppError("account_mismatch", "Imported login belongs to another account.")
                break
            except Exception:
                context = who = None
        if not context or not who:
            result["errors"].append({"legacy_profile_id": old_id, "code": "legacy_login_unavailable", "message": "Login unavailable, revoked, or account mismatch. Reconnect; original source is intact."})
            continue
        with store.lock:
            existing = store.db.execute("SELECT id FROM accounts WHERE subject=?", (who["sub"],)).fetchone()
        account_id = existing[0] if existing else store.add(profile.get("label", "Google account"))
        try:
            auth.save(account_id, context, expected_email=profile.get("email"))
            for provider in ("Hugging Face", "Civitai"):
                encoded = old_secrets.get(f"{old_id}:{provider}")
                if encoded and not store.secret(account_id, provider):
                    token = protect(base64.b64decode(encoded, validate=True), decrypt=True).decode()
                    store.secret(account_id, provider, token)
            drive_token = old_home / ".local/share/comfy-colab/drive" / old_id / "token.json"
            if drive_token.is_file():
                try:
                    auth.save(account_id, authorized(json.loads(drive_token.read_text(encoding="utf-8"))), purpose="drive", expected_email=who["email"])
                except Exception:
                    result["errors"].append({"legacy_profile_id": old_id, "code": "legacy_drive_unavailable", "message": "Colab imported; Drive requires new authorization."})
            drive_client = old_home / ".config/comfy-colab/drive-oauth-client.json"
            if drive_client.is_file() and not store.secret(account_id, "google_drive_client"):
                try:
                    config = json.loads(drive_client.read_text(encoding="utf-8"))
                    installed = config.get("installed", config.get("web", {}))
                    if not all(isinstance(installed.get(k), str) and installed[k] for k in ("client_id", "client_secret")) or not isinstance(installed.get("redirect_uris"), list):
                        raise ValueError()
                    store.secret(account_id, "google_drive_client", config)
                except Exception:
                    result["errors"].append({"legacy_profile_id": old_id, "code": "legacy_drive_client_unavailable", "message": "Colab imported; configure the app OAuth client to authorize Drive."})
            # Client binding is already in each authorized-user credential. No old client config is published.
            store.update(account_id, gpu=profile.get("gpu") if profile.get("gpu") in {"CPU", "T4", "L4", "A100", "G4"} else "T4", output_mode=profile.get("output_mode") if profile.get("output_mode") in {"pc", "drive"} else "drive")
            store.set("legacy_import_" + old_id, account_id)
            result["imported"] += 1
            result["accounts"].append({"id": account_id, "label": store.account(account_id)["label"], "email": who["email"]})
        except Exception:
            result["errors"].append({"legacy_profile_id": old_id, "code": "legacy_credential_unavailable", "message": "Some credentials could not be imported. Original files are intact."})
    return result
