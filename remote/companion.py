"""Loopback-only, identity-bound companion for one controller-owned runtime."""
from __future__ import annotations

import json
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

if __package__:
    from . import artifacts
    from .bootstrap import MANIFEST, validate_runtime, listening
    from .cache import Cache, memory
    from .common import CATEGORIES, SUFFIXES, Fault, Paths, atomic_bytes, atomic_json, authorize, comfy_json, inside, public_identity, queue_idle, read_json, validate_identity
    from .downloads import Downloads
    from .nodes import Nodes
    from .outputs import Outputs
    from .legacy import Legacy
else:
    import artifacts
    from bootstrap import MANIFEST, validate_runtime, listening
    from cache import Cache, memory
    from common import CATEGORIES, SUFFIXES, Fault, Paths, atomic_bytes, atomic_json, authorize, comfy_json, inside, public_identity, queue_idle, read_json, validate_identity
    from downloads import Downloads
    from nodes import Nodes
    from outputs import Outputs
    from legacy import Legacy


def page(query):
    try:
        offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["50"])[0])
        if offset < 0 or offset > 1000000 or not 1 <= limit <= 100: raise ValueError()
        return offset, limit
    except (ValueError, TypeError): raise Fault("invalid_page", "Use a nonnegative offset and a page size from 1 to 100.") from None


def body_limit(route):
    return 16*1024*1024 if route in {"/workflows/check", "/workflows/import", "/outputs/ack"} else 1024*1024


class CpuSample:
    def __init__(self):
        self.lock = threading.Lock(); self.previous = None; self.at = None; self.value = None

    def read(self):
        with self.lock:
            now = time.monotonic()
            if self.at is not None and now-self.at < 5: return self.value
            self.at = now
            try:
                values = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
                if len(values) < 5: raise ValueError()
                total, idle = sum(values), values[3]+values[4]
                if self.previous:
                    elapsed, resting = total-self.previous[0], idle-self.previous[1]
                    self.value = round(max(0, min(100, 100*(elapsed-resting)/elapsed)), 1) if elapsed > 0 and resting >= 0 else None
                self.previous = (total, idle)
            except (OSError, ValueError, IndexError): self.value = None; self.previous = None
            return self.value


class Service:
    def __init__(self, paths=None, config=None):
        self.paths = paths or Paths()
        self.config = config or read_json(self.paths.state / "session.json")
        validate_identity(self.config)
        self.cpu = self.config.get("gpu") == "CPU"
        self.idle = (lambda: True) if self.cpu else queue_idle
        self.outputs = Outputs(self.paths, self.config, self.capture_idle)
        self.cache = Cache(self.paths, self.idle)
        self.cache.reconcile()
        self.downloads = Downloads(self.paths, self.cache.invalidate)
        self.nodes = Nodes(self.paths, MANIFEST["manager"], call=self.manager_call, idle=self.idle)
        self.legacy = Legacy(self.paths, self.cache.invalidate)
        self.mutation = threading.Lock()
        self.catalog_lock = threading.Lock()
        self.catalog = None; self.catalog_at = 0
        self.cpu_sample = CpuSample()

    def manager_call(self, path, value=None):
        return comfy_json(path, value, headers={"X-Easy-Control": self.config["bearer_token"]})

    def capture_idle(self):
        return self.idle() and (self.cpu or self.nodes.queue_idle())

    def health(self):
        return {**public_identity(self.config), "output_mode": self.config["output_mode"], "mode": "downloads" if self.cpu else "comfy", "remote_ports": {"comfy": None if self.cpu else 8188, "companion": 8190, "mcp": None if self.cpu else 8189}, "paths": {"root": str(self.paths.root), "models": str(self.paths.models), "workflows": str(self.paths.drive / "user/default/workflows"), "outputs": str(self.outputs.root)}}

    def pending_outputs(self, quick=True):
        if self.config["output_mode"] == "pc": return self.outputs.pending(quick=quick)
        pc = Outputs(self.paths, dict(self.config, output_mode="pc"), self.capture_idle)
        return pc.pending(quick=quick)

    def library(self, offset=0, limit=50):
        rows, total = [], 0
        # Scan filenames without retaining the entire model catalog in memory.
        for category in sorted(CATEGORIES):
            folder = self.paths.models / category
            if not folder.is_dir(): continue
            for directory, dirs, names in os.walk(folder, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(directory)/d).is_symlink())
                for filename in sorted(names):
                    file = Path(directory)/filename
                    if not file.is_file() or file.is_symlink() or file.suffix.lower() not in SUFFIXES: continue
                    name = file.relative_to(self.paths.models).as_posix()
                    inside(self.paths.models, name)
                    if offset <= total < offset+limit:
                        stat = file.stat()
                        rows.append({"id": name, "path": name, "name": file.name, "category": category, "size": stat.st_size, "modified_at": stat.st_mtime, "cached": (self.paths.cache / name).is_file()})
                    total += 1
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def snapshot(self):
        queue, hardware = None, None
        metrics = {"cpu_percent": self.cpu_sample.read(), "ram_used": None, "ram_total": None, "gpu": None, "disk_free": None}
        try:
            total, available = memory(); metrics.update(ram_total=total, ram_used=total-available)
            metrics["disk_free"] = __import__("shutil").disk_usage(self.paths.root).free
        except OSError: pass
        if self.cpu:
            queue = {"running": 0, "pending": 0}
        else:
            try:
                raw = comfy_json("/queue"); queue = {"running": len(raw["queue_running"]), "pending": len(raw["queue_pending"])}
                hardware = comfy_json("/system_stats")
                metrics["gpu"] = hardware.get("devices")
            except (Fault, KeyError, TypeError, AttributeError): pass
        packages = [{"id": "core", "version": MANIFEST["core"]["revision"]}, {"id": "frontend", "version": MANIFEST["frontend"]}, {"id": "manager", "version": MANIFEST["manager"]}, {"id": "mcp", "version": MANIFEST["mcp"]}] if not self.cpu else []
        node_state = read_json(self.paths.state / "nodes.json", {})
        packages.extend({"id": key, "version": value.get("ver"), "enabled": value.get("enabled")} for key, value in list(node_state.items())[:100])
        with self.catalog_lock:
            if self.catalog is None or time.monotonic()-self.catalog_at > 30:
                self.catalog = self.library(); self.catalog_at = time.monotonic()
            catalog = self.catalog
        return {"identity": public_identity(self.config), "output_mode":self.config["output_mode"], "queue": queue, "metrics": metrics, "hardware": hardware, "downloads": self.downloads.snapshot(), "models": catalog, "cache": self.cache.snapshot(), "packages": packages, "packages_total": len(node_state)+len(packages)-min(100,len(node_state)), "runtime": {"installed": self.cpu or (self.paths.state / "installed.json").is_file(), "verified": None, "last_saved": (self.paths.state / "runtime-preserved.json").is_file(), "operation": read_json(self.paths.state / "operation-status.json", None)}, "pending_outputs": self.pending_outputs(), "paused": (self.paths.state / "stopping").exists()}

    def switch_outputs(self, data):
        old, target = self.config["output_mode"], data.get("output_mode")
        if target not in {"pc", "drive"} or data.get("previous_output_mode") != old:
            raise Fault("output_mode_conflict", "Output mode changed during restart. Reconnect and verify the current destination.")
        if not (self.paths.state / "stopping").exists() or listening(8188):
            raise Fault("output_switch_not_ready", "Pause admission and stop the owned ComfyUI process before switching its output destination.")
        if self.downloads.busy or self.cache.snapshot()["busy"] or self.nodes.busy:
            raise Fault("runtime_busy", "Finish background operations before switching output destinations.")
        # The admission marker and closed Comfy port make the source stable; its queue API is now offline.
        rows = self.outputs.files()
        report = {"manifest_id":hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(), "pending_outputs":self.outputs.pending(rows)}
        saved = self.outputs.acknowledged()
        if report["pending_outputs"] or saved.get("manifest_id") != report["manifest_id"]:
            raise Fault("outputs_pending", "Deliver and verify the previous output destination before switching modes.")
        self.preserved()
        updated = dict(self.config, output_mode=target)
        outputs = Outputs(self.paths, updated, self.capture_idle)
        atomic_json(self.paths.state / "session.json", updated)
        self.config, self.outputs = updated, outputs
        return {"switched":True, "output_mode":target, **public_identity(self.config)}

    def workflow_list(self, offset, limit):
        root = self.paths.drive / "user/default/workflows"
        if root.is_symlink(): raise Fault("invalid_workflow", "The workflow library cannot be a symbolic link.")
        rows, total = [], 0
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(directory)/d).is_symlink())
            for name in sorted(names):
                file = Path(directory)/name
                if file.suffix.lower() != ".json" or file.is_symlink() or not file.is_file(): continue
                relative = file.relative_to(root).as_posix()
                if len(relative.encode("utf-8")) > 1024: continue
                try: inside(root, relative)
                except Fault: continue
                if offset <= total < offset+limit:
                    stat = file.stat()
                    rows.append({"name": file.name, "relative_path": relative, "remote_path": str(file), "size": stat.st_size, "modified": stat.st_mtime})
                total += 1
                if total > 10000: raise Fault("workflow_library_limit", "The workflow library exceeded 10,000 files. Organize it before listing.")
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def require_idle(self):
        if self.downloads.busy or self.cache.snapshot()["busy"] or self.nodes.busy:
            raise Fault("runtime_busy", "Finish or cancel downloads, cache preparation and node operations before preserving or stopping the VM.")
        if not self.idle(): raise Fault("generation_busy", "Wait for the generation queue before this operation.")
        if not self.cpu and not self.nodes.queue_idle(): raise Fault("runtime_busy", "Finish Manager operations before preserving or stopping the VM.")

    def save(self):
        self.require_idle()
        with self.outputs.barrier():
            self.require_idle()
            if self.cpu: return {"verified": True, "changed": False, "manifest": {"mode": "downloads", "runtime_required": False}}
            from_state = self.nodes.list()
            if any(key != "easy_runtime_guard" and not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", str(value.get("ver", ""))) for key, value in from_state.items()):
                raise Fault("node_unpinned", "An installed node has no reproducible registry version. Resolve its version before preserving the runtime.")
            if __package__: from .common import atomic_json
            else: from common import atomic_json
            atomic_json(self.paths.state / "nodes.json", from_state)
            return artifacts.save(self.paths, validate_runtime)

    def preserved(self):
        if self.cpu: return True
        manifest = read_json(self.paths.artifacts / "selected.json", {})
        validate_runtime(self.paths.root)
        if manifest.get("recipe") != artifacts.recipe() or manifest.get("base") != artifacts.compatibility() or manifest.get("source_signature") != artifacts.source_signature(self.paths.root):
            raise Fault("runtime_not_preserved", "Save and verify the current runtime before ending the VM.")
        artifacts.verify_pointer(self.paths, manifest)
        return True

    def workflow(self, action, data):
        workflow = data.get("workflow")
        selected = data.get("relative_path")
        if selected is not None:
            if action != "check" or workflow is not None: raise Fault("invalid_workflow", "Select either a saved workflow or a workflow JSON for validation.")
            root = self.paths.drive / "user/default/workflows"
            file = inside(root, selected)
            parts = file.relative_to(root).parts
            if len(selected.encode("utf-8")) > 1024 or file.suffix.lower() != ".json" or root.is_symlink() or file.is_symlink() or any(root.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts))):
                raise Fault("invalid_workflow", "Choose a JSON workflow inside this app's Drive library without symbolic links.")
            try:
                with file.open("rb") as stream: payload = stream.read(16*1024*1024+1)
                if len(payload) > 16*1024*1024: raise Fault("workflow_too_large", "Saved workflow exceeded the 16 MiB size limit.")
                workflow = json.loads(payload)
            except Fault: raise
            except (OSError, ValueError): raise Fault("invalid_workflow", "Saved workflow could not be read as JSON. Refresh the library.") from None
        if not isinstance(workflow, dict) or not workflow: raise Fault("invalid_workflow", "Choose a valid ComfyUI workflow JSON.")
        if action == "import":
            filename = data.get("filename", data.get("name"))
            if not isinstance(filename, str) or not filename.endswith(".json") or "/" in filename or "\\" in filename:
                raise Fault("invalid_workflow", "Choose a JSON filename without directories.")
            target = inside(self.paths.drive / "user/default/workflows", filename)
            payload = json.dumps(workflow, ensure_ascii=False, sort_keys=True, indent=2).encode()
            if target.exists() and target.read_bytes() != payload: raise Fault("workflow_conflict", "This workflow filename already has different content; choose another name.")
            atomic_bytes(target, payload)
            return {"imported": True, "remote_path": str(target), "name": filename}
        if self.cpu: raise Fault("gpu_required", "Start a GPU session to check workflow node availability.")
        info = comfy_json("/object_info")
        if not isinstance(info, dict): raise Fault("node_state_unknown", "ComfyUI node definitions could not be verified.")
        nodes = workflow.get("nodes", list(workflow.values()))
        if not isinstance(nodes, list): raise Fault("invalid_workflow", "Workflow nodes must be a list or an API graph.")
        missing_nodes, missing_models, unresolved = set(), set(), []
        if workflow.get("definitions") or workflow.get("subgraphs"): unresolved.append({"reason": "subgraphs_require_execution"})
        for position, node in enumerate(nodes):
            if not isinstance(node, dict): continue
            kind = node.get("class_type", node.get("type"))
            if isinstance(kind, str) and kind not in info and kind not in {"Note", "Reroute", "PrimitiveNode"}: missing_nodes.add(kind)
            spec = info.get(kind, {}).get("input", {})
            definitions = {**spec.get("required", {}), **spec.get("optional", {})}
            values = node.get("inputs") if isinstance(node.get("inputs"), dict) else None
            if values is None:
                widgets = node.get("widgets_values", [])
                # A single combo widget has an unambiguous input mapping. Seed controls and custom widgets do not.
                if len(definitions) == 1 and isinstance(widgets, list) and len(widgets) == 1:
                    field = next(iter(definitions)); definition = definitions[field]
                    if isinstance(definition, (list, tuple)) and definition and isinstance(definition[0], list): values = {field: widgets[0]}
                if values is None and widgets: unresolved.append({"node": node.get("id", position), "reason": "ui_widget_mapping"})
                values = values or {}
            for field, value in values.items():
                definition = definitions.get(field)
                choices = definition[0] if isinstance(definition, (list, tuple)) and definition and isinstance(definition[0], list) else None
                if isinstance(value, str) and choices is not None and (Path(value).suffix.lower() in SUFFIXES or any(isinstance(x, str) and Path(x).suffix.lower() in SUFFIXES for x in choices)) and value not in choices:
                    missing_models.add(value)
                if field not in definitions: unresolved.append({"node": node.get("id", position), "field": field, "reason": "unknown_input"})
            if not kind or (kind not in info and kind in {"Reroute", "PrimitiveNode"}): unresolved.append({"node": node.get("id", position), "reason": "dynamic_node"})
        return {"valid": not missing_nodes and not missing_models and not unresolved, "missing_nodes": sorted(missing_nodes), "missing_models": sorted(missing_models), "unresolved": unresolved, "scope": "static node and exact ComfyUI input-choice availability; execution validates graph behavior", "identity": public_identity(self.config), "relative_path": selected}

    def route(self, method, route, query, data):
        if method == "GET":
            offset, limit = page(query)
            if route == "/health": return self.health()
            if route == "/snapshot": return self.snapshot()
            if route == "/models": return self.library(offset, limit)
            if route == "/downloads": return self.downloads.snapshot(offset, limit)
            if route == "/workflows": return self.workflow_list(offset, limit)
            if route == "/legacy/files": return self.legacy.list(query.get("kind", [None])[0], query.get("category", [None])[0], offset, limit)
            if route in {"/nodes/catalog", "/nodes/versions"}:
                if self.cpu: raise Fault("gpu_required", "Start a GPU session to discover compatible custom nodes.")
                if route == "/nodes/catalog": return self.nodes.registry.catalog(query.get("query", [""])[0], offset, limit, self.nodes.list())
                self.nodes.verify_manager()
                return self.nodes.registry.versions(query.get("id", [None])[0], offset, limit)
            if route == "/nodes":
                if self.cpu: raise Fault("gpu_required", "Start a GPU session to manage custom nodes.")
                values = self.nodes.list(); keys = sorted(values)
                return {"items": [dict(values[k], id=k) for k in keys[offset:offset+limit]], "total": len(keys), "offset": offset, "limit": limit}
            raise Fault("endpoint_missing", "Unknown companion endpoint.")
        if not self.mutation.acquire(blocking=False): raise Fault("runtime_busy", "Another runtime change is in progress; refresh before retrying.")
        was_paused = (self.paths.state / "stopping").exists()
        try:
            if route == "/outputs/resume": return self.outputs.resume()
            if route == "/outputs/switch": return self.switch_outputs(data)
            if route == "/outputs/delivery":
                self.require_idle(); return self.outputs.delivery()
            if route == "/outputs/prepare":
                self.require_idle(); return self.outputs.prepare()
            if route == "/outputs/ack": return self.outputs.ack(data)
            if route == "/runtime/save":
                try: return self.save()
                finally:
                    if not was_paused: self.outputs.resume()
            if route == "/stop/prepare":
                self.require_idle(); report = self.outputs.prepare()
                if report["pending_outputs"] or (self.config["output_mode"] == "drive" and self.pending_outputs(quick=False)): raise Fault("outputs_pending", "Deliver and acknowledge all PC outputs before ending the VM, including any previous PC destination.")
                verified = self.preserved()
                return {"safe_to_stop": True, "pending_outputs": 0, "runtime_verified": verified, "manifest_id": report["manifest_id"]}
            if route == "/restart/prepare":
                self.require_idle()
                with self.outputs.barrier(): return {"paused": True, "safe_to_restart": True}
            if was_paused: raise Fault("session_paused", "Resume this session before adding downloads, cache work or node changes.")
            if route == "/legacy/import":
                self.require_idle()
                result = self.legacy.import_file(data)
                with self.catalog_lock: self.catalog = None
                return result
            if route == "/downloads": return self.downloads.add(data)
            match = re.fullmatch(r"/downloads/([a-f0-9]{32})/(cancel|resume)", route)
            if match: return self.downloads.action(match[1], match[2], data.get("tokens"))
            if route == "/cache/prepare": return self.cache.prepare(data)
            if route == "/cache/cancel": return self.cache.cancel()
            if route == "/cache/clear": return self.cache.clear()
            match = re.fullmatch(r"/nodes/(install|update|remove)", route)
            if match:
                if self.cpu: raise Fault("gpu_required", "Start a GPU session to manage custom nodes.")
                self.require_idle()
                try:
                    with self.outputs.barrier():
                        result = self.nodes.operate(match[1], data)
                        validate_runtime(self.paths.root)
                        return result
                finally: self.outputs.resume()
            match = re.fullmatch(r"/workflows/(check|import)", route)
            if match: return self.workflow(match[1], data)
            raise Fault("endpoint_missing", "Unknown companion endpoint.")
        finally: self.mutation.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self): self.handle_json("GET")
    def do_POST(self): self.handle_json("POST")

    def handle_json(self, method):
        large_body = False
        try:
            authorize(self.server.service.config, self.headers)
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment: raise Fault("invalid_endpoint", "Use a companion path.")
            if self.headers.get("Transfer-Encoding"): raise Fault("invalid_request", "Chunked companion requests are unsupported.")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > body_limit(parsed.path): raise Fault("request_too_large", "Companion request exceeded the endpoint payload limit.")
            if length > 1024*1024:
                if not self.server.large_bodies.acquire(blocking=False): raise Fault("runtime_busy", "Another large request is in progress; retry after it finishes.")
                large_body = True
            if method == "POST":
                raw = self.rfile.read(length)
                if len(raw) != length: raise Fault("invalid_request", "The complete declared JSON request was not received.")
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict): raise Fault("invalid_request", "Submit a JSON object.")
            else: data = {}
            result = {"ok": True, "result": self.server.service.route(method, parsed.path, parse_qs(parsed.query), data)}
            status = 200
        except Fault as error:
            result = {"ok": False, "error": {"code": error.code, "message": error.message}}
            status = 401 if error.code == "unauthorized" else 409 if error.code in {"identity_mismatch", "runtime_busy", "generation_busy", "session_paused"} else 400
        except Exception:
            status = 400; result = {"ok": False, "error": {"code": "companion_failed", "message": "The operation could not be verified; preserve the VM and inspect recovery."}}
        try:
            payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store"); self.end_headers()
            try: self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError): pass
        finally:
            if large_body: self.server.large_bodies.release()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, service):
        self.service = service
        self.capacity = threading.BoundedSemaphore(16)
        self.large_bodies = threading.BoundedSemaphore(1)
        super().__init__(address, Handler)

    def get_request(self):
        sock, address = super().get_request(); sock.settimeout(30); return sock, address

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False): self.shutdown_request(request); return
        try: super().process_request(request, address)
        except Exception: self.capacity.release(); raise

    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.capacity.release()


def main():
    os.umask(0o077)
    Server(("127.0.0.1", 8190), Service()).serve_forever(poll_interval=0.5)


if __name__ == "__main__": main()
