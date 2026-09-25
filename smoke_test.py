"""Testa uma execução real do ComfyUI através do túnel local."""

from __future__ import annotations

import json
import struct
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE = "http://127.0.0.1:18188"


def get_json(path: str) -> dict:
    with urlopen(BASE + path, timeout=15) as response:
        return json.load(response)


def main() -> None:
    stats = get_json("/system_stats")
    devices = stats.get("devices", [])
    if not any("cuda" in str(device.get("type", "")).lower() for device in devices):
        raise RuntimeError(f"ComfyUI não informou GPU CUDA: {devices}")

    model_name = "RealESRGAN_x4plus.safetensors"
    workflow = {
        "1": {
            "class_type": "EmptyImage",
            "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 0x336699},
        },
        "2": {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": model_name},
        },
        "3": {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]},
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "ComfyColab_smoke",
                "images": ["3", 0],
            },
        },
    }
    body = json.dumps({"prompt": workflow}).encode("utf-8")
    request = Request(
        BASE + "/prompt",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        queued = json.load(response)
    prompt_id = queued["prompt_id"]

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        history = get_json("/history/" + prompt_id).get(prompt_id)
        if history is not None:
            status = history.get("status", {})
            if status.get("status_str") != "success":
                raise RuntimeError(f"Workflow terminou com erro: {status}")
            images = history.get("outputs", {}).get("4", {}).get("images", [])
            if not images:
                raise RuntimeError(f"Workflow não produziu imagem: {history}")
            image = images[0]
            query = urlencode(
                {
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                }
            )
            with urlopen(BASE + "/view?" + query, timeout=30) as response:
                header = response.read(24)
                if header[:8] != b"\x89PNG\r\n\x1a\n":
                    raise RuntimeError("A resposta de /view não é uma imagem PNG.")
                dimensions = struct.unpack(">II", header[16:24])
                if dimensions != (128, 128):
                    raise RuntimeError(f"Dimensão de saída inesperada: {dimensions}")
            print("Teste concluído: RealESRGAN executado na GPU do Colab e PNG 128x128 recebido no notebook.")
            return
        time.sleep(2)
    raise TimeoutError("A geração de teste não terminou em 90 segundos.")


if __name__ == "__main__":
    main()
