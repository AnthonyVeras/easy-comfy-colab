"""Output destination shared by initial launch and server restart."""

import json
import os
from pathlib import Path
import signal
import sys
import uuid
from urllib.request import urlopen

ROOT = Path('/content/comfy-colab')
DRIVE = Path('/content/drive/MyDrive/ComfyColab/output')


def prepare(mode=None):
    marker = ROOT / 'output-mode'
    if mode is None:
        mode = marker.read_text().strip() if marker.exists() else 'drive'
    if mode not in {'pc', 'drive'}:
        raise ValueError('Destino de outputs inválido.')
    ROOT.mkdir(parents=True, exist_ok=True)
    if mode == 'pc':
        identity = ROOT / 'output-session'
        if not identity.exists():
            identity.write_text(uuid.uuid4().hex)
        session = identity.read_text().strip()
        if len(session) != 32 or any(c not in '0123456789abcdef' for c in session):
            raise ValueError('Identificador de outputs inválido.')
        directory = ROOT / 'output-pc' / session
    else:
        directory = DRIVE
    directory.mkdir(parents=True, exist_ok=True)
    marker.write_text(mode)
    return directory


def pause():
    """Pause the idle server while the final PC copy and VM stop run."""
    with urlopen('http://127.0.0.1:8188/queue', timeout=8) as response:
        queue = json.load(response)
    if not {'queue_running', 'queue_pending'} <= set(queue) or any(
        queue.get(key) for key in ('queue_running', 'queue_pending')
    ):
        raise RuntimeError('Aguarde a fila terminar antes de salvar no PC e encerrar.')
    pid = int((ROOT / 'comfyui.pid').read_text())
    cmd = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    if b'main.py' not in cmd or b'--port' not in cmd or b'8188' not in cmd:
        raise RuntimeError('Não foi possível confirmar o processo ComfyUI.')
    os.kill(pid, signal.SIGSTOP)
    print(pid)


if __name__ == '__main__':
    if sys.argv[1:] == ['pause']:
        pause()
    else:
        print(prepare(sys.argv[1] if len(sys.argv) > 1 else None))
