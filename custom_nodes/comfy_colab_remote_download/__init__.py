"""Downloads na VM, inventário e telemetria para o Comfy Colab."""
import folder_paths
from server import PromptServer
from .service import register
from .model_cache import ModelCache, install_resolver
from .service import MODEL_ROOT, CATEGORIES, SUFFIXES

WEB_DIRECTORY = './web'
NODE_CLASS_MAPPINGS = {}

def queue_busy():
    try:
        running, pending = PromptServer.instance.prompt_queue.get_current_queue()
        return bool(running or pending)
    except Exception:
        return None

cache = ModelCache(MODEL_ROOT, CATEGORIES, SUFFIXES, queue_status=queue_busy)
install_resolver(folder_paths, cache)
register(PromptServer.instance.routes, invalidate=folder_paths.filename_list_cache.clear,
         queue_status=queue_busy, cache=cache)
cache.start()
