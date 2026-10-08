"""Validated paths, identity and durable publication shared by remote services."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.request import Request, urlopen

CATEGORIES = frozenset({"checkpoints", "diffusion_models", "text_encoders", "clip", "clip_vision", "loras", "vae", "controlnet", "upscale_models", "embeddings", "unet", "llm_gguf", "onnx", "audio_encoders", "model_patches"})
SUFFIXES = frozenset({".safetensors", ".sft", ".ckpt", ".pth", ".pt", ".gguf", ".bin", ".onnx"})


class Fault(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/content/easy-comfy-colab-2")
    drive: Path = Path("/content/drive/MyDrive/EasyComfyColab2")

    @property
    def comfy(self): return self.root / "ComfyUI"
    @property
    def state(self): return self.root / "state"
    @property
    def models(self): return self.drive / "models"
    @property
    def cache(self): return self.root / "cache/models"
    @property
    def artifacts(self): return self.drive / "runtime-artifacts"


def read_json(path: Path, default=None):
    if not path.exists(): return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError): raise Fault("invalid_state", "Saved runtime state is invalid; preserve the VM and inspect recovery.") from None


def atomic_bytes(path: Path, data: bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def digest(path: Path):
    with path.open("rb") as stream: return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value: str):
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise Fault("invalid_path", "Choose a relative path inside the selected library.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(p in {"", "."} for p in value.split("/")):
        raise Fault("invalid_path", "Choose a relative path inside the selected library.")
    return path


def inside(root: Path, value: str):
    path = root / relative_path(value)
    if not path.resolve().is_relative_to(root.resolve()):
        raise Fault("invalid_path", "Symbolic links cannot leave the selected library.")
    return path


def model_path(root: Path, category: str, filename: str):
    if not isinstance(category, str) or not isinstance(filename, str) or category not in CATEGORIES or Path(filename).suffix.lower() not in SUFFIXES:
        raise Fault("invalid_model", "Select a supported model category and file format.")
    if len(relative_path(filename).parts) != 1:
        raise Fault("invalid_model", "A model filename cannot contain directories.")
    return inside(root, category + "/" + filename)


def validate_identity(data):
    for name in ("account_id", "session_id", "nonce"):
        if not isinstance(data.get(name), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data[name]):
            raise Fault("invalid_identity", "Account, allocation and session nonce are required.")
    token = data.get("bearer_token", "")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise Fault("invalid_identity", "A strong per-session bearer token is required.")
    if data.get("output_mode", "drive") not in {"pc", "drive"}:
        raise Fault("invalid_mode", "Select PC or Drive outputs.")


def public_identity(config):
    return {key: config[key] for key in ("account_id", "session_id", "nonce")}


def authorize(config, headers):
    if not hmac.compare_digest(headers.get("Authorization", ""), "Bearer " + config["bearer_token"]):
        raise Fault("unauthorized", "Connect through the owned session controller.")
    for key, header in (("account_id", "X-Easy-Account"), ("session_id", "X-Easy-Session"), ("nonce", "X-Easy-Nonce")):
        if not hmac.compare_digest(headers.get(header, ""), config[key]):
            raise Fault("identity_mismatch", "This endpoint belongs to a different account or session.")


def comfy_json(path, value=None, timeout=8, headers=None):
    request = Request("http://127.0.0.1:8188" + path,
                      data=None if value is None else json.dumps(value).encode(),
                      headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
            try: return json.loads(raw)
            except ValueError: return raw.decode("utf-8", errors="replace")
    except Exception: raise Fault("comfy_unavailable", "ComfyUI did not respond; preserve the session and reconnect.") from None


def queue_idle():
    queue = comfy_json("/queue")
    if not isinstance(queue, dict) or not {"queue_running", "queue_pending"} <= queue.keys():
        raise Fault("queue_unknown", "The generation queue could not be verified.")
    return not queue["queue_running"] and not queue["queue_pending"]
