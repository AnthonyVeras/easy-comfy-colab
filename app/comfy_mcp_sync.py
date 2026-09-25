"""Sincroniza workflows criados pelo Codex com o ComfyUI da VM ativa."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from backend import Gateway, ProfileStore
from runtime_config import wsl_prefix


REMOTE_WORKFLOWS = "/content/drive/MyDrive/ComfyColab/user/default/workflows"


def linux_path(path: Path) -> str:
    result = subprocess.run(
        [*wsl_prefix(), "wslpath", "-a", str(path.resolve())],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def run_cli(*arguments: str) -> None:
    store = ProfileStore()
    profile = store.current()
    gateway = Gateway()
    command = gateway.cli_command(profile, *arguments)
    result = gateway.run(profile, *command, timeout=120)
    if result.returncode:
        raise SystemExit(
            result.stderr.strip() or result.stdout.strip() or "Colab CLI falhou."
        )
    if result.stdout.strip():
        print(result.stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    upload = commands.add_parser(
        "upload", help="Envia um workflow JSON local ao Drive da VM"
    )
    upload.add_argument("file", type=Path)
    download = commands.add_parser("download", help="Copia um workflow do Drive da VM")
    download.add_argument("name")
    args = parser.parse_args()

    if args.action == "upload":
        source = args.file.resolve()
        if not source.is_file() or source.suffix.lower() != ".json":
            raise SystemExit("Informe um workflow .json existente.")
        remote = f"{REMOTE_WORKFLOWS}/{source.name}"
        run_cli("upload", "-s", "comfy-colab", linux_path(source), remote)
        print("Caminho para o MCP:", remote)
    else:
        name = Path(args.name).name
        if name != args.name or not name.lower().endswith(".json"):
            raise SystemExit("Informe somente o nome do workflow .json.")
        profile = ProfileStore().current()
        target = profile.local_data / "user/default/workflows" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        run_cli(
            "download",
            "-s",
            "comfy-colab",
            f"{REMOTE_WORKFLOWS}/{name}",
            linux_path(target),
        )
        print("Workflow local:", target)


if __name__ == "__main__":
    main()
