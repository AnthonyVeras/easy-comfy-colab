"""Modo CPU: downloads sem instalar ComfyUI/CUDA."""

import importlib.util
from pathlib import Path
from aiohttp import web

spec = importlib.util.spec_from_file_location(
    "download_service", Path(__file__).with_name("service.py")
)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
routes = web.RouteTableDef()
service.MODEL_ROOT.mkdir(parents=True, exist_ok=True)
service.register(routes, mode="downloads", queue_status=lambda: False)


@routes.get("/system_stats")
async def health(request):
    return web.json_response(
        {"mode": "downloads", "system": {"device": "cpu"}, "devices": []}
    )


@routes.get("/queue")
async def queue(request):
    return web.json_response({"queue_running": [], "queue_pending": []})


app = web.Application(client_max_size=16384)
app.add_routes(routes)
web.run_app(app, host="127.0.0.1", port=8188, print=None)
