"""Installed ComfyUI custom node: same-origin remote model downloads."""
import json
from pathlib import Path
from server import PromptServer
from .model_bridge import register

CONFIG = json.loads(Path("/content/easy-comfy-colab-2/state/session.json").read_text())
register(PromptServer.instance.routes, CONFIG)
WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
