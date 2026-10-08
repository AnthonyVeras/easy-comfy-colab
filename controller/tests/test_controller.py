from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from controller.core import Companion, Controller, download_payload, public, resources, safe_output
from controller.state import AppError, InstanceLock, Store, protect


def fake_crypt(data, decrypt=False):
    return data[7:] if decrypt else b"sealed:" + data


class TemporaryStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state", crypt=fake_crypt)
        self.account_id = self.store.add("Conta de João")
        self.identity = {"sub": "subject-a", "email": "a@example.invalid", "email_verified": True}
        self.store.bind(self.account_id, self.identity)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class StateTests(TemporaryStore):
    def test_identity_cannot_be_rebound(self):
        with self.assertRaisesRegex(AppError, "another Google"):
            self.store.bind(self.account_id, {**self.identity, "sub": "subject-b"})
        self.assertEqual(self.store.account(self.account_id)["subject"], "subject-a")

    def test_duplicate_subject_cannot_take_another_profile(self):
        other = self.store.add("Outra")
        with self.assertRaises(AppError) as error:
            self.store.bind(other, self.identity)
        self.assertEqual(error.exception.code, "account_duplicate")
        self.assertIsNone(self.store.account(other)["subject"])

    def test_unverified_email_rejected(self):
        with self.assertRaises(AppError):
            self.store.bind(self.account_id, {**self.identity, "email_verified": False})

    def test_credential_encryption_failure_does_not_bind(self):
        other = self.store.add("Novo")
        self.store.crypt = Mock(side_effect=OSError("encryption unavailable"))
        with self.assertRaises(OSError):
            self.store.bind(other, {**self.identity, "sub": "new"}, credential=("google_colab", {"refresh_token": "synthetic"}))
        self.assertFalse(self.store.account(other)["connected"])
        self.assertIsNone(self.store.account(other)["subject"])

    def test_secret_storage_not_in_public_or_plain_blob(self):
        self.store.secret(self.account_id, "Hugging Face", "synthetic-token")
        self.assertEqual(self.store.secret(self.account_id, "Hugging Face"), "synthetic-token")
        self.assertNotIn("synthetic-token", json.dumps(self.store.accounts()))
        self.assertNotEqual(self.store.db.execute("SELECT value FROM secrets").fetchone()[0], b'"synthetic-token"')
        self.store.remove(self.account_id)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM secrets").fetchone()[0], 0)

    def test_account_settings_transaction_validates_all_first(self):
        with self.assertRaises(AppError):
            self.store.update(self.account_id, gpu="L4", output_mode="telegram")
        self.assertEqual(self.store.account(self.account_id)["gpu"], "T4")

    def test_concurrent_store_instances_preserve_account_additions(self):
        second = Store(self.root / "state", crypt=fake_crypt)
        try:
            second.add("Segundo")
            self.store.add("Terceiro")
            self.assertEqual(len(second.accounts()), 3)
        finally:
            second.close()

    def test_singleton_is_released_after_close(self):
        first = InstanceLock(self.root / "instance")
        try:
            with self.assertRaises(AppError) as error:
                InstanceLock(self.root / "instance")
            self.assertEqual(error.exception.code, "already_running")
        finally:
            first.close()
        InstanceLock(self.root / "instance").close()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_round_trip_synthetic_only(self):
        value = b"synthetic-test-secret"
        sealed = protect(value)
        self.assertNotIn(value, sealed)
        self.assertEqual(protect(sealed, decrypt=True), value)


class ValidationTests(unittest.TestCase):
    def test_encoded_secret_url_keys_never_cross_bridge(self):
        for key in ("token", "%74oken", "%2574oken", "%252574oken", "API_KEY", "access_token", "key"):
            with self.subTest(key=key), self.assertRaises(AppError) as error:
                download_payload({"urls": ["https://civitai.com/api/download/models/1?" + key + "=synthetic"]})
            self.assertEqual(error.exception.code, "url_secret")

    def test_model_url_provider_and_port_boundary(self):
        for url in ("https://huggingface.co.evil.invalid/file", "http://huggingface.co/a", "https://u:p@huggingface.co/a", "https://civitai.com:8443/a", "https://huggingface.co/a#token"):
            with self.subTest(url=url), self.assertRaises(AppError):
                download_payload({"urls": [url]})
        self.assertEqual(download_payload({"urls": ["https://huggingface.co/owner/repo/resolve/main/model.safetensors"]})["parallel"], 3)

    def test_parallel_is_bounded(self):
        for parallel in (True, 0, 7, "3"):
            with self.assertRaises(AppError):
                download_payload({"urls": ["https://huggingface.co/owner/repo/resolve/main/model.safetensors"], "parallel": parallel})

    def test_civitai_filename_normalizes_and_llm_maps_category(self):
        payload = download_payload({"urls": ["https://civitai.com/api/download/models/1 | test.gguf"], "category": "LLM"})
        self.assertEqual(payload["urls"], [{"url": "https://civitai.com/api/download/models/1", "filename": "test.gguf"}])
        self.assertEqual(payload["category"], "llm_gguf")

    def test_public_recursive_redaction(self):
        self.assertEqual(public({"tokens": "synthetic", "nested": [{"access_token": "synthetic", "count": 2}]}), {"nested": [{"count": 2}]})

    def test_output_escapes_windows_devices_and_other_sessions_rejected(self):
        root = Path(tempfile.gettempdir()) / "safe-output-test"
        for relative in ("../x", "C:/x", "/x", "dir\\x", "con.png", "x.", "dir/x ", None):
            entry = {"relative_path": relative, "remote_path": "/content/easy-comfy-colab-2/output-pc/session/x", "size": 0, "sha256": "a" * 64}
            with self.subTest(relative=relative), self.assertRaises(AppError):
                safe_output(root, entry, "session")
        entry = {"relative_path": "x.png", "remote_path": "/content/easy-comfy-colab-2/output-pc/other/x.png", "size": 0, "sha256": "a" * 64}
        with self.assertRaises(AppError):
            safe_output(root, entry, "session")

    def test_frozen_resources_resolve_meipass(self):
        with patch.object(sys, "_MEIPASS", "C:/frozen/resources", create=True):
            self.assertEqual(resources(), Path("C:/frozen/resources"))


class LifecycleTests(TemporaryStore):
    def setUp(self):
        super().setUp()
        self.auth = Mock()
        self.colab = Mock()
        self.transport = Mock(fingerprint="owned-fingerprint")
        self.assignment = SimpleNamespace(endpoint="exact-endpoint", runtime_proxy_info=SimpleNamespace(url="https://owned.googleusercontent.com", token="synthetic"))
        self.colab.allocate.return_value = self.assignment
        self.colab.recover.return_value = self.assignment
        self.controller = Controller(self.store, auth=self.auth, colab_factory=lambda *_: self.colab, transport_factory=lambda *_: self.transport)
        self.listeners = {name: Mock() for name in ("comfy", "companion", "mcp")}
        self.port_patch = patch("controller.transport.reserved_listeners", return_value=self.listeners)
        self.port_patch.start()
        self.result = {"ready": True, "remote_ports": {"comfy": None, "companion": 8190, "mcp": None}}

    def tearDown(self):
        self.controller.close()
        self.port_patch.stop()
        super().tearDown()

    def owned_session(self, output_mode="pc"):
        self.controller.session = {**self.controller.idle(), "state": "ready", "account_id": self.account_id, "session_id": "owned-session", "notebook_id": "00000000-0000-0000-0000-000000000001", "nonce": "owned-nonce", "endpoint": "exact-endpoint", "subject": "subject-a", "gpu": "T4", "output_mode": output_mode, "ssh_fingerprint": "owned-fingerprint", "drive_mounted": True, "prepared": True, "services_started": True}
        self.controller.transport = self.transport
        self.controller.colab = self.colab
        self.remote = Mock()
        self.controller.companion = self.remote
        return self.controller.session

    def test_preflight_port_failure_never_allocates(self):
        with patch("controller.transport.reserved_listeners", side_effect=AppError("port_busy", "Port busy")):
            result = self.controller.dispatch({"id": "1", "action": "session.start", "params": {"account_id": self.account_id}})
        self.assertFalse(result["ok"])
        self.colab.allocate.assert_not_called()

    def test_success_native_start_records_exact_identity_and_resources(self):
        self.transport.bootstrap.return_value = {"ok": True, "result": self.result}
        with patch.object(self.controller, "connect_companion"):
            result = self.controller.dispatch({"id": "1", "action": "session.start", "params": {"account_id": self.account_id, "gpu": "CPU"}})
        self.assertTrue(result["ok"])
        self.assertEqual(self.controller.session["endpoint"], "exact-endpoint")
        self.assertEqual(self.controller.session["ssh_fingerprint"], "owned-fingerprint")
        self.assertNotIn("endpoint", result["result"])
        self.assertNotIn("nonce", result["result"])
        self.assertEqual(self.transport.upload_remote.call_args.args[0], resources() / "remote")
        self.colab.stop.assert_not_called()

    def test_cancel_after_allocation_only_stops_exact_owned_endpoint(self):
        def allocate(*_):
            self.controller.cancelled.set()
            return self.assignment
        self.colab.allocate.side_effect = allocate
        result = self.controller.dispatch({"id": "1", "action": "session.start", "params": {"account_id": self.account_id}})
        self.assertEqual(result["error"]["code"], "cancelled")
        self.colab.stop.assert_called_once_with("exact-endpoint")
        self.assertEqual(self.controller.session["state"], "idle")

    def test_app_closure_during_allocation_preserves_vm_and_ownership(self):
        def allocate(*_):
            self.controller.close()
            return self.assignment
        self.colab.allocate.side_effect = allocate
        result = self.controller.dispatch({"id": "1", "action": "session.start", "params": {"account_id": self.account_id}})
        self.assertEqual(result["error"]["code"], "controller_closed")
        self.colab.stop.assert_not_called()
        self.assertEqual(self.store.get("active_session")["endpoint"], "exact-endpoint")

    def test_lost_allocation_response_records_notebook_for_read_only_recovery(self):
        self.colab.allocate.side_effect = TimeoutError("synthetic token=must-not-appear")
        result = self.controller.dispatch({"id": "1", "action": "session.start", "params": {"account_id": self.account_id}})
        self.assertFalse(result["ok"])
        self.assertNotIn("must-not-appear", json.dumps(result))
        self.assertTrue(self.store.get("active_session")["notebook_id"])
        self.colab.stop.assert_not_called()
        retry = self.controller.dispatch({"id": "2", "action": "session.start", "params": {"account_id": self.account_id}})
        self.assertEqual(retry["error"]["code"], "session_exists")

    def test_failure_after_prepare_never_bypasses_preservation(self):
        self.transport.bootstrap.side_effect = [{"ok": True, "result": {}}, {"ok": False, "error": {"message": "private"}}]
        result = self.controller.dispatch({"id": "1", "action": "session.start", "params": {"account_id": self.account_id}})
        self.assertFalse(result["ok"])
        self.assertTrue(self.controller.session["prepared"])
        self.colab.stop.assert_not_called()

    def test_reconnect_requires_same_notebook_endpoint_and_account(self):
        self.owned_session()
        self.colab.recover.return_value = SimpleNamespace(endpoint="unrelated-endpoint")
        result = self.controller.dispatch({"id": "1", "action": "session.reconnect", "params": {}})
        self.assertEqual(result["error"]["code"], "owned_vm_missing")
        self.transport.bootstrap.assert_not_called()
        self.colab.stop.assert_not_called()
        for listener in self.listeners.values():
            listener.close.assert_called()

    def test_reconnect_rejects_remote_nonce_before_mutation(self):
        session = self.owned_session()
        self.transport.bootstrap.return_value = {"ok": True, "result": {"account_id": self.account_id, "session_id": session["session_id"], "nonce": "other-nonce"}}
        result = self.controller.dispatch({"id": "1", "action": "session.reconnect", "params": {}})
        self.assertEqual(result["error"]["code"], "session_identity_mismatch")
        self.assertEqual(self.transport.bootstrap.call_args.args[0]["action"], "health")
        self.colab.stop.assert_not_called()

    def test_controller_restart_does_not_adopt_recorded_session(self):
        session = self.owned_session()
        self.store.set("active_session", session)
        restored = Controller(self.store, auth=self.auth, colab_factory=lambda *_: self.colab)
        self.assertEqual(restored.session["state"], "error")
        self.assertIsNone(restored.transport)
        self.colab.allocate.assert_not_called()
        self.colab.stop.assert_not_called()
        restored.close()

    def test_runtime_gate_failure_resumes_outputs_without_unassign(self):
        self.owned_session()
        self.remote.call.side_effect = [{"manifest_id": "manifest", "files": [], "pending_outputs": 0}, {"verified": True}, {"verified": False}, {"paused": False}]
        result = self.controller.dispatch({"id": "1", "action": "session.stop", "params": {}})
        self.assertEqual(result["error"]["code"], "runtime_unverified")
        self.colab.stop.assert_not_called()
        self.assertEqual(self.remote.call.call_args.args[0], "/outputs/resume")

    def test_output_ack_failure_never_saves_or_stops_vm(self):
        self.owned_session()
        self.remote.call.side_effect = [{"manifest_id": "manifest", "files": [], "pending_outputs": 0}, {"verified": False}, {"paused": False}]
        result = self.controller.dispatch({"id": "1", "action": "session.stop", "params": {}})
        self.assertEqual(result["error"]["code"], "output_delivery_unverified")
        self.colab.stop.assert_not_called()
        self.transport.bootstrap.assert_not_called()

    def test_shutdown_gate_rejects_unknown_pending_outputs(self):
        self.owned_session()
        self.remote.call.side_effect = [{"manifest_id": "manifest", "files": [], "pending_outputs": 0}, {"verified": True}, {"verified": True}, {"paused": False}]
        self.transport.bootstrap.return_value = {"ok": True, "result": {"safe_to_stop": True, "runtime_verified": True, "pending_outputs": None}}
        result = self.controller.dispatch({"id": "1", "action": "session.stop", "params": {}})
        self.assertEqual(result["error"]["code"], "shutdown_gate_failed")
        self.colab.stop.assert_not_called()

    def test_drive_outputs_stop_without_pc_copy(self):
        self.owned_session("drive")
        entry = {"relative_path": "image.png", "remote_path": "/content/drive/MyDrive/EasyComfyColab2/output/image.png", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
        self.remote.call.side_effect = [{"manifest_id": "manifest", "files": [entry], "pending_outputs": 0}, {"verified": True}, {"verified": True}]
        self.transport.bootstrap.return_value = {"ok": True, "result": {"safe_to_stop": True, "runtime_verified": True, "pending_outputs": 0}}
        with patch("controller.core.Path.home", return_value=self.root):
            result = self.controller.dispatch({"id": "1", "action": "session.stop", "params": {}})
        self.assertTrue(result["ok"])
        self.transport.sftp.get.assert_not_called()
        self.colab.stop.assert_called_once_with("exact-endpoint")
        self.assertFalse((self.root / "Easy Comfy Colab 2 Results").exists())

    def test_pc_copy_hash_verified_before_ack_and_stop(self):
        self.owned_session()
        content = b"synthetic-output"
        entry = {"relative_path": "folder/image.png", "remote_path": "/content/easy-comfy-colab-2/output-pc/owned-session/folder/image.png", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        self.transport.sftp.get.side_effect = lambda _, path: Path(path).write_bytes(content)
        self.remote.call.side_effect = [{"manifest_id": "manifest", "files": [entry], "pending_outputs": 1}, {"verified": True}, {"verified": True}]
        self.transport.bootstrap.return_value = {"ok": True, "result": {"safe_to_stop": True, "runtime_verified": True, "pending_outputs": 0}}
        with patch("controller.core.Path.home", return_value=self.root):
            result = self.controller.dispatch({"id": "1", "action": "session.stop", "params": {}})
        self.assertTrue(result["ok"])
        self.assertEqual((self.root / "Easy Comfy Colab 2 Results" / self.account_id / "output/folder/image.png").read_bytes(), content)
        ack = self.remote.call.call_args_list[1].args[1]
        self.assertNotIn("remote_path", ack["files"][0])
        self.colab.stop.assert_called_once_with("exact-endpoint")

    def test_corrupt_pc_output_prevents_ack_and_stop(self):
        self.owned_session()
        entry = {"relative_path": "image.png", "remote_path": "/content/easy-comfy-colab-2/output-pc/owned-session/image.png", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
        self.transport.sftp.get.side_effect = lambda _, path: Path(path).write_bytes(b"bad")
        self.remote.call.side_effect = [{"manifest_id": "manifest", "files": [entry], "pending_outputs": 1}, {"paused": False}]
        with patch("controller.core.Path.home", return_value=self.root):
            result = self.controller.dispatch({"id": "1", "action": "session.stop", "params": {}})
        self.assertEqual(result["error"]["code"], "output_integrity")
        self.assertEqual([call.args[0] for call in self.remote.call.call_args_list], ["/outputs/prepare", "/outputs/resume"])
        self.colab.stop.assert_not_called()

    def test_unknown_idle_queue_cannot_trigger_automatic_stop(self):
        self.owned_session()
        self.store.set("preferences", {"idle_minutes": 1})
        self.remote.call.return_value = {"queue": {}, "downloads": {"items": []}, "cache": {"busy": False}}
        self.controller.idle_since = time.monotonic() - 120
        with patch.object(self.controller, "dispatch") as dispatch:
            self.controller.poll()
        dispatch.assert_not_called()
        self.assertIsNone(self.controller.idle_since)

    def test_ui_cannot_supply_secrets(self):
        for action, params in (("account.credentials", {"provider": "Civitai", "token": "synthetic"}), ("account.submitCode", {"code": "synthetic"})):
            result = self.controller.dispatch({"id": "1", "action": action, "params": {"account_id": self.account_id, **params}})
            self.assertEqual(result["error"]["code"], "native_secret_required")

    def test_native_download_provider_keys_and_filename_match_remote(self):
        self.owned_session()
        self.store.secret(self.account_id, "Hugging Face", "synthetic-hf")
        result = self.controller.dispatch({"id": "1", "action": "download.add", "params": {"urls": ["https://civitai.com/api/download/models/1 | test.gguf"], "category": "LLM"}})
        self.assertTrue(result["ok"])
        payload = self.remote.call.call_args.args[1]
        self.assertEqual(payload["tokens"], {"huggingface": "synthetic-hf", "civitai": ""})
        self.assertEqual(payload["urls"][0]["filename"], "test.gguf")

    def test_model_pagination_node_manifest_and_protected_package(self):
        self.owned_session()
        self.remote.call.return_value = {"items": [{"name": "Modelo João", "size": 3}], "total": 60}
        self.controller.dispatch({"id": "1", "action": "model.list", "params": {"offset": 50, "limit": 50}})
        self.assertEqual(self.remote.call.call_args.args[0], "/models?offset=50&limit=50")
        self.assertEqual(self.controller.snapshot()["models"][0]["name"], "Modelo João")
        self.remote.call.return_value = {"items": [{"id": "registered-node", "ver": "1.2.3"}]}
        self.controller.dispatch({"id": "1", "action": "node.list", "params": {}})
        self.assertEqual(self.controller.snapshot()["packages"][0]["version"], "1.2.3")
        self.controller.dispatch({"id": "1", "action": "node.update", "params": {"package_id": "registered-node", "version": "1.2.4", "expected_version": "1.2.3"}})
        self.assertEqual(self.remote.call.call_args.args[1], {"id": "registered-node", "version": "1.2.4", "expected_version": "1.2.3"})
        result = self.controller.dispatch({"id": "1", "action": "node.remove", "params": {"package_id": "core"}})
        self.assertEqual(result["error"]["code"], "protected_package")

    def test_mcp_readiness_checks_official_server_workspace_and_measured_gpu(self):
        self.owned_session()
        http = Mock()
        stats = Mock()
        stats.json.return_value = {"devices": [{"type": "cuda", "name": "Measured GPU"}]}
        http.get.return_value = stats
        initialized, notification, tool = Mock(), Mock(), Mock()
        initialized.headers = {}; tool.headers = {}
        initialized.json.return_value = {"result": {"protocolVersion": "2025-03-26"}}
        tool.json.return_value = {"result": {"structuredContent": {"server": {"running": True}, "config": {"default_workspace": "/content/easy-comfy-colab-2/ComfyUI"}}}}
        http.post.side_effect = [initialized, notification, tool]
        with patch("requests.Session", return_value=http):
            self.controller.ready({"remote_ports": {"comfy": 8188, "mcp": 8189}})
        self.assertEqual(self.controller.public_session()["hardware"], "Measured GPU")
        self.assertEqual(http.post.call_args.kwargs["json"]["params"]["name"], "server_info")
        self.assertEqual(self.controller.session["state"], "ready")
        tool.json.return_value = {"result": {"structuredContent": {"server": {"running": True}, "config": {"default_workspace": "/content/legacy/ComfyUI"}}}}
        http.post.side_effect = [initialized, notification, tool]
        with patch("requests.Session", return_value=http), self.assertRaises(AppError) as error:
            self.controller.ready({"remote_ports": {"comfy": 8188, "mcp": 8189}})
        self.assertEqual(error.exception.code, "mcp_target_mismatch")
        self.colab.stop.assert_not_called()

    def test_recovery_of_confirmed_absent_notebook_clears_only_local_record(self):
        self.owned_session()
        self.colab.recover.side_effect = AppError("owned_allocation_absent", "No exact notebook allocation")
        result = self.controller.dispatch({"id": "1", "action": "session.reconnect", "params": {}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["state"], "idle")
        self.colab.stop.assert_not_called()

    def test_incomplete_owned_startup_recovers_mount_prepare_without_allocating(self):
        session = self.owned_session()
        session.update(drive_mounted=False, prepared=False, services_started=False, gpu="CPU")
        session.pop("ssh_fingerprint")
        self.controller.account_metrics = {"balance": 999, "rate": 3}
        self.controller.last_balance = 100
        identity = {key: session[key] for key in ("account_id", "session_id", "nonce")}
        self.transport.bootstrap.side_effect = [{"ok": True, "result": {}}, {"ok": True, "result": {**self.result, **identity}}]
        with patch.object(self.controller, "connect_companion"):
            result = self.controller.dispatch({"id": "recovery", "action": "session.reconnect", "params": {}})
        self.assertTrue(result["ok"])
        self.colab.allocate.assert_not_called()
        self.colab.stop.assert_not_called()
        self.colab.mount.assert_called_once_with(self.assignment, self.controller.cancelled)
        self.assertEqual(self.colab.session_id, session["session_id"])
        self.assertEqual([call.args[0]["action"] for call in self.transport.bootstrap.call_args_list], ["prepare", "start"])
        saved = self.store.get("active_session")
        self.assertEqual(saved["endpoint"], "exact-endpoint")
        self.assertTrue(saved["drive_mounted"])
        self.assertTrue(saved["prepared"])
        self.assertTrue(saved["services_started"])
        self.assertEqual(self.controller.account_metrics, {"balance": None, "rate": None})

    def test_import_requires_explicit_selection(self):
        result = self.controller.dispatch({"id": "1", "action": "account.importLegacy", "params": {}})
        self.assertEqual(result["error"]["code"], "import_confirmation_required")


class RemoteBoundaryTests(unittest.TestCase):
    def test_google_proxy_domains_and_ws_path_match_official_wire_contract(self):
        from controller.transport import proxy_ws_url
        from urllib.parse import urlsplit, parse_qs
        for origin in ("https://8080-owned.asia-east1-0.prod.colab.dev", "https://owned.googleusercontent.com/", "https://owned.googleusercontent.com:443"):
            result = urlsplit(proxy_ws_url(SimpleNamespace(url=origin, token="synthetic + / token")))
            self.assertEqual(result.scheme, "wss")
            self.assertEqual(result.path, "/colab/ssh")
            self.assertEqual(parse_qs(result.query), {"colab-runtime-proxy-token": ["synthetic + / token"]})

    def test_google_proxy_rejects_domain_suffix_impersonation_plaintext_and_userinfo(self):
        from controller.transport import proxy_ws_url
        for origin in ("https://prod.colab.dev.evil.invalid", "https://evilprod.colab.dev", "https://owned.googleusercontent.com.evil.invalid", "http://owned.prod.colab.dev", "wss://owned.prod.colab.dev", "https://u:p@owned.prod.colab.dev", "https://owned.prod.colab.dev:8443", "https://owned.prod.colab.dev/extra", "https://owned.prod.colab.dev?token=synthetic", "https://owned.prod.colab.dev#synthetic", "https://owned.prod.colab.dev:bad"):
            with self.subTest(origin=origin), self.assertRaises(AppError) as error:
                proxy_ws_url(SimpleNamespace(url=origin, token="synthetic"))
            self.assertEqual(error.exception.code, "proxy_invalid")
            self.assertNotIn("synthetic", error.exception.message)

    def test_companion_identity_is_checked_before_mutating_http(self):
        companion = Companion({"account_id": "a", "session_id": "s", "nonce": "n"}, "synthetic")
        response = Mock()
        response.json.return_value = {"account_id": "other", "session_id": "s", "nonce": "n"}
        companion.http = Mock()
        companion.http.get.return_value = response
        with self.assertRaises(AppError):
            companion.call("/runtime/save", {})
        companion.http.request.assert_not_called()
        self.assertEqual(companion.headers["X-Easy-Nonce"], "n")

    def test_websocket_adapter_retains_partial_binary_frame(self):
        import websocket
        from controller.transport import WebSocketSocket
        ws = Mock()
        ws.recv_data.side_effect = [(websocket.ABNF.OPCODE_BINARY, b"abcdef"), (websocket.ABNF.OPCODE_CLOSE, b"")]
        stream = WebSocketSocket(ws)
        self.assertEqual(stream.recv(2), b"ab")
        self.assertEqual(stream.recv(20), b"cdef")
        self.assertEqual(stream.recv(1), b"")
        self.assertEqual(stream.send(b"x"), 1)
        ws.send_binary.assert_called_once_with(b"x")

    def test_upload_includes_reviewed_manifest_excludes_tests_and_private_files(self):
        from controller.transport import Transport
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bootstrap.py").write_text("pass")
            (root / "runtime.json").write_text("{}")
            (root / "secret.json").write_text("synthetic")
            (root / "tests").mkdir()
            (root / "tests/test_ignore.py").write_text("pass")
            transport = object.__new__(Transport)
            transport.mkdir = Mock()
            transport.sftp = Mock()
            transport.upload_remote(root)
            targets = [call.args[1] for call in transport.sftp.put.call_args_list]
            self.assertEqual(targets, ["/content/easy-comfy-colab-2/remote/bootstrap.py", "/content/easy-comfy-colab-2/remote/runtime.json"])

    def test_colab_recovery_uses_exact_notebook_get_never_allocate_post(self):
        from controller.google import Colab
        colab = object.__new__(Colab)
        colab.client = Mock()
        assignment = SimpleNamespace(endpoint="owned")
        colab.client._get_assignment.return_value = assignment
        colab.client.list_assignments.return_value = [assignment, SimpleNamespace(endpoint="other")]
        import uuid
        notebook = uuid.UUID(int=1)
        self.assertIs(colab.recover(notebook, "CPU"), assignment)
        self.assertEqual(colab.client._get_assignment.call_args.args[0], notebook)
        colab.client.assign.assert_not_called()
        colab.client._post_assignment.assert_not_called()


class JsonlProcessTests(unittest.TestCase):
    def test_utf8_malformed_request_snapshot_and_eof(self):
        with tempfile.TemporaryDirectory() as temporary:
            request = {"id": "joão", "action": "account.connect", "params": {"label": "Conta de João"}}
            raw = b"invalid-json\n" + json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n" + json.dumps({"id": "snapshot", "action": "app.snapshot", "params": {}}).encode() + b"\n"
            env = {**os.environ, "LOCALAPPDATA": temporary, "ECC_DATA_DIR": str(Path(temporary) / "EasyComfyColab2"), "EASY_COMFY_OAUTH_CLIENT": str(Path(temporary) / "missing.json")}
            result = subprocess.run([sys.executable, "-m", "controller.main"], input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=Path(__file__).resolve().parents[2], timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            items = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
            self.assertTrue(any(item.get("id") == "joão" for item in items))
            self.assertTrue(any(item.get("error", {}).get("code") == "invalid_json" for item in items))
            self.assertTrue(any(item.get("id") == "snapshot" and item.get("ok") for item in items))
            store = Store(Path(temporary) / "EasyComfyColab2")
            try:
                self.assertEqual(store.accounts()[0]["label"], "Conta de João")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
