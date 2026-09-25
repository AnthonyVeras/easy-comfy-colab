"""Downloads e telemetria compartilhados pelo ComfyUI e modo CPU."""

from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import requests
from aiohttp import web

MODEL_ROOT = Path(
    os.environ.get("COMFY_MODEL_ROOT", "/content/drive/MyDrive/ComfyColab/models")
)
STATE_PATH = MODEL_ROOT.parent / "user" / "comfy-colab-downloads.json"
CATEGORIES = {
    "checkpoints",
    "diffusion_models",
    "loras",
    "vae",
    "text_encoders",
    "clip",
    "clip_vision",
    "controlnet",
    "upscale_models",
    "embeddings",
    "unet",
    "LLM",
    "llm_gguf",
}
SUFFIXES = {".safetensors", ".sft", ".ckpt", ".pth", ".pt", ".gguf", ".bin", ".onnx"}
JOBS = {}
ACTIVE_TARGETS = {}
LIMIT = 3
RUNNING = 0
MODE = "comfy"
INVALIDATE = lambda: None
QUEUE = lambda: None


def restore():
    try:
        for job in json.loads(STATE_PATH.read_text()).values():
            if job.get("status") in ("running", "queued", "cancelling"):
                job["status"] = "interrupted"
            JOBS[job["id"]] = job
    except (OSError, ValueError, TypeError, KeyError):
        pass


def persist():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(JOBS, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def destination(data):
    url, name, category = (data.get(k) for k in ("url", "name", "directory"))
    if not all(isinstance(x, str) for x in (url, name, category)):
        raise ValueError("Informe URL, nome e categoria.")
    parts = urlsplit(url)
    allowed = parts.hostname in {"huggingface.co", "civitai.com", "civitai.red"} or (
        parts.hostname == "github.com"
        and parts.path
        == "/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
    )
    if (
        not allowed
        or parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("Use HTTPS do Hugging Face ou Civitai.")
    if len(url) > 4096 or re.search(
        r"(?:^|&)(token|api_key|access_token)=", parts.query, re.I
    ):
        raise ValueError("Use o campo de credenciais, sem token na URL.")
    if category not in CATEGORIES:
        raise ValueError("Categoria inválida.")
    relative = PurePosixPath(name)
    if (
        len(name) > 512
        or "\\" in name
        or any(ord(c) < 32 for c in name)
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() not in SUFFIXES
    ):
        raise ValueError("Nome de modelo inválido.")
    target = MODEL_ROOT / category / Path(*relative.parts)
    if not target.resolve().is_relative_to(MODEL_ROOT.resolve()):
        raise ValueError("Destino fora da biblioteca.")
    return url, target


class Cancelled(Exception):
    pass


def download_file(job, url, target, token):
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    metadata = partial.with_name(partial.name + ".json")
    try:
        previous = json.loads(metadata.read_text())
    except (OSError, ValueError):
        previous = {}
    if previous.get("url") != url or not previous.get("etag"):
        offset = 0
    job["etag"] = previous.get("etag", "")
    headers = {"Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if offset:
        headers["Range"] = f"bytes={offset}-"
        if job.get("etag"):
            headers["If-Range"] = job["etag"]
    start = time.monotonic()
    with requests.get(url, headers=headers, stream=True, timeout=(20, 45)) as response:
        response.raise_for_status()
        if (
            urlsplit(response.url).scheme != "https"
            or "text/html" in response.headers.get("Content-Type", "").lower()
        ):
            raise ValueError("A origem não entregou um arquivo de modelo HTTPS.")
        append = offset > 0 and response.status_code == 206
        if append and not response.headers.get("Content-Range", "").startswith(
            f"bytes {offset}-"
        ):
            raise ValueError(
                "A origem retornou um intervalo incorreto. Verifique o parcial."
            )
        if not append:
            offset = 0
        length = response.headers.get("Content-Length", "")
        job.update(
            bytes=offset,
            total=offset + int(length) if length.isdigit() else None,
            etag=response.headers.get("ETag", ""),
        )
        needed = (job["total"] or offset) - offset
        if needed > shutil.disk_usage(target.parent).free:
            raise ValueError("Espaço insuficiente no destino. Libere espaço no Drive.")
        metadata.write_text(json.dumps({"url": url, "etag": job["etag"]}))
        with partial.open("ab" if append else "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if job.get("cancel"):
                    raise Cancelled()
                if chunk:
                    output.write(chunk)
                    job["bytes"] += len(chunk)
                    job["speed"] = (job["bytes"] - offset) / max(
                        time.monotonic() - start, 0.01
                    )
                    job["eta"] = (
                        (job["total"] - job["bytes"]) / job["speed"]
                        if job["total"] and job["speed"]
                        else None
                    )
            output.flush()
            os.fsync(output.fileno())
    if job.get("cancel"):
        raise Cancelled()
    if not job["bytes"] or (job["total"] is not None and job["bytes"] != job["total"]):
        raise ValueError("Arquivo incompleto. Use Retomar.")
    partial.replace(target)
    metadata.unlink(missing_ok=True)


async def run_download(job, url, target, token):
    global RUNNING
    acquired = False
    try:
        while RUNNING >= LIMIT:
            if job.get("cancel"):
                raise Cancelled()
            await asyncio.sleep(0.25)
        if job.get("cancel"):
            raise Cancelled()
        RUNNING += 1
        acquired = True
        if target.is_file() and target.stat().st_size:
            job.update(
                bytes=target.stat().st_size, total=target.stat().st_size, existing=True
            )
        else:
            job["status"] = "running"
            persist()
            await asyncio.to_thread(download_file, job, url, target, token)
        job.update(status="complete", speed=0, eta=0)
        INVALIDATE()
    except Cancelled:
        job.update(status="cancelled", speed=0, eta=None)
    except Exception as exc:
        message = (
            f"HTTP {exc.response.status_code}. Verifique o link e a credencial."
            if isinstance(exc, requests.HTTPError) and exc.response is not None
            else str(exc).split("https://", 1)[0][:180]
        )
        job.update(
            status="error", error=message or "Falha de conexão. Tente retomar.", speed=0
        )
    finally:
        if acquired:
            RUNNING -= 1
        ACTIVE_TARGETS.pop(str(target), None)
        job["updated"] = time.time()
        job.pop("cancel", None)
        persist()


async def checked_json(request):
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="Origem não permitida")
    if (
        request.content_type != "application/json"
        or (request.content_length or 0) > 16384
    ):
        raise web.HTTPBadRequest(text="Requisição inválida")
    data = await request.json()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Objeto JSON obrigatório")
    return data


def inventory():
    items = []
    if MODEL_ROOT.is_dir():
        for category in sorted(CATEGORIES):
            base = MODEL_ROOT / category
            if base.is_dir():
                for path in base.rglob("*"):
                    if path.is_file() and path.suffix.lower() in SUFFIXES:
                        items.append(
                            {
                                "category": category,
                                "name": path.relative_to(base).as_posix(),
                                "bytes": path.stat().st_size,
                            }
                        )
    disk = shutil.disk_usage(MODEL_ROOT) if MODEL_ROOT.is_dir() else None
    return {
        "models": items,
        "root": str(MODEL_ROOT),
        "disk_free": disk.free if disk else None,
    }


def metrics():
    import psutil

    ram = psutil.virtual_memory()
    disk = shutil.disk_usage(
        "/content" if Path("/content").exists() else MODEL_ROOT.anchor
    )
    gpus = []
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in r.stdout.splitlines():
            name, total, used, util, temp = [x.strip() for x in line.split(",")]
            gpus.append(
                {
                    "name": name,
                    "total": float(total) * 1048576,
                    "used": float(used) * 1048576,
                    "percent": float(util),
                    "temperature": float(temp),
                }
            )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    output_mode = "drive"
    if MODE == "comfy":
        try:
            import folder_paths
            output_mode = "pc" if str(folder_paths.get_output_directory()).startswith("/content/comfy-colab/output-pc/") else "drive"
        except ImportError:
            output_mode = "unknown"
    return {
        "output_mode": output_mode,
        "mode": MODE,
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "ram_total": ram.total,
        "ram_used": ram.total - ram.available,
        "ram_available": ram.available,
        "disk_free": disk.free,
        "disk_total": disk.total,
        "gpus": gpus,
        "queue_busy": QUEUE(),
        "downloads_busy": any(
            j["status"] in ("queued", "running", "cancelling") for j in JOBS.values()
        ),
        "timestamp": time.time(),
    }


def register(routes, *, mode="comfy", invalidate=None, queue_status=None):
    global MODE, INVALIDATE, QUEUE
    MODE = mode
    if invalidate:
        INVALIDATE = invalidate
    if queue_status:
        QUEUE = queue_status
    restore()

    @routes.get("/comfy-colab/model-download/capabilities")
    async def capabilities(request):
        return web.json_response(
            {
                "manual_url": True,
                "bearer_token": True,
                "version": 2,
                "parallel": LIMIT,
                "mode": MODE,
            }
        )

    @routes.get("/comfy-colab/downloads")
    async def downloads(request):
        return web.json_response({"jobs": list(JOBS.values()), "parallel": LIMIT})

    @routes.post("/comfy-colab/downloads/settings")
    async def settings(request):
        global LIMIT
        data = await checked_json(request)
        try:
            LIMIT = max(1, min(6, int(data["parallel"])))
        except (KeyError, ValueError, TypeError):
            return web.json_response(
                {"error": "Escolha de 1 a 6 downloads."}, status=400
            )
        return web.json_response({"parallel": LIMIT})

    @routes.post("/comfy-colab/model-download")
    async def start(request):
        data = await checked_json(request)
        try:
            url, target = destination(data)
            token = data.get("token", "")
            if (
                not isinstance(token, str)
                or len(token) > 1024
                or "\n" in token
                or "\r" in token
            ):
                raise ValueError("Credencial inválida.")
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not MODEL_ROOT.is_dir():
            return web.json_response(
                {"error": "Monte o Drive antes de baixar."}, status=503
            )
        if str(target) in ACTIVE_TARGETS:
            return web.json_response({"job_id": ACTIVE_TARGETS[str(target)]})
        job_id = uuid.uuid4().hex
        job = dict(
            id=job_id,
            status="queued",
            url=url,
            name=data["name"],
            directory=data["directory"],
            bytes=0,
            total=None,
            speed=0,
            eta=None,
            error=None,
            etag="",
            existing=False,
            updated=time.time(),
            created=time.time(),
        )
        JOBS[job_id] = job
        ACTIVE_TARGETS[str(target)] = job_id
        persist()
        asyncio.create_task(run_download(job, url, target, token))
        return web.json_response(
            {"job_id": job_id, "path": str(target.relative_to(MODEL_ROOT))}, status=202
        )

    @routes.get("/comfy-colab/model-download/{job_id}")
    async def status(request):
        job = JOBS.get(request.match_info["job_id"])
        return web.json_response(
            job or {"error": "Download não encontrado."}, status=200 if job else 404
        )

    @routes.post("/comfy-colab/model-download/{job_id}/cancel")
    async def cancel(request):
        await checked_json(request)
        job = JOBS.get(request.match_info["job_id"])
        if not job:
            return web.json_response({"error": "Download não encontrado."}, status=404)
        if job["status"] in ("queued", "running", "cancelling"):
            job.update(cancel=True, status="cancelling")
        return web.json_response({"status": job["status"]})

    @routes.get("/comfy-colab/models")
    async def models(request):
        return web.json_response(await asyncio.to_thread(inventory))

    @routes.get("/comfy-colab/metrics")
    async def stats(request):
        return web.json_response(await asyncio.to_thread(metrics))
