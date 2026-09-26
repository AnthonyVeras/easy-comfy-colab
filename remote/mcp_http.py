"""Prepara o Comfy MCP oficial dentro da VM e o expõe só no loopback."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path('/content/comfy-colab')
COMFY = ROOT / 'ComfyUI-Easy-Install/ComfyUI'
VENV = ROOT / 'mcp-venv'
PYTHON = VENV / 'bin/python'
COMFY_BIN = VENV / 'bin/comfy'
PIDFILE = ROOT / 'comfy-mcp.pid'
LOG = ROOT / 'comfy-mcp.log'
HOST = '127.0.0.1'
PORT = 8189


def listening() -> bool:
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex((HOST, PORT)) == 0


def main() -> None:
    if not (COMFY / 'main.py').is_file():
        raise SystemExit('ComfyUI da VM não está instalado.')
    if listening():
        print('Comfy MCP já responde na VM.')
        return
    if not PYTHON.is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(VENV)], check=True)
    ready = subprocess.run(
        [str(PYTHON), '-c', 'from importlib.metadata import version; '
         'assert version("comfy-mcp")=="0.10.0"; assert version("comfy-cli")=="1.21.0"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
    ).returncode == 0
    if not ready:
        subprocess.run(
            [str(PYTHON), '-m', 'pip', '--disable-pip-version-check', 'install',
             'comfy-mcp==0.10.0', 'comfy-cli==1.21.0'],
            check=True, stdout=subprocess.DEVNULL, timeout=600,
        )
    subprocess.run([str(COMFY_BIN), 'set-default', str(COMFY)], check=True)
    environment = os.environ.copy()
    environment['COMFY_BIN'] = str(COMFY_BIN)
    environment.pop('COMFYUI_URL', None)
    environment.pop('COMFYUI_HOST', None)
    code = (
        'from comfy_mcp.server import mcp; '
        "mcp.run(transport='streamable-http', host='127.0.0.1', port=8189)"
    )
    with LOG.open('ab') as output:
        process = subprocess.Popen(
            [str(PYTHON), '-c', code], cwd=COMFY, env=environment,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PIDFILE.write_text(str(process.pid))
    for _ in range(60):
        if process.poll() is not None:
            raise SystemExit(f'Comfy MCP encerrou com código {process.returncode}; veja comfy-mcp.log.')
        if listening():
            print('Comfy MCP pronto em 127.0.0.1:8189.')
            return
        time.sleep(1)
    raise SystemExit('Comfy MCP não abriu a porta 8189; veja comfy-mcp.log.')


if __name__ == '__main__':
    main()
