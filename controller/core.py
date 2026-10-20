from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, unquote, urlsplit, urlencode

from controller.state import AppError, Store, secret_prompt
from controller.monitor import LocalMonitor

SESSION_KEYS = {"state", "account_id", "session_id", "phase", "started_at", "comfy_url", "mcp_url", "hardware", "output_mode", "pending_outputs", "error", "error_code"}
PREFERENCES = {"language": "pt-BR", "idle_minutes": 0, "balance_alert": 20, "auto_open": True}
MODEL_CATEGORIES = {"checkpoints", "diffusion_models", "loras", "vae", "text_encoders", "clip", "clip_vision", "controlnet", "upscale_models", "embeddings", "unet", "llm_gguf", "onnx", "audio_encoders", "model_patches"}
SECRET_KEYS = {"token", "tokens", "bearer", "bearer_token", "refresh_token", "access_token", "client_secret", "credentials", "authorization"}
RECORDED_ACTIONS = {"app.preferences", "account.importLegacy", "account.connect", "account.submitCode", "account.select", "account.remove", "account.update", "account.credentials", "session.start", "session.cancel", "session.reconnect", "session.restart", "session.stop", "session.freeMemory", "session.runtimeSave", "download.add", "download.cancel", "download.resume", "cache.prepare", "cache.cancel", "cache.clear", "workflow.check", "workflow.import", "node.install", "node.update", "node.remove", "drive.copy", "drive.share", "legacy.import", "training.import", "training.prepare", "training.start", "training.resume", "training.cancel", "training.preserve", "training.export"}


def resources():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def download_payload(params):
    urls = params.get("urls")
    if not isinstance(urls, list) or not 1 <= len(urls) <= 50:
        raise AppError("invalid_downloads", "Add between 1 and 50 model links.")
    normalized = []
    for entry in urls:
        url = entry if isinstance(entry, str) else entry.get("url", "") if isinstance(entry, dict) else ""
        filename = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(url, str) or any(ord(c) < 32 for c in url):
            raise AppError("invalid_model_url", "Use an HTTPS Hugging Face file or Civitai download link.")
        if "|" in url:
            url, filename = (value.strip() for value in url.split("|", 1))
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in {"huggingface.co", "civitai.com"} or parts.username or parts.password or parts.port not in (None, 443):
            raise AppError("invalid_model_url", "Use an HTTPS Hugging Face file or Civitai download link.")
        query = parts.query
        for _ in range(8):
            decoded = unquote(query)
            if decoded == query:
                break
            query = decoded
        if "%" in query or any(key.casefold() in {"token", "api_key", "apikey", "access_token", "authorization", "key"} for key, _ in parse_qsl(query, keep_blank_values=True)) or parts.fragment:
            raise AppError("url_secret", "Keep credentials outside model URLs; use native credential settings.")
        value = {"url": url}
        if filename is not None:
            if not isinstance(filename, str) or not filename or any(c in filename for c in "\\/:|\r\n") or filename in (".", ".."):
                raise AppError("invalid_model_filename", "Use a simple model filename, without paths.")
            value["filename"] = filename
        if isinstance(entry, dict) and entry.get("sha256"):
            value["sha256"] = entry["sha256"]
        if parts.hostname == "civitai.com" and not filename:
            raise AppError("model_filename_required", "Civitai downloads require an explicit filename: URL | model.safetensors.")
        normalized.append(value)
    category = params.get("category", "checkpoints")
    category = "llm_gguf" if category == "LLM" else category
    parallel = params.get("parallel", 3)
    if category not in MODEL_CATEGORIES or type(parallel) is not int or not 1 <= parallel <= 6:
        raise AppError("invalid_download_settings", "Choose a supported category and 1–6 parallel downloads.")
    return {"urls": normalized, "category": category, "parallel": parallel}


def packages(items):
    return [{**item, "version": item.get("version", item.get("ver")), "installed": True, "protected": bool(item.get("protected")) or item.get("id") in {"core", "frontend", "manager", "mcp"}} for item in items]


def page_query(params):
    offset, limit = params.get("offset", 0), params.get("limit", 50)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AppError("invalid_page", "Choose a valid library page with 1–100 items.")
    return f"?offset={offset}&limit={limit}"


def request_body(path, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    limit = 16 * 1024 * 1024 if path.split("?", 1)[0] in {"/workflows/check", "/workflows/import", "/outputs/ack"} else 1024 * 1024
    if len(body) > limit:
        raise AppError("request_too_large", "This operation exceeds its transfer size limit; split the data before retrying.")
    return body


def safe_output(root, entry, session_id=None):
    relative = entry.get("relative_path", "")
    if not isinstance(relative, str):
        raise AppError("output_path_invalid", "Remote output path is invalid. The VM was preserved.")
    path = PurePosixPath(relative)
    if not path.parts or len(relative.encode("utf-8")) > 1024 or path.is_absolute() or ".." in path.parts or "\\" in relative or any(ord(c) < 32 for c in relative) or ":" in relative or any(p.endswith((".", " ")) or p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *("COM" + str(n) for n in range(1, 10)), *("LPT" + str(n) for n in range(1, 10))} for p in path.parts):
        raise AppError("output_path_invalid", "Remote output path is invalid. The VM was preserved.")
    remote = entry.get("remote_path", "")
    prefix = "/content/easy-comfy-colab-2/output-pc/" + (session_id + "/" if session_id else "")
    if not isinstance(remote, str) or not remote.startswith(prefix) or ".." in PurePosixPath(remote).parts or session_id and remote != prefix + relative:
        raise AppError("output_source_invalid", "Output source is outside the owned runtime.")
    size, checksum = entry.get("size"), entry.get("sha256")
    if type(size) is not int or size < 0 or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise AppError("output_manifest_invalid", "Output manifest could not be verified.")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise AppError("output_path_invalid", "Output destination escaped its account folder.")
    return target


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


class Companion:
    def __init__(self, session, bearer):
        import requests
        self.http = requests.Session()
        self.http.trust_env = False
        self.base = "http://127.0.0.1:28190"
        self.identity = {key: session[key] for key in ("account_id", "session_id", "nonce")}
        self.headers = {"Authorization": "Bearer " + bearer,
                        "X-Easy-Account": session["account_id"], "X-Easy-Session": session["session_id"], "X-Easy-Nonce": session["nonce"]}

    def health(self):
        response = self.http.get(self.base + "/health", headers=self.headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get("ok") is True and "result" in data:
            data = data["result"]
        if any(data.get(k) != value for k, value in self.identity.items()):
            raise AppError("session_identity_mismatch", "The tunnel belongs to another session. No remote operation was performed.")
        return data

    def call(self, path, payload=None, timeout=120):
        body = request_body(path, payload) if payload is not None else None
        self.health()
        response = self.http.request("POST" if payload is not None else "GET", self.base + path, headers={**self.headers, "Content-Type": "application/json; charset=utf-8"}, data=body, timeout=timeout)
        try:
            data = response.json()
        except ValueError:
            raise AppError("remote_response_invalid", "The remote response could not be verified. The VM remains active.") from None
        if response.status_code >= 400 or data.get("ok") is False:
            code = data.get("error", {}).get("code", "remote_operation_failed")
            raise AppError(code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "remote_operation_failed", "The remote operation could not complete. The VM remains active.")
        return data.get("result", data)


class Controller:
    def __init__(self, store=None, emit=None, auth=None, colab_factory=None, transport_factory=None):
        self.store = store or Store()
        self.send = emit or (lambda *_: None)
        self.auth = auth
        self.colab_factory = colab_factory
        self.transport_factory = transport_factory
        self.operation = threading.Lock()
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.transport = self.colab = self.companion = None
        self.remote_snapshot = {}
        manifest = json.loads((resources() / "remote/runtime.json").read_text(encoding="utf-8"))
        self.pending_base = [item.get("folder", item["id"]) for item in manifest.get("packages", []) if item.get("status", "enabled") != "enabled"]
        self.output_delivery = None
        self.session = self.store.get("active_session") or self.idle()
        if self.session["state"] != "idle":
            self.session.update(state="error", phase="recovery", comfy_url=None, mcp_url=None, error_code="recovery_unverified", error="A recorded owned VM may still be active. Reconnect before operating it.")
        self.started = time.monotonic()
        self.idle_since = None
        self.last_balance = 0
        self.usage_cache = {}
        self.usage_locks = {}
        self.local_monitor = LocalMonitor()
        self.benchmark = os.environ.get("ECC_BENCHMARK") == "1"

    @staticmethod
    def idle():
        return {"state": "idle", "account_id": None, "session_id": None, "phase": None, "started_at": None,
                "comfy_url": None, "mcp_url": None, "hardware": None, "output_mode": None, "pending_outputs": None, "error": None}

    def emit(self, event, data):
        self.send(event, public(data))

    def google(self):
        if self.auth is None:
            from controller.google import GoogleAuth
            self.auth = GoogleAuth(self.store, self.emit)
        return self.auth

    def phase(self, state, phase):
        self.session.update(state=state, phase=phase, error=None, error_code=None)
        self.store.set("active_session", self.session)
        self.emit("state", {"session": self.public_session()})
        self.emit("progress", {"account_id": self.session["account_id"], "session_id": self.session["session_id"], "phase": phase, "elapsed_seconds": round(time.monotonic() - self.started, 1)})

    def public_session(self):
        result = {k: self.session.get(k) for k in SESSION_KEYS}
        if isinstance(result["hardware"], dict):
            devices = result["hardware"].get("devices", [])
            result["hardware"] = next((d.get("name") for d in devices if isinstance(d, dict) and d.get("type") == "cuda"), None)
        if not isinstance(result["hardware"], (str, type(None))):
            result["hardware"] = None
        return result

    def snapshot(self):
        usage = self.cached_usage(self.store.get("selected_account_id"))
        downloads = self.remote_snapshot.get("downloads", [])
        if isinstance(downloads, dict):
            downloads = downloads.get("items", [])
        return {"accounts": self.store.accounts(), "selected_account_id": self.store.get("selected_account_id"),
                "session": self.public_session(), "downloads": public([{**item, "name": item.get("name", item.get("filename", item.get("id")))} for item in downloads]),
                "models": public(self.remote_snapshot.get("models", {}).get("items", []) if isinstance(self.remote_snapshot.get("models"), dict) else self.remote_snapshot.get("models", [])), "packages": public(packages(self.remote_snapshot.get("packages", []))),
                "history": self.store.history(), "preferences": self.store.get("preferences", PREFERENCES),
                "account_usage": usage,
                "metrics": public({**(self.remote_snapshot.get("metrics") or {}), "balance": usage["balance"], "rate": usage["rate"]}),
                "local_metrics": self.local_monitor.sample(), "cache": public(self.remote_snapshot.get("cache", {"status": "unknown", "busy": None, "items": [], "error": None})),
                "runtime": public(self.remote_snapshot.get("runtime")),
                "training": public(self.remote_snapshot.get("training")),
                "output_delivery": public(self.output_delivery),
                "downloads_total": self.remote_snapshot.get("downloads", {}).get("total") if isinstance(self.remote_snapshot.get("downloads"), dict) else None,
                "packages_total": self.remote_snapshot.get("packages_total"),
                "capabilities": {"native": True, "legacy_import": True, "google_oauth_configured": Path(os.environ.get("EASY_COMFY_OAUTH_CLIENT", ".private/oauth-client.json")).is_file(), "owned_remote_connected": self.companion is not None, "version": "0.1.2-dev", "pending_base_packages": self.pending_base}}

    def account_id(self, params):
        account_id = params.get("account_id") or self.store.get("selected_account_id")
        self.store.account(account_id)
        return account_id

    def cached_usage(self, account_id):
        empty = {"account_id": account_id, "balance": None, "rate": None, "status": "idle", "checked_at": None, "error_code": None}
        try:
            account = self.store.account(account_id)
        except AppError:
            return empty
        if not account["connected"]:
            return empty
        saved = self.usage_cache.get(account_id)
        return dict(saved["data"]) if saved and saved["subject"] == account["subject"] else empty

    def read_usage(self, account_id, force=False):
        """Read account CUs only; never assign or recover a runtime."""
        account = self.store.account(account_id)
        if self.benchmark or not account["connected"]:
            return self.cached_usage(account_id)
        lock = self.usage_locks.setdefault(account_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return self.cached_usage(account_id)
        try:
            cached = self.usage_cache.get(account_id)
            if not force and cached and cached["subject"] == account["subject"] and time.monotonic() - cached["at"] < 60:
                return dict(cached["data"])
            data = {"account_id": account_id, "balance": None, "rate": None, "status": "loading", "checked_at": None, "error_code": None}
            self.usage_cache[account_id] = {"subject": account["subject"], "at": time.monotonic(), "data": data}
            try:
                from controller.google import Colab
                colab = self.colab if self.colab and getattr(self.colab, "account_id", None) == account_id else (self.colab_factory or Colab)(self.google(), account_id, self.emit)
                usage = colab.client.get_consumption_user_info()
                balance, rate = usage.paid_compute_units_balance, usage.consumption_rate_hourly
                if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in (balance, rate)):
                    raise ValueError("Invalid consumption response")
                data = {**data, "balance": balance, "rate": rate, "status": "ready"}
            except Exception as error:
                code = error.code if isinstance(error, AppError) and error.code in {"account_mismatch", "identity_unverified", "account_not_connected", "credential_format", "credential_type"} else "balance_unavailable"
                data = {**data, "status": "error", "error_code": code}
            if self.closed.is_set() or self.store.account(account_id)["subject"] != account["subject"]:
                return self.cached_usage(account_id)
            data["checked_at"] = time.time()
            self.usage_cache[account_id] = {"subject": account["subject"], "at": time.monotonic(), "data": data}
            return dict(data)
        finally:
            lock.release()

    def require_remote(self):
        if not self.companion or not self.transport or not self.session.get("endpoint"):
            raise AppError("session_not_connected", "Start or reconnect the owned session first.")
        if self.session["state"] not in ("ready", "stopping", "starting", "reconnecting"):
            raise AppError("session_not_ready", "Reconnect the owned VM before operating it.")
        self.companion.health()
        return self.companion

    def bootstrap(self, action, **overrides):
        payload = {key: self.session[key] for key in ("account_id", "session_id", "nonce", "gpu", "output_mode")}
        payload.update(action=action, bearer_token=self.store.secret(self.session["account_id"], "session:" + self.session["session_id"]), **overrides)
        reply = self.transport.bootstrap(payload)
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            code = reply.get("error", {}).get("code", "runtime_operation_failed") if isinstance(reply, dict) else "runtime_operation_failed"
            raise AppError(code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "runtime_operation_failed", "The runtime operation failed. The owned VM remains available for recovery.")
        return reply["result"]

    def check_cancel(self):
        if self.closed.is_set():
            raise AppError("controller_closed", "The app closed. The recorded owned VM remains available for recovery.")
        if self.cancelled.is_set():
            raise AppError("cancelled", "Startup was cancelled. Reconnect the recorded owned VM to finish safe recovery.")

    def reset_metrics(self):
        self.last_balance = 0
        self.account_metrics = {"balance": None, "rate": None}
        self.remote_snapshot = {}
        self.output_delivery = None
        self.idle_since = None

    def start(self, params):
        if self.session.get("endpoint") or self.session.get("notebook_id"):
            raise AppError("session_exists", "An owned session is recorded. Reconnect or safely stop it before starting another.")
        from controller.google import Colab
        from controller.transport import Transport, reserved_listeners
        account_id = self.account_id(params)
        self.store.update(account_id, **{k: params[k] for k in ("gpu", "output_mode") if k in params})
        account = self.store.account(account_id)
        self.reset_metrics()
        self.started = time.monotonic()
        self.google().context(account_id)
        listeners = reserved_listeners()
        self.cancelled.clear()
        self.session = {**self.idle(), "account_id": account_id, "session_id": uuid.uuid4().hex,
                        "nonce": uuid.uuid4().hex, "notebook_id": str(uuid.uuid4()), "gpu": account["gpu"],
                        "output_mode": account["output_mode"], "started_at": time.time(), "subject": account["subject"]}
        self.session["services_started"] = False
        self.store.secret(account_id, "session:" + self.session["session_id"], os.urandom(32).hex())
        preparing = False
        try:
            self.phase("allocating", "allocation")
            self.check_cancel()
            self.colab = (self.colab_factory or Colab)(self.google(), account_id, self.emit)
            self.colab.session_id = self.session["session_id"]
            assignment = self.colab.allocate(uuid.UUID(self.session["notebook_id"]), account["gpu"])
            self.session["endpoint"] = assignment.endpoint
            self.store.set("active_session", self.session)
            self.check_cancel()
            self.phase("preparing", "drive")
            self.colab.mount(assignment, self.cancelled)
            self.session["drive_mounted"] = True
            self.store.set("active_session", self.session)
            self.check_cancel()
            self.transport = (self.transport_factory or Transport)(self.store, account_id, assignment.runtime_proxy_info, listeners)
            self.session["ssh_fingerprint"] = self.transport.fingerprint
            self.phase("preparing", "runtime")
            self.transport.upload_remote(resources() / "remote")
            preparing = True
            self.bootstrap("prepare")
            self.session["prepared"] = True
            self.store.set("active_session", self.session)
            self.check_cancel()
            self.phase("starting", "services")
            result = self.bootstrap("start")
            self.session["services_started"] = True
            self.store.set("active_session", self.session)
            self.transport.forward(result["remote_ports"])
            self.connect_companion()
            self.check_cancel()
            self.ready(result)
            return self.public_session()
        except Exception as error:
            if not preparing and not self.closed.is_set() and self.session.get("endpoint") and isinstance(error, AppError) and error.code == "cancelled":
                self.colab.stop(self.session["endpoint"])
                self.session = self.idle()
                self.store.set("active_session", self.session)
            for sock in listeners.values():
                if not self.transport:
                    sock.close()
            raise

    def connect_companion(self):
        self.companion = Companion(self.session, self.store.secret(self.session["account_id"], "session:" + self.session["session_id"]))
        deadline = time.monotonic() + 90
        while True:
            try:
                self.companion.health()
                self.push_model_credentials()
                return
            except AppError:
                raise
            except Exception:
                if time.monotonic() > deadline:
                    raise AppError("readiness_timeout", "Owned companion did not become ready. The VM remains active.") from None
                self.cancelled.wait(1)
                self.check_cancel()

    def model_credentials(self):
        return {key: self.store.secret(self.session["account_id"], provider) or "" for provider, key in (("Hugging Face", "huggingface"), ("Civitai", "civitai"))}

    def push_model_credentials(self):
        # This authenticated request stays outside the browser and JSONL events.
        return self.companion.call("/credentials/models", {"tokens": self.model_credentials()})

    def ready(self, result):
        import requests
        from controller.transport import PORTS
        ports = result["remote_ports"]
        hardware = "CPU" if self.session.get("gpu") == "CPU" else None
        if ports.get("comfy"):
            http = requests.Session()
            http.trust_env = False
            response = http.get(f"http://127.0.0.1:{PORTS['comfy']}/system_stats", timeout=10)
            response.raise_for_status()
            stats = response.json()
            hardware = next((item.get("name") for item in stats.get("devices", []) if item.get("type") == "cuda"), None)
        if ports.get("mcp"):
            http = requests.Session()
            http.trust_env = False
            url = f"http://127.0.0.1:{PORTS['mcp']}/mcp"
            headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}

            def rpc(method, params, sequence):
                response = http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": sequence, "method": method, "params": params}, timeout=90)
                response.raise_for_status()
                if response.headers.get("Mcp-Session-Id"):
                    headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    messages = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
                    data = next((m for m in messages if m.get("id") == sequence), {})
                else:
                    data = response.json()
                if data.get("error") or not isinstance(data.get("result"), dict):
                    raise AppError("mcp_not_ready", "The official MCP did not return a valid readiness response. The VM remains active.")
                return data["result"]

            initialized = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "easy-comfy-colab-2", "version": "0.1.0"}}, 1)
            if initialized.get("protocolVersion") in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
                headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
            http.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10).raise_for_status()
            info = rpc("tools/call", {"name": "server_info", "arguments": {}}, 2)
            if info.get("isError"):
                raise AppError("mcp_not_ready", "The official MCP could not confirm ComfyUI. The VM remains active.")
            value = info.get("structuredContent")
            if not value:
                value = next((json.loads(item["text"]) for item in info.get("content", []) if item.get("type") == "text"), {})
            if value.get("server", {}).get("running") is not True or value.get("config", {}).get("default_workspace") != "/content/easy-comfy-colab-2/ComfyUI":
                raise AppError("mcp_target_mismatch", "The official MCP did not confirm this app's ComfyUI workspace. The VM remains active.")
        self.session.update(comfy_url=f"http://127.0.0.1:{PORTS['comfy']}/" if ports.get("comfy") else None,
                            mcp_url=f"http://127.0.0.1:{PORTS['mcp']}/mcp" if ports.get("mcp") else None,
                            hardware=hardware, pending_outputs=0)
        self.phase("ready", "ready")

    def reconnect(self):
        from controller.google import Colab
        from controller.transport import Transport, reserved_listeners
        if not self.session.get("notebook_id"):
            raise AppError("recovery_unverified", "No owned notebook allocation is recorded. No legacy or unrelated VM will be adopted.")
        account_id = self.session["account_id"]
        if self.store.account(account_id)["subject"] != self.session["subject"]:
            raise AppError("account_mismatch", "The recorded session belongs to another account identity.")
        self.cancelled.clear()
        self.reset_metrics()
        self.phase("reconnecting", "ownership")
        if self.transport:
            self.transport.close()
        self.transport = self.companion = None
        if self.colab:
            self.colab.close()
        listeners = reserved_listeners()
        try:
            self.colab = (self.colab_factory or Colab)(self.google(), account_id, self.emit)
            self.colab.session_id = self.session["session_id"]
            assignment = self.colab.recover(uuid.UUID(self.session["notebook_id"]), self.session["gpu"])
            if self.session.get("endpoint") and assignment.endpoint != self.session["endpoint"]:
                raise AppError("owned_vm_missing", "The exact owned notebook endpoint changed. No VM was adopted or stopped.")
            self.session["endpoint"] = assignment.endpoint
            self.store.set("active_session", self.session)
            if not self.session.get("drive_mounted"):
                self.colab.mount(assignment, self.cancelled)
                self.session["drive_mounted"] = True
                self.store.set("active_session", self.session)
            self.check_cancel()
            self.transport = (self.transport_factory or Transport)(self.store, account_id, assignment.runtime_proxy_info, listeners, self.session.get("ssh_fingerprint"))
            self.session["ssh_fingerprint"] = self.transport.fingerprint
            self.store.set("active_session", self.session)
            if not self.session.get("prepared"):
                self.transport.upload_remote(resources() / "remote")
                self.bootstrap("prepare")
                self.session["prepared"] = True
                self.store.set("active_session", self.session)
                result = self.bootstrap("start")
            else:
                result = self.bootstrap("health" if self.session.get("services_started", False) or self.session.get("output_transition") else "start")
            for key in ("account_id", "session_id", "nonce"):
                if result.get(key) != self.session[key]:
                    raise AppError("session_identity_mismatch", "Recorded runtime identity changed. No remote mutation was performed.")
            self.reconcile_output_mode(result)
            recovering_outputs = self.session.get("output_transition")
            if recovering_outputs:
                result = self.bootstrap("start")
                self.reconcile_output_mode(result)
            self.transport.forward(result["remote_ports"])
            self.connect_companion()
            self.ready(result)
            if recovering_outputs:
                self.store.update(account_id, output_mode=self.session["output_mode"])
                self.session.pop("output_transition", None)
            self.session["services_started"] = True
            self.store.set("active_session", self.session)
            return self.public_session()
        except Exception as error:
            if not self.transport:
                for sock in listeners.values():
                    sock.close()
            if isinstance(error, AppError) and error.code == "owned_allocation_absent":
                self.store.event("session.recovery", account_id, "allocation_absent", session_id=self.session["session_id"])
                self.session = self.idle()
                self.store.set("active_session", self.session)
                self.emit("state", {"session": self.public_session()})
                return self.public_session()
            raise

    def copy_outputs(self, manifest, verify_context=None):
        if verify_context:
            verify_context()
        if not isinstance(manifest.get("files"), list) or len(manifest["files"]) > 10000 or not manifest.get("manifest_id"):
            raise AppError("output_manifest_invalid", "Outputs were not verified. The VM remains active.")
        root = Path.home() / "Easy Comfy Colab 2 Results" / self.session["account_id"] / "output"
        if self.session["output_mode"] == "pc":
            root.mkdir(parents=True, exist_ok=True)
        acknowledged = []
        for entry in manifest["files"]:
            if verify_context:
                verify_context()
            if self.session["output_mode"] == "drive":
                relative = entry.get("relative_path", "")
                if entry.get("remote_path") != "/content/drive/MyDrive/EasyComfyColab2/output/" + relative:
                    raise AppError("output_source_invalid", "Drive output source is outside the new runtime root.")
                # The companion hashes mounted Drive files; acknowledgement rechecks durability there.
                safe_output(root, {**entry, "remote_path": "/content/easy-comfy-colab-2/output-pc/" + self.session["session_id"] + "/" + relative}, self.session["session_id"])
                acknowledged.append({key: entry[key] for key in ("relative_path", "size", "sha256")})
                continue
            target = safe_output(root, entry, self.session["session_id"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.stat().st_size != entry["size"] or digest(target) != entry["sha256"]:
                    raise AppError("output_conflict", "A different local output already exists. Choose recovery before stopping; nothing was overwritten.")
            else:
                temporary = target.with_name(target.name + ".easy-part")
                if not temporary.resolve().is_relative_to(root.resolve()):
                    raise AppError("output_path_invalid", "The temporary output destination escaped its account folder.")
                self.transport.sftp.get(entry["remote_path"], str(temporary))
                if verify_context:
                    verify_context()
                if temporary.stat().st_size != entry["size"] or digest(temporary) != entry["sha256"]:
                    raise AppError("output_integrity", "Output verification failed. The VM and original output were preserved.")
                with temporary.open("rb+") as saved:
                    os.fsync(saved.fileno())
                try:
                    if os.name == "nt":
                        temporary.rename(target)  # Windows rename refuses an existing destination.
                    else:
                        os.link(temporary, target)
                        temporary.unlink()
                except FileExistsError:
                    raise AppError("output_conflict", "A local output appeared during delivery. Nothing was overwritten; the VM remains active.") from None
            acknowledged.append({key: entry[key] for key in ("relative_path", "size", "sha256")})
        result = {"manifest_id": manifest["manifest_id"], "files": acknowledged}
        request_body("/outputs/ack", result)
        return result

    def reconcile_output_mode(self, result):
        transition = self.session.get("output_transition")
        mode = result.get("output_mode")
        if transition or mode is not None:
            if any(result.get(key) != self.session.get(key) for key in ("account_id", "session_id", "nonce")) or mode not in {"pc", "drive"} or transition and mode not in {transition["previous"], transition["target"]}:
                raise AppError("output_mode_unverified", "The current output destination could not be verified. Reconnect the owned VM before operating it.")
            self.session["output_mode"] = mode
            self.store.set("active_session", self.session)

    def restart(self, params):
        remote = self.require_remote()
        target = params.get("output_mode", self.session["output_mode"])
        if target not in {"pc", "drive"}:
            raise AppError("invalid_output_mode", "Choose PC or Drive.")
        if self.session.get("output_transition"):
            raise AppError("output_mode_unverified", "Reconnect to verify the unfinished output destination change before restarting.")
        previous = self.session["output_mode"]
        switching = target != previous
        try:
            if switching:
                self.phase("starting", "outputs")
                manifest = remote.call("/outputs/prepare", {})
                self.session["pending_outputs"] = manifest.get("pending_outputs")
                acknowledgement = remote.call("/outputs/ack", self.copy_outputs(manifest))
                if acknowledgement.get("verified") is not True:
                    raise AppError("output_delivery_unverified", "Output delivery was not confirmed. The previous destination and VM were preserved.")
                self.phase("starting", "runtime_preservation")
                if remote.call("/runtime/save", {}, timeout=1200).get("verified") is not True:
                    raise AppError("runtime_unverified", "Runtime preservation was not verified. The previous destination and VM were preserved.")
                self.session["output_transition"] = {"previous": previous, "target": target}
                self.store.set("active_session", self.session)
            self.phase("starting", "services")
            self.session["services_started"] = False
            self.store.set("active_session", self.session)
            result = self.bootstrap("restart", output_mode=target, previous_output_mode=previous)
            self.reconcile_output_mode(result)
            if switching and self.session["output_mode"] != target:
                raise AppError("output_mode_unverified", "The new output destination was not confirmed. Reconnect the owned VM.")
            self.ready(result)
            self.session["services_started"] = True
            if switching:
                self.store.update(self.session["account_id"], output_mode=target)
                self.session.pop("output_transition", None)
            self.store.set("active_session", self.session)
            return result
        except Exception:
            resume_previous = not self.session.get("output_transition")
            if self.session.get("output_transition"):
                try:
                    self.reconcile_output_mode(self.bootstrap("health"))
                    resume_previous = self.session["output_mode"] == previous
                except Exception:
                    pass
            if resume_previous:
                try:
                    remote.call("/outputs/resume", {})
                except Exception:
                    pass
            raise

    def stop(self):
        if self.session.get("prepared") is not True and self.session.get("services_started") is not True and self.session.get("endpoint"):
            return self.stop_unprepared()
        remote = self.require_remote()
        self.phase("stopping", "outputs")
        try:
            manifest = remote.call("/outputs/prepare", {})
            self.session["pending_outputs"] = manifest.get("pending_outputs")
            ack = self.copy_outputs(manifest)
            confirmation = remote.call("/outputs/ack", ack)
            if confirmation.get("verified") is not True:
                raise AppError("output_delivery_unverified", "Output delivery was not confirmed. The VM remains active.")
            self.phase("stopping", "runtime_preservation")
            preserved = remote.call("/runtime/save", {}, timeout=1200)
            if preserved.get("verified") is not True:
                raise AppError("runtime_unverified", "Runtime preservation was not verified. The VM remains active.")
            gate = self.bootstrap("stop")
            if gate.get("safe_to_stop") is not True or gate.get("runtime_verified") is not True or gate.get("pending_outputs") != 0:
                raise AppError("shutdown_gate_failed", "The runtime could not confirm safe shutdown. The VM remains active.")
            if self.store.account(self.session["account_id"])["subject"] != self.session["subject"]:
                raise AppError("account_mismatch", "Session account identity changed. No VM was stopped.")
            self.colab.stop(self.session["endpoint"])
            self.transport.close()
            self.colab.close()
            self.transport = self.colab = self.companion = None
            self.reset_metrics()
            self.session = self.idle()
            self.store.set("active_session", self.session)
            self.emit("state", {"session": self.public_session()})
            return self.public_session()
        except Exception:
            try:
                remote.call("/outputs/resume", {})
            except Exception:
                pass
            raise

    def stop_unprepared(self):
        from controller.google import Colab
        from controller.transport import Transport
        if self.session.get("prepared") is True or self.session.get("services_started") is True or not self.session.get("notebook_id") or not self.session.get("ssh_fingerprint"):
            raise AppError("unprepared_shutdown_unverified", "An incomplete allocation must have verified ownership and SSH identity before safe shutdown.")
        account_id = self.session["account_id"]
        if self.store.account(account_id)["subject"] != self.session.get("subject"):
            raise AppError("account_mismatch", "Session account identity changed. No VM was stopped.")
        self.phase("stopping", "ownership")
        if not self.colab:
            self.colab = (self.colab_factory or Colab)(self.google(), account_id, self.emit)
            self.colab.session_id = self.session["session_id"]
        assignment = self.colab.recover(uuid.UUID(self.session["notebook_id"]), self.session["gpu"])
        if assignment.endpoint != self.session["endpoint"]:
            raise AppError("owned_vm_missing", "The exact owned notebook endpoint changed. No VM was stopped.")
        if not self.transport:
            self.transport = (self.transport_factory or Transport)(self.store, account_id, assignment.runtime_proxy_info, {}, self.session["ssh_fingerprint"])
        if self.transport.fingerprint != self.session["ssh_fingerprint"]:
            raise AppError("host_changed", "The owned VM SSH identity changed. No VM was stopped.")
        # Only the failed, unprepared runtime receives the reviewed shutdown gate sources.
        self.transport.upload_remote(resources() / "remote")
        gate = self.bootstrap("stop_unprepared")
        if any(gate.get(key) != self.session.get(key) for key in ("account_id", "session_id", "nonce")) or gate.get("safe_to_stop") is not True or gate.get("unprepared") is not True or gate.get("installed") is not False or gate.get("runtime_required") is not False or gate.get("pending_outputs") != 0:
            raise AppError("unprepared_shutdown_unverified", "The incomplete runtime could not prove safe shutdown. Its VM and original Drive artifact were preserved.")
        if self.store.account(account_id)["subject"] != self.session["subject"]:
            raise AppError("account_mismatch", "Session account identity changed. No VM was stopped.")
        self.colab.stop(self.session["endpoint"])
        self.transport.close(); self.colab.close()
        self.transport = self.colab = self.companion = None
        self.reset_metrics()
        self.session = self.idle()
        self.store.set("active_session", self.session)
        self.emit("state", {"session": self.public_session()})
        return self.public_session()

    def preferences(self, params):
        values = {**PREFERENCES, **self.store.get("preferences", {})}
        for name, value in params.items():
            if name == "language" and value in ("pt-BR", "en") or name == "auto_open" and type(value) is bool:
                values[name] = value
            elif name in ("idle_minutes", "balance_alert") and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= (1440 if name == "idle_minutes" else 100000):
                values[name] = value
            else:
                raise AppError("invalid_preferences", "Invalid preference value.")
        self.store.set("preferences", values)
        return values

    def action(self, action, params):
        if self.benchmark and action not in {"app.snapshot", "app.preferences", "app.diagnostics", "history.list", "history.export"}:
            raise AppError("benchmark_readonly", "Benchmark mode only permits local snapshots, preferences, history and diagnostics.")
        if action == "app.snapshot":
            return self.snapshot()
        if action == "account.usage":
            return self.read_usage(self.account_id(params), force=params.get("force") is True)
        if action in {"history.list", "history.export"}:
            return self.mutate(action, params)
        if action == "session.cancel":
            self.cancelled.set()
            return {"cancel_requested": True, "vm_preserved_until_ownership_verified": True}
        if action == "account.submitCode" and params.get("purpose") == "mount_drive":
            if "code" in params or params.get("confirmed") is not True or params.get("account_id") != self.session.get("account_id") or params.get("session_id") != self.session.get("session_id") or not self.colab:
                raise AppError("auth_not_pending", "Confirm the Drive authorization for the current owned session.")
            return self.colab.confirm_mount()
        if not self.operation.acquire(blocking=False):
            raise AppError("operation_busy", "Wait for the current operation to finish.")
        try:
            return self.mutate(action, params)
        finally:
            self.operation.release()

    def mutate(self, action, p):
        if action == "app.preferences":
            return self.preferences(p)
        if action == "history.list":
            return self.store.history_page(p.get("offset", 0), p.get("limit", 50), p.get("account_id"), p.get("session_id"), p.get("operation"))
        if action == "history.export":
            return {"schema_version": 1, "exported_at": time.time(), **self.store.history_page(0, 2000, p.get("account_id"), p.get("session_id"), p.get("operation"), export=True)}
        if action == "app.diagnostics":
            return {"version": "0.1.2-dev", "session": self.public_session(), "capabilities": self.snapshot()["capabilities"], "metrics": public(self.remote_snapshot.get("metrics")), "history": self.store.history()}
        if action == "account.importLegacy":
            from controller.import_legacy import import_legacy
            if p.get("confirmed") is not True:
                raise AppError("import_confirmation_required", "Explicitly select the private legacy credential import. Originals remain intact.")
            return import_legacy(self.store, self.google(), source_root=p.get("source_root"), windows_root=p.get("windows_root"), all_accounts=p.get("all_accounts", True))
        if action == "account.connect":
            account_id = p.get("account_id") or self.store.add(p.get("label", "Google account"))
            try:
                return self.google().begin(account_id, p.get("purpose", "colab"), p.get("client_config_path"))
            except Exception as error:
                error._receipt_account_id = account_id
                raise
        if action == "account.submitCode":
            if "code" in p:
                raise AppError("native_secret_required", "Enter the authorization code in the native protected dialog.")
            return self.google().submit(self.account_id(p))
        if action in ("account.select", "account.remove", "account.update"):
            account_id = self.account_id(p)
            if self.session.get("endpoint") and (action != "account.update" or any(k in p for k in ("gpu", "output_mode"))):
                raise AppError("owned_session_active", "Safely stop the owned VM before changing its account or session settings.")
            if action == "account.select":
                self.store.set("selected_account_id", account_id)
            elif action == "account.remove":
                self.store.remove(account_id)
                self.usage_cache.pop(account_id, None)
                self.usage_locks.pop(account_id, None)
            else:
                self.store.update(account_id, **{k: p[k] for k in ("label", "gpu", "output_mode") if k in p})
            return {"accounts": self.store.accounts(), "selected_account_id": self.store.get("selected_account_id")}
        if action == "account.credentials":
            account_id, provider = self.account_id(p), p.get("provider")
            if provider not in ("Hugging Face", "Civitai"):
                raise AppError("provider_invalid", "Choose Hugging Face or Civitai.")
            if "token" in p:
                raise AppError("native_secret_required", "Enter service credentials in the native protected dialog.")
            if p.get("remove"):
                self.store.secret(account_id, provider, delete=True)
                if self.companion and account_id == self.session.get("account_id"):
                    self.push_model_credentials()
                return {"provider": provider, "configured": False}
            token = secret_prompt(provider, self.store.get("preferences", PREFERENCES).get("language", "pt-BR"))
            if not token or len(token) > 1024 or "\r" in token or "\n" in token:
                raise AppError("credential_invalid", "Enter a valid service credential.")
            self.store.secret(account_id, provider, token)
            if self.companion and account_id == self.session.get("account_id"):
                self.push_model_credentials()
            return {"provider": provider, "configured": True}
        if action == "session.start":
            return self.start(p)
        if action == "session.reconnect":
            return self.reconnect()
        if action == "session.stop":
            if "session_id" in p and (p["session_id"] != self.session.get("session_id") or p.get("account_id") != self.session.get("account_id")):
                raise AppError("session_changed", "The session changed while metrics were being read. No VM was stopped.")
            return self.stop()
        if action == "session.open":
            self.require_remote()
            if not self.session.get("comfy_url"):
                raise AppError("cpu_session", "CPU download sessions have no ComfyUI frontend.")
            return {"url": self.session["comfy_url"]}
        if action in ("session.restart", "session.freeMemory", "session.runtimeSave"):
            self.require_remote()
            if action == "session.restart":
                return self.restart(p)
            if action == "session.runtimeSave":
                result = self.companion.call("/runtime/save", {}, timeout=1200)
                if result.get("verified") is not True:
                    raise AppError("runtime_unverified", "Runtime artifact was not verified. The VM remains active.")
                return result
            return self.bootstrap("freeMemory")
        if action in ("drive.copy", "drive.share"):
            from controller.drive import transfer
            return transfer(self.google(), p, action.split(".")[1], self.emit)
        if action.startswith("training."):
            from controller.training import handle
            return handle(self, action, p)
        if action not in {"model.list", "download.list", "download.add", "download.cancel", "download.resume", "cache.prepare", "cache.cancel", "cache.clear", "node.list", "node.catalog", "node.versions", "node.install", "node.update", "node.remove", "workflow.list", "workflow.check", "workflow.import", "legacy.list", "legacy.import"}:
            raise AppError("unknown_action", "This controller action is not supported.")
        remote = self.require_remote()
        if action in {"legacy.list", "legacy.import"}:
            kind, category = p.get("kind"), p.get("category")
            if not isinstance(kind, str) or kind not in {"model", "workflow"} or kind == "model" and (not isinstance(category, str) or category not in MODEL_CATEGORIES) or kind == "workflow" and category is not None:
                raise AppError("legacy_selection_invalid", "Choose a supported legacy model category or workflow.")
            selected = {"kind": kind, **({"category": category} if category is not None else {})}
            if action == "legacy.list":
                return remote.call("/legacy/files" + page_query(p) + "&" + urlencode(selected))
            relative = p.get("relative_path")
            if not isinstance(relative, str) or not relative or len(relative.encode("utf-8")) > 1024 or PurePosixPath(relative).is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")) or any(c in relative for c in "\\:") or any(ord(c) < 32 for c in relative):
                raise AppError("legacy_selection_invalid", "Choose a listed file inside the fixed legacy model or workflow root.")
            selected["relative_path"] = relative
            if "source_category" in p:
                source_category = p["source_category"]
                if kind != "model" or not isinstance(source_category, str) or source_category != category and not (category == "llm_gguf" and source_category == "LLM"):
                    raise AppError("legacy_selection_invalid", "Choose a listed file from the matching legacy model category.")
                selected["source_category"] = source_category
            if "destination_name" in p:
                name = p["destination_name"]
                if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 255 or name in {".", ".."} or any(c in name for c in "/\\:") or any(ord(c) < 32 for c in name):
                    raise AppError("legacy_selection_invalid", "Choose a filename without directories for the imported copy.")
                selected["destination_name"] = name
            return remote.call("/legacy/import", selected, timeout=1200)
        if action == "model.list":
            result = remote.call("/models" + page_query(p))
            self.remote_snapshot["models"] = result.get("items", result.get("models", []))
            return result
        if action == "download.list":
            result = remote.call("/downloads" + page_query(p))
            self.remote_snapshot["downloads"] = result
            return {**result, "items": [{**item, "name": item.get("name", item.get("filename", item.get("id")))} for item in result.get("items", [])]}
        if action == "workflow.list":
            return remote.call("/workflows" + page_query(p))
        if action == "node.catalog":
            query = p.get("query", "")
            if not isinstance(query, str) or len(query) > 200 or any(ord(c) < 32 for c in query):
                raise AppError("invalid_node_query", "Search custom nodes with up to 200 characters.")
            return remote.call("/nodes/catalog" + page_query(p) + "&" + urlencode({"query": query}))
        if action == "node.versions":
            node_id = p.get("id")
            if not isinstance(node_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", node_id):
                raise AppError("invalid_node", "Choose a registered node identifier.")
            return remote.call("/nodes/versions" + page_query(p) + "&" + urlencode({"id": node_id}))
        if action == "download.add":
            payload = download_payload(p)
            payload["tokens"] = self.model_credentials()
            return remote.call("/downloads", payload)
        if action in ("download.cancel", "download.resume"):
            job_id = p.get("job_id")
            if not isinstance(job_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job_id):
                raise AppError("download_missing", "Select a download first.")
            provider_tokens = self.model_credentials()
            return remote.call(f"/downloads/{job_id}/" + action.split(".")[1], {"tokens": provider_tokens} if action.endswith("resume") else {})
        if action in ("cache.prepare", "cache.cancel", "cache.clear"):
            return remote.call("/cache/" + action.split(".")[1], p)
        if action == "node.list":
            result = remote.call("/nodes" + page_query(p))
            base = [item for item in self.remote_snapshot.get("packages", []) if item.get("id") in {"core", "frontend", "manager", "mcp"}]
            self.remote_snapshot["packages"] = base + packages(result.get("items", result.get("packages", [])))
            result["packages"] = self.remote_snapshot["packages"]
            return result
        if action in ("node.install", "node.update", "node.remove"):
            if p.get("package_id") in {"core", "frontend", "manager", "mcp"}:
                raise AppError("protected_package", "Core packages are pinned to the app runtime. They cannot be changed as custom nodes.")
            return remote.call("/nodes/" + action.split(".")[1], {"id": p.get("package_id"), "version": p.get("version"), "expected_version": p.get("expected_version")}, timeout=1200)
        if action in ("workflow.check", "workflow.import"):
            if action == "workflow.check" and "relative_path" in p:
                relative = p["relative_path"]
                if not isinstance(relative, str) or not relative.casefold().endswith(".json") or len(relative.encode("utf-8")) > 1024 or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts or any(c in relative for c in "\\\r\n:"):
                    raise AppError("workflow_invalid", "Choose a saved workflow inside the new Drive workflow root.")
                return remote.call("/workflows/check", {"relative_path": relative})
            path = Path(p.get("path", ""))
            if not path.is_file() or path.suffix.casefold() != ".json" or path.stat().st_size > 16 * 1024 * 1024:
                raise AppError("workflow_invalid", "Choose a workflow JSON file up to 16 MiB.")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise AppError("workflow_invalid", "Workflow JSON must be an object.")
            route = "/workflows/" + action.split(".")[1]
            payload = {"workflow": data, "filename": path.name}
            request_body(route, payload)
            return remote.call(route, payload)
        raise AppError("unknown_action", "This controller action is not supported.")

    def dispatch(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        began = time.monotonic()
        action = request.get("action", "invalid") if isinstance(request, dict) else "invalid"
        params = request.get("params", {}) if isinstance(request, dict) and isinstance(request.get("params", {}), dict) else {}
        account_id = params.get("account_id") or (self.session.get("account_id") if isinstance(action, str) and action.startswith(("training.", "session.", "download.", "cache.", "node.", "workflow.", "legacy.")) else None) or self.store.get("selected_account_id")
        if action in ("drive.copy", "drive.share"):
            account_id = params.get("source_account_id")
        try:
            account_id = self.store.account(account_id)["id"]
        except Exception:
            account_id = None
        session_id = self.session.get("session_id")

        def record(result, error_code=None):
            if error_code == "benchmark_readonly" or not isinstance(action, str) or action not in RECORDED_ACTIONS:
                return
            try:
                self.store.event(action, account_id, result, session_id=session_id or self.session.get("session_id"), duration_seconds=time.monotonic()-began, error_code=error_code)
            except Exception:
                # A successful operation must not be retried because the optional audit receipt failed.
                self.emit("state", {"history_error_code": "history_unavailable"})
        try:
            if not isinstance(request, dict) or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or not isinstance(request.get("action"), str) or not isinstance(request.get("params", {}), dict):
                raise AppError("invalid_request", "Send a valid JSONL request with id, action and object params.")
            result = self.action(request["action"], request.get("params", {}))
            if action == "account.connect" and isinstance(result, dict):
                account_id = result.get("account_id", account_id)
            record("complete")
            return {"id": request_id, "ok": True, "result": public(result)}
        except Exception as error:
            code = error.code if isinstance(error, AppError) else "operation_failed"
            message = error.message if isinstance(error, AppError) else "The operation could not complete. Check setup and reconnect; recorded VMs and original data were preserved."
            if action == "account.connect":
                try:
                    account_id = self.store.account(getattr(error, "_receipt_account_id", None))["id"]
                except Exception:
                    account_id = None
            if code not in {"benchmark_readonly", "session_changed"} and isinstance(action, str) and action.startswith("session.") and action != "session.cancel" and self.session.get("notebook_id"):
                self.session.update(state="error", error=message, error_code=code, comfy_url=None, mcp_url=None)
                self.store.set("active_session", self.session)
                self.emit("state", {"session": self.public_session()})
            record("error", code)
            return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}

    def close(self):
        # Closing the controller disconnects; it never implicitly destroys a paid VM.
        self.closed.set()
        self.cancelled.set()
        if self.transport:
            self.transport.close()
        if self.colab:
            self.colab.close()

    def poll(self):
        """Refresh metrics without allocating or reauthenticating a recorded VM."""
        local_metrics = self.local_monitor.sample()
        if self.benchmark or not self.companion or self.session["state"] != "ready" or self.operation.locked():
            self.emit("state", {"local_metrics": local_metrics})
            return
        companion, colab = self.companion, self.colab
        identity = tuple(self.session.get(k) for k in ("account_id", "session_id", "nonce", "endpoint"))

        def current():
            return not self.closed.is_set() and self.companion is companion and self.colab is colab and self.session["state"] == "ready" and tuple(self.session.get(k) for k in ("account_id", "session_id", "nonce", "endpoint")) == identity

        try:
            snapshot = companion.call("/snapshot", timeout=15)
            if not current() or self.operation.locked():
                return
            account_metrics = dict(getattr(self, "account_metrics", {}))
            last_balance, balance_alert = self.last_balance, False
            if colab and time.monotonic() - last_balance >= 60:
                last_balance = time.monotonic()
                try:
                    usage = self.read_usage(identity[0])
                    account_metrics = {key: usage[key] for key in ("balance", "rate")}
                    balance_alert = usage["balance"] is not None and usage["balance"] < self.store.get("preferences", PREFERENCES).get("balance_alert", 20)
                except Exception:
                    account_metrics = {"balance": None, "rate": None}
                if not current() or self.operation.locked():
                    return
            if not self.operation.acquire(blocking=False):
                return
            stop_idle = False
            try:
                if not current():
                    return
                delivery = self.output_delivery
                queue = snapshot.get("queue")
                queue_idle = isinstance(queue, dict) and all(k in queue and isinstance(queue[k], (int, list)) and not isinstance(queue[k], bool) and not queue[k] for k in ("running", "pending"))
                if self.session.get("output_mode") == "pc" and queue_idle and snapshot.get("training", {}).get("busy") is False and snapshot.get("training", {}).get("preserved") is True and snapshot.get("pending_outputs") != 0:
                    def verify_delivery():
                        if not current() or self.session.get("output_mode") != "pc":
                            raise AppError("session_changed", "The session changed during output delivery. Original VM files were preserved.")
                    try:
                        manifest = companion.call("/outputs/delivery", {})
                        verify_delivery()
                        if manifest.get("delivery") is not True or manifest.get("output_mode") != "pc" or not isinstance(manifest.get("files"), list) or len(manifest["files"]) > 100:
                            raise AppError("output_manifest_invalid", "The continuous delivery batch could not be verified. Original VM files were preserved.")
                        if manifest["files"]:
                            acknowledgement = {**self.copy_outputs(manifest, verify_delivery), "delivery": True}
                            verify_delivery()
                            confirmed = companion.call("/outputs/ack", acknowledgement)
                            verify_delivery()
                            if confirmed.get("verified") is not True:
                                raise AppError("output_delivery_unverified", "Continuous output delivery was not confirmed. Original VM files were preserved.")
                            delivery = {"status": "complete", "delivered": len(manifest["files"]), "last_delivery_at": time.time(), "error_code": None}
                            snapshot["pending_outputs"] = confirmed.get("pending_outputs")
                        else:
                            snapshot["pending_outputs"] = manifest.get("pending_outputs")
                    except Exception as error:
                        if not current():
                            return
                        delivery = {"status": "error", "error_code": error.code if isinstance(error, AppError) else "output_delivery_failed"}
                if not current():
                    return
                snapshot["metrics"] = {**(snapshot.get("metrics") or {}), **account_metrics}
                self.remote_snapshot = snapshot
                self.output_delivery = delivery
                self.account_metrics, self.last_balance = account_metrics, last_balance
                self.session["pending_outputs"] = snapshot.get("pending_outputs")
                if isinstance(snapshot.get("hardware"), dict):
                    self.session["hardware"] = snapshot["hardware"]
                if balance_alert:
                    self.emit("progress", {"phase": "balance_alert", "balance": account_metrics["balance"], "message": "The verified account balance is below your alert threshold."})
                self.emit("state", {"session": self.public_session(), "metrics": public(snapshot.get("metrics")), "local_metrics": local_metrics, "output_delivery": public(delivery)})
                self.emit("download", {"downloads": public(snapshot.get("downloads", []))})
                queue, downloads = snapshot.get("queue"), snapshot.get("downloads", {})
                # A displayed download page cannot prove that all remote jobs are idle.
                idle = isinstance(queue, dict) and all(k in queue and isinstance(queue[k], (int, list)) and not isinstance(queue[k], bool) for k in ("running", "pending")) and not queue["running"] and not queue["pending"] and isinstance(downloads, dict) and downloads.get("busy") is False and snapshot.get("cache", {}).get("busy") is False and snapshot.get("training", {}).get("busy") is False and snapshot.get("training", {}).get("preserved") is True
                minutes = self.store.get("preferences", PREFERENCES).get("idle_minutes", 0)
                if not minutes or not idle:
                    self.idle_since = None
                elif self.idle_since is None:
                    self.idle_since = time.monotonic()
                else:
                    stop_idle = time.monotonic() - self.idle_since >= minutes * 60
            finally:
                self.operation.release()
            if stop_idle and current():
                self.emit("progress", {"phase": "idle_shutdown", "message": "Owned VM has remained idle; running the verified shutdown gates."})
                self.dispatch({"id": "idle-stop", "action": "session.stop", "params": {"account_id": identity[0], "session_id": identity[1]}})
        except Exception:
            if not self.operation.acquire(blocking=False):
                return
            try:
                if current():
                    self.remote_snapshot = {}
                    self.idle_since = None
                    self.emit("state", {"session": self.public_session(), "metrics": None, "local_metrics": local_metrics, "error": "Remote metrics unavailable; automatic stop suspended."})
            finally:
                self.operation.release()
