"""Configuração desta máquina, mantida fora do repositório."""

import json
import os
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "EasyComfyColab" if base else Path.home() / ".easy-comfy-colab"


def wsl_prefix() -> list[str]:
    try:
        config = json.loads((app_data_dir() / "runtime.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    distro = os.environ.get("EASY_COMFY_WSL_DISTRO") or config.get("distro") or "Ubuntu-24.04"
    user = os.environ.get("EASY_COMFY_WSL_USER") or config.get("user") or ""
    if not isinstance(distro, str) or not isinstance(user, str):
        raise ValueError("Distribuição e usuário WSL devem ser texto.")
    result = ["wsl.exe", "--distribution", distro]
    if user:
        result += ["--user", user]
    return result + ["--exec"]
