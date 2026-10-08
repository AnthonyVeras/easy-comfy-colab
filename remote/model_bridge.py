"""ComfyUI's browser hook queues models through the owned VM companion."""
import asyncio
import json
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

FILES = {"__init__.py": "model_node.py", "model_bridge.py": "model_bridge.py", "web/remote_download.js": "web/remote_download.js"}
LIMIT = 16 * 1024


def install_model_bridge(paths):
    destination = paths.comfy / "custom_nodes/easy_model_download"
    source = Path(__file__).parent
    if destination.is_symlink() or not destination.resolve().is_relative_to(paths.comfy.resolve()): raise ValueError("Model bridge destination leaves the owned ComfyUI.")
    for target, name in FILES.items():
        file = destination / target
        if file.is_symlink() or not file.resolve().is_relative_to(destination.resolve()): raise ValueError("Model bridge destination contains an external link.")
        file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, file)


def validate_model_bridge(root):
    try:
        from .common import Fault
    except ImportError:
        from common import Fault
    destination = Path(root) / "ComfyUI/custom_nodes/easy_model_download"
    if destination.is_symlink() or not destination.resolve().is_relative_to((Path(root)/"ComfyUI").resolve()):
        raise Fault("runtime_incomplete", "The VM model-download bridge leaves the owned ComfyUI.")
    for target, name in FILES.items():
        file = destination / target
        if not file.is_file() or file.is_symlink() or file.read_bytes() != (Path(__file__).parent / name).read_bytes():
            raise Fault("runtime_incomplete", "The VM model-download bridge is missing or changed; reinstall before use.")


def same_origin(headers, require_origin=False):
    try:
        host = urlsplit("http://" + headers.get("Host", ""))
        if host.hostname not in {"localhost", "127.0.0.1", "::1"} or host.username or host.password or host.path or host.query or host.fragment:
            return False
        host.port  # Reject malformed ports.
        if headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}: return False
        origin = headers.get("Origin")
        if not origin: return not require_origin
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == host.netloc and not (parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password)
    except (ValueError, TypeError):
        return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def relay(config, route, payload=None):
    # Only these two loopback routes are reachable; the browser never chooses a destination.
    headers = {"Authorization": "Bearer " + config["bearer_token"], "X-Easy-Account": config["account_id"], "X-Easy-Session": config["session_id"], "X-Easy-Nonce": config["nonce"], "Content-Type": "application/json"}
    request = Request("http://127.0.0.1:8190" + route, headers=headers, data=None if payload is None else json.dumps(payload).encode())
    try:
        try: response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=12)
        except HTTPError as error: response = error
        with response:
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT: raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("ok"), bool): raise ValueError()
            return response.status, data
    except Exception:
        return 503, {"ok": False, "error": {"code": "comfy_unavailable", "message": "Reconnect the owned session in Easy Comfy Colab 2."}}


def register(routes, config):
    from aiohttp import web

    def failure(code, status=400):
        return web.json_response({"ok": False, "error": {"code": code, "message": "The VM download could not start. Check the Easy Comfy Colab 2 session."}}, status=status)

    @routes.post("/easy-comfy/model-download")
    async def enqueue(request):
        if not same_origin(request.headers, require_origin=True): return failure("unauthorized", 403)
        if request.content_type != "application/json": return failure("invalid_download", 415)
        try:
            raw = bytearray()
            async for chunk in request.content.iter_chunked(LIMIT + 1):
                raw.extend(chunk)
                if len(raw) > LIMIT: return failure("invalid_download", 413)
            payload = json.loads(raw)
            if not isinstance(payload, dict): return failure("invalid_download")
        except (ValueError, UnicodeError): return failure("invalid_download")
        status, reply = await asyncio.to_thread(relay, config, "/comfy/downloads", payload)
        return web.json_response(reply, status=status)

    @routes.get("/easy-comfy/model-download/{job_id}")
    async def progress(request):
        if not same_origin(request.headers): return failure("unauthorized", 403)
        job_id = request.match_info["job_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", job_id): return failure("download_missing", 404)
        status, reply = await asyncio.to_thread(relay, config, "/comfy/downloads/" + job_id)
        return web.json_response(reply, status=status)
