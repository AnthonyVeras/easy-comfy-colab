"""Installed as our own ComfyUI node: serialize admission against safe shutdown."""
import fcntl
import json
from pathlib import Path
from aiohttp import web
from server import PromptServer
from .gate import controlled, protected

STATE = Path("/content/easy-comfy-colab-2/state")
STATE.mkdir(parents=True, exist_ok=True)
CONFIG = json.loads((STATE / "session.json").read_text())


@web.middleware
async def admission(request, handler):
    if protected(request.method, request.path) and not controlled(request.path, request.headers, CONFIG):
        # Nonblocking admission avoids stalling the event loop during a stop gate.
        with (STATE / "admission.lock").open("a") as lock:
            try: fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError: raise web.HTTPConflict(text="Session is preparing to stop.")
            if (STATE / "stopping").exists() or (STATE / "training-active").exists(): raise web.HTTPConflict(text="Session is preparing to stop.")
            return await handler(request)
    return await handler(request)


PromptServer.instance.app.middlewares.append(admission)
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
