"""Reinicia apenas o servidor ComfyUI na VM, preservando GPU e Drive."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen


ROOT = Path("/content/comfy-colab")
INSTALL = ROOT / "ComfyUI-Easy-Install"
COMFY = INSTALL / "ComfyUI"
PYTHON = INSTALL / ".venv/bin/python"
DRIVE = Path("/content/drive/MyDrive/ComfyColab")
PIDFILE = ROOT / "comfyui.pid"
LOG = ROOT / "comfyui.log"
BASE = "http://127.0.0.1:8188/"


def main() -> None:
    if not PIDFILE.is_file() or not PYTHON.is_file() or not DRIVE.is_dir():
        raise SystemExit("Servidor ou Drive indisponível. Inicie o ComfyUI primeiro.")
    with urlopen(BASE + "queue", timeout=8) as response:
        queue = json.load(response)
    if queue.get("queue_running") or queue.get("queue_pending"):
        raise SystemExit("Há uma geração em andamento ou na fila. Aguarde antes de reiniciar.")

    with urlopen(BASE + "comfy-colab/downloads", timeout=8) as response:
        downloads = json.load(response)
    if any(job.get("status") in {"running", "queued", "cancelling"} for job in downloads.get("jobs", [])):
        raise SystemExit("Há downloads em andamento. Aguarde ou cancele antes de reiniciar.")

    pid = int(PIDFILE.read_text().strip())
    command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    if b"main.py" not in command_line or b"--port 8188" not in command_line:
        raise SystemExit("O PID registrado não pertence ao servidor ComfyUI esperado.")
    print("Reiniciando o servidor ComfyUI, mantendo a VM ligada...", flush=True)
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        with socket.socket() as connection:
            connection.settimeout(0.5)
            if connection.connect_ex(("127.0.0.1", 8188)) != 0:
                break
        time.sleep(1)
    else:
        raise SystemExit("O servidor anterior não liberou a porta 8188.")

    command = [
        str(PYTHON), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--enable-manager",
        "--input-directory", str(DRIVE / "input"),
        "--output-directory", str(DRIVE / "output"),
        "--user-directory", str(DRIVE / "user"),
    ]
    with LOG.open("ab") as output:
        process = subprocess.Popen(
            command, cwd=COMFY, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
    PIDFILE.write_text(str(process.pid))
    for _ in range(150):
        if process.poll() is not None:
            raise SystemExit(f"O servidor encerrou com código {process.returncode}. Veja comfyui.log na VM.")
        try:
            with urlopen(BASE + "system_stats", timeout=2) as response:
                if response.status == 200:
                    print("ComfyUI reiniciado e pronto. O Manager também foi carregado.", flush=True)
                    return
        except OSError:
            pass
        time.sleep(1)
    raise SystemExit("ComfyUI não respondeu após o reinício. Veja comfyui.log na VM.")


if __name__ == "__main__":
    main()
