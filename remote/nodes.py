"""Versioned ComfyUI-Manager operations; no direct arbitrary installers."""
import re
import threading
import time
import uuid
from pathlib import Path

try:
    from .common import Fault, atomic_json, comfy_json, queue_idle, read_json
    from .registry import Registry
except ImportError:
    from common import Fault, atomic_json, comfy_json, queue_idle, read_json
    from registry import Registry


class Nodes:
    def __init__(self, paths, version, call=comfy_json, idle=queue_idle, registry=None):
        self.paths, self.version, self.call, self.idle = paths, version, call, idle
        self.lock = threading.Lock(); self.busy = False
        try:
            core_version = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)', (paths.comfy / "comfyui_version.py").read_text()).group(1)
        except (OSError, AttributeError): core_version = None
        manifest = read_json(Path(__file__).with_name("runtime.json"))
        self.registry = registry or Registry(core_version, manifest["frontend"])

    def verify_manager(self):
        version = self.call("/v2/manager/version")
        if str(version).strip().lstrip("vV") != self.version:
            raise Fault("manager_version", "Manager version changed; use the pinned runtime before managing nodes.")

    def list(self):
        self.verify_manager()
        nodes = self.call("/v2/customnode/installed")
        if not isinstance(nodes, dict): raise Fault("manager_state", "Installed node state could not be verified.")
        return nodes

    def queue_idle(self):
        self.verify_manager()
        status = self.call("/v2/manager/queue/status")
        if not isinstance(status, dict) or not {"is_processing", "total_count", "in_progress_count"} <= status.keys():
            raise Fault("manager_state", "Manager queue state could not be verified.")
        return not status["is_processing"] and not status["in_progress_count"] and not status["total_count"] and not status.get("pending_count", 0)

    def operate(self, action, data):
        node_id, version = data.get("id"), data.get("version")
        if not isinstance(node_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", node_id): raise Fault("invalid_node", "Choose a registered node identifier.")
        if action != "remove" and (not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:\+[a-zA-Z0-9.-]+)?", version)):
            raise Fault("invalid_version", "Choose an explicit stable node version.")
        if not self.lock.acquire(blocking=False): raise Fault("node_busy", "Wait for the current node operation.")
        try:
            self.busy = True
            if not self.idle(): raise Fault("generation_busy", "Wait for the generation queue before modifying nodes.")
            existing = self.list()
            current = existing.get(node_id)
            if action == "install" and current: raise Fault("node_conflict", "This node already exists; choose a versioned update.")
            if action in {"update", "remove"} and (not current or current.get("ver") != data.get("expected_version")):
                raise Fault("node_conflict", "The installed node version changed; refresh before modifying it.")
            if action != "remove": self.registry.verify_version(node_id, version)
            if not self.queue_idle():
                raise Fault("node_busy", "The Manager queue is already busy; no operation was added.")
            route = "uninstall" if action == "remove" else "install"
            task_id = uuid.uuid4().hex
            params = {"node_name": node_id, "is_unknown": False} if action == "remove" else {"id": node_id, "version": current["ver"] if current else version, "selected_version": version, "mode": "cache", "channel": "default", "skip_post_install": False}
            payload = {"ui_id": task_id, "client_id": "easy-comfy-colab-2", "kind": route, "params": params}
            self.call("/v2/manager/queue/task", payload)
            self.call("/v2/manager/queue/start", {})
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                history = self.call("/v2/manager/queue/history?ui_id=" + task_id)
                task = history.get("history", {}).get(task_id, {}) if isinstance(history, dict) else {}
                status = task.get("status", {})
                if status.get("completed"):
                    if status.get("status_str") != "success": raise Fault("node_failed", "Manager rejected or failed the requested node operation; inspect its installed state.")
                    break
                time.sleep(0.5)
            else: raise Fault("node_timeout", "Manager operation did not finish; inspect the session before retrying.")
            final = self.list()
            if (action == "remove" and node_id in final) or (action != "remove" and final.get(node_id, {}).get("ver") != version):
                raise Fault("node_failed", "Manager did not reach the requested node version; no success was recorded.")
            atomic_json(self.paths.state / "nodes.json", final)
            (self.paths.state / "runtime-preserved.json").unlink(missing_ok=True)
            return {"verified": True, "nodes": final, "requires_restart": True}
        finally:
            self.busy = False; self.lock.release()
