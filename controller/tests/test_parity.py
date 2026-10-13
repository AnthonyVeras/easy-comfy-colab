from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from controller.core import Controller
from controller.monitor import LocalMonitor
from controller.state import AppError, Store, secret_prompt


def crypt(data, decrypt=False):
    return data[7:] if decrypt else b"sealed:" + data


class MonitorTests(unittest.TestCase):
    def fixture(self):
        times = namedtuple("Times", "user system idle")
        backend = Mock()
        backend.cpu_times.side_effect = [times(10, 10, 80), times(12, 11, 87)]
        backend.virtual_memory.return_value = SimpleNamespace(total=1000, available=300)
        process = backend.Process.return_value
        process.cpu_percent.side_effect = [0, 3.5]
        process.memory_info.return_value = SimpleNamespace(rss=42)
        clock = Mock(return_value=0)
        return LocalMonitor(backend, clock), backend, clock

    def test_cpu_first_sample_unknown_then_counters_across_worker_threads(self):
        monitor, backend, clock = self.fixture()
        first = monitor.sample()
        self.assertIsNone(first["cpu_percent"])
        self.assertIsNone(first["controller_cpu_percent"])
        self.assertEqual(first["ram_used"], 700)
        self.assertEqual(first["controller_rss"], 42)
        clock.return_value = 6
        result = []
        thread = threading.Thread(target=lambda: result.append(monitor.sample()))
        thread.start(); thread.join()
        self.assertEqual(result[0]["cpu_percent"], 30)
        self.assertEqual(result[0]["controller_cpu_percent"], 3.5)
        backend.cpu_percent.assert_not_called()
        backend.Process.return_value.cpu_percent.assert_called_with(interval=None)

    def test_sampling_is_bounded_cached_values_cannot_be_mutated_by_consumer(self):
        monitor, backend, clock = self.fixture()
        value = monitor.sample()
        value["ram_used"] = 0
        clock.return_value = 4.999
        self.assertEqual(monitor.sample()["ram_used"], 700)
        self.assertEqual(backend.virtual_memory.call_count, 1)

    def test_unavailable_measurements_reset_baseline_instead_of_reusing_stale(self):
        monitor, backend, clock = self.fixture()
        monitor.sample()
        clock.return_value = 6
        backend.virtual_memory.side_effect = PermissionError("not allowed")
        self.assertTrue(all(value is None for value in monitor.sample().values()))
        self.assertIsNone(monitor.previous_cpu)
        self.assertFalse(monitor.primed)


@unittest.skipUnless(os.name == "nt", "Windows native credential dialog")
class NativeDialogTests(unittest.TestCase):
    def test_custom_native_text_uses_language_without_showing_a_real_dialog(self):
        prompt = Mock(return_value=1223)
        native = SimpleNamespace(credui=SimpleNamespace(CredUIPromptForCredentialsW=prompt))
        with patch("controller.state.ctypes.windll", native):
            for provider, language, expected in (("Hugging Face", "pt-BR", "Informe o token do Hugging Face"), ("Google authorization code", "pt-BR", "Informe o código de autorização do Google"), ("Google Drive authorization code", "pt-BR", "Informe o código de autorização do Google Drive"), ("Civitai", "en", "Enter Civitai")):
                with self.assertRaises(AppError) as error:
                    secret_prompt(provider, language)
                self.assertEqual(error.exception.code, "cancelled")
                info = prompt.call_args.args[0]._obj
                self.assertTrue(info.message.startswith(expected))
                self.assertEqual(info.caption, "Easy Comfy Colab 2")
                self.assertEqual(prompt.call_args.args[-1], 0x40000 | 0x2 | 0x80)


class HistoryAndDelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, crypt=crypt)
        self.account = self.store.add("Conta de teste")
        self.controller = Controller(self.store)

    def tearDown(self):
        self.controller.close()
        self.store.close()
        self.temp.cleanup()

    def request(self, action, params=None):
        return self.controller.dispatch({"id": "test", "action": action, "params": params or {}})

    def remote(self):
        remote = Mock()
        self.controller.require_remote = lambda: remote
        self.controller.session.update(account_id=self.account, session_id="owned-session")
        return remote

    def test_history_pagination_filters_retention_and_sanitized_details(self):
        with self.store.lock, self.store.db:
            self.store.db.executemany("INSERT INTO history(time,operation,account_id,session_id,result,details) VALUES(?,?,?,?,?,?)", [(index, "download.add", self.account, "session-old", "complete", "{}") for index in range(2000)])
        self.store.event("node.install", self.account, "error", session_id="session-new", duration_seconds=1.2345, error_code="node_failed", token="synthetic-secret", params={"url": "https://example.invalid?token=synthetic"}, message="sensitive")
        page = self.store.history_page(limit=2)
        self.assertEqual(page["total"], 2000)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["items"][0]["details"], {"duration_seconds": 1.234, "error_code": "node_failed"})
        self.assertEqual(page["items"][0]["session_id"], "session-new")
        filtered = self.store.history_page(account_id=self.account, session_id="session-new", operation="node.install")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(self.store.history_page(offset=2000)["items"], [])
        self.assertNotIn("synthetic", json.dumps(page))
        self.assertEqual(self.store.history_page(operation="node.install' OR 1=1 --")["total"], 0)
        with self.assertRaises(AppError):
            self.store.history_page(limit=101)

    def test_history_export_contains_all_bounded_filtered_receipts_instead_of_snapshot_page(self):
        other = self.store.add("Outra")
        for index in range(120):
            self.store.event("download.add", self.account, "complete", session_id="session", duration_seconds=index)
        self.store.event("node.install", other, "error", session_id="other-session", error_code="node_failed", url="synthetic-secret")
        self.controller.operation.acquire()
        try:
            export = self.request("history.export", {"account_id": self.account})
        finally:
            self.controller.operation.release()
        self.assertTrue(export["ok"])
        self.assertEqual(export["result"]["schema_version"], 1)
        self.assertEqual(export["result"]["total"], 120)
        self.assertEqual(len(export["result"]["items"]), 120)
        self.assertEqual(export["result"]["limit"], 2000)
        self.assertNotIn("synthetic-secret", json.dumps(export))
        self.assertEqual(len(self.controller.snapshot()["history"]), 50)

    def test_operation_receipts_include_account_session_duration_and_error_without_params(self):
        remote = self.remote()
        remote.call.return_value = {"ids": ["job"]}
        good = self.request("download.add", {"urls": ["https://huggingface.co/owner/repo/resolve/main/a.safetensors"]})
        self.assertTrue(good["ok"])
        bad = self.request("download.add", {"urls": ["https://civitai.com/api/download/models/1?token=synthetic-secret"]})
        self.assertEqual(bad["error"]["code"], "url_secret")
        items = self.request("history.list", {"account_id": self.account, "limit": 1})["result"]
        self.assertEqual(items["total"], 2)
        self.assertEqual(items["items"][0]["result"], "error")
        self.assertEqual(items["items"][0]["details"]["error_code"], "url_secret")
        self.assertEqual(items["items"][0]["session_id"], "owned-session")
        self.assertGreaterEqual(items["items"][0]["details"]["duration_seconds"], 0)
        self.assertNotIn("synthetic-secret", json.dumps(self.store.history()))
        self.assertNotIn("huggingface.co", json.dumps(self.store.history()))
        self.request("model.list")
        self.assertEqual(self.store.history_page()["total"], 2)

    def test_successful_shutdown_captures_previous_session_exactly_once(self):
        self.controller.session.update(account_id=self.account, session_id="finished-session")
        def stop():
            self.controller.session = self.controller.idle()
            return self.controller.public_session()
        with patch.object(self.controller, "stop", side_effect=stop):
            self.assertTrue(self.request("session.stop")["ok"])
        page = self.store.history_page(operation="session.stop")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["session_id"], "finished-session")
        self.assertEqual(page["items"][0]["account_id"], self.account)

    def test_history_count_and_page_share_read_snapshot_during_external_write(self):
        self.store.event("session.start", self.account, "complete", session_id="old-session")
        other = Store(self.root, crypt=crypt)
        wrote = False
        def interleave(statement):
            nonlocal wrote
            if statement.startswith("SELECT * FROM history") and not wrote:
                wrote = True
                other.event("session.stop", self.account, "complete", session_id="new-session")
        self.store.db.set_trace_callback(interleave)
        try:
            page = self.store.history_page()
            self.assertTrue(wrote)
            self.assertEqual(page["total"], 1)
            self.assertEqual([item["session_id"] for item in page["items"]], ["old-session"])
            self.assertEqual(self.store.history_page()["total"], 2)
        finally:
            self.store.db.set_trace_callback(None)
            other.close()

    def test_new_start_receipt_uses_new_session_and_selected_account(self):
        def start(_):
            self.controller.session.update(account_id=self.account, session_id="new-session")
            return self.controller.public_session()
        with patch.object(self.controller, "start", side_effect=start):
            self.assertTrue(self.request("session.start")["ok"])
        item = self.store.history()[0]
        self.assertEqual(item["account_id"], self.account)
        self.assertEqual(item["session_id"], "new-session")

    def test_unknown_malformed_actions_cannot_be_persisted_or_escape_dispatch(self):
        for action in ([], {}, "https://example.invalid?token=synthetic-secret"):
            result = self.request(action)
            self.assertFalse(result["ok"])
        self.assertEqual(self.store.history(), [])

    def test_failed_new_account_auth_is_attributed_to_created_account(self):
        self.controller.auth = Mock()
        self.controller.auth.begin.side_effect = AppError("oauth_client_missing", "Configure the private OAuth client.")
        result = self.request("account.connect", {"label": "Nova conta"})
        self.assertFalse(result["ok"])
        created = next(item["id"] for item in self.store.accounts() if item["id"] != self.account)
        self.assertEqual(self.store.history()[0]["account_id"], created)
        self.assertEqual(self.store.history()[0]["details"]["error_code"], "oauth_client_missing")

    def test_untrusted_account_identifiers_are_not_stored_in_receipts(self):
        result = self.request("account.update", {"account_id": "synthetic-secret", "label": "Changed"})
        self.assertFalse(result["ok"])
        self.assertIsNone(self.store.history()[0]["account_id"])
        self.assertNotIn("synthetic-secret", json.dumps(self.store.history()))

    def test_provider_and_oauth_native_inputs_receive_selected_language(self):
        from controller.google import GoogleAuth
        self.store.set("preferences", {"language": "en"})
        with patch("controller.core.secret_prompt", return_value="synthetic") as prompt:
            self.assertTrue(self.request("account.credentials", {"account_id": self.account, "provider": "Civitai"})["ok"])
            prompt.assert_called_once_with("Civitai", "en")
        auth = GoogleAuth(self.store, lambda *_: None)
        flow = Mock()
        auth.pending[self.account] = (flow, "state", "colab", None)
        with patch("controller.google.secret_prompt", return_value="synthetic-code") as prompt, patch.object(auth, "save"), patch("controller.google.NativeAuthorizedSession"):
            auth.submit(self.account)
            prompt.assert_called_once_with("Google authorization code", "en")

    def test_auto_stop_requires_global_download_idle_even_when_current_page_is_empty(self):
        self.controller.session.update(state="ready", account_id=self.account, session_id="owned-session")
        self.controller.companion = Mock()
        self.store.set("preferences", {"idle_minutes": 1})
        snapshot = {"queue": {"running": 0, "pending": 0}, "downloads": {"items": [], "total": 100}, "cache": {"busy": False}}
        self.controller.companion.call.return_value = snapshot
        with patch.object(self.controller, "dispatch") as dispatch:
            for busy in (None, True):
                snapshot["downloads"]["busy"] = busy
                self.controller.idle_since = time.monotonic() - 120
                self.controller.poll()
                self.assertIsNone(self.controller.idle_since)
            dispatch.assert_not_called()
            snapshot["downloads"]["busy"] = False
            self.controller.idle_since = time.monotonic() - 120
            self.controller.poll()
            dispatch.assert_called_once_with({"id": "idle-stop", "action": "session.stop", "params": {"account_id": self.account, "session_id": "owned-session"}})

    def test_old_snapshot_response_is_discarded_after_session_or_companion_changes(self):
        events = []
        self.controller.send = lambda event, data: events.append((event, data))
        for change in ("identity", "companion", "operation", "ready"):
            self.controller.session.update(state="ready", account_id=self.account, session_id="old-session", hardware=None)
            remote = Mock()
            self.controller.companion = remote
            self.controller.remote_snapshot = {"new-session": True}
            def delayed_reply(*_, **__):
                if change == "identity": self.controller.session["session_id"] = "new-session"
                if change == "companion": self.controller.companion = Mock()
                if change == "operation": self.controller.operation.acquire()
                if change == "ready": self.controller.session["state"] = "stopping"
                return {"metrics": {"ram_used": 999}, "hardware": {"devices": [{"name": "old-device"}]}, "downloads": {"items": [{"id": "old-job"}]}}
            remote.call.side_effect = delayed_reply
            self.controller.poll()
            self.assertEqual(self.controller.remote_snapshot, {"new-session": True})
            self.assertIsNone(self.controller.session["hardware"])
            self.assertEqual(events, [])
            if self.controller.operation.locked(): self.controller.operation.release()

    def test_slow_usage_result_and_failure_cannot_overwrite_new_account_stats(self):
        self.store.bind(self.account, {"sub": "usage-subject", "email": "usage@example.invalid", "email_verified": True})
        events = []
        self.controller.send = lambda event, data: events.append((event, data))
        for fail in (False, True):
            self.controller.usage_cache.clear()
            self.controller.session.update(state="ready", account_id=self.account, session_id="old-session", hardware=None)
            self.controller.companion = Mock()
            self.controller.companion.call.return_value = {"metrics": {"ram_used": 999}}
            self.controller.colab = Mock()
            self.controller.last_balance = 0
            def switch_account():
                self.controller.session["session_id"] = "new-session"
                self.controller.remote_snapshot = {"new-session": True}
                self.controller.account_metrics = {"balance": 123, "rate": 4}
                self.controller.last_balance = 456
                if fail: raise OSError("synthetic-old-response")
                return SimpleNamespace(paid_compute_units_balance=1, consumption_rate_hourly=99)
            self.controller.colab.client.get_consumption_user_info.side_effect = switch_account
            self.controller.poll()
            self.assertEqual(self.controller.remote_snapshot, {"new-session": True})
            self.assertEqual(self.controller.account_metrics, {"balance": 123, "rate": 4})
            self.assertEqual(self.controller.last_balance, 456)
            self.assertEqual(events, [])

    def test_stale_network_failure_cannot_clear_new_session_snapshot(self):
        events = []
        self.controller.send = lambda event, data: events.append((event, data))
        self.controller.session.update(state="ready", session_id="old-session")
        self.controller.companion = Mock()
        def delayed_error(*_, **__):
            self.controller.session["session_id"] = "new-session"
            self.controller.remote_snapshot = {"new-session": True}
            raise OSError("old connection failed")
        self.controller.companion.call.side_effect = delayed_error
        self.controller.poll()
        self.assertEqual(self.controller.remote_snapshot, {"new-session": True})
        self.assertEqual(events, [])

    def test_benchmark_blocks_cloud_and_import_even_with_connected_session(self):
        with patch.dict(os.environ, {"ECC_BENCHMARK": "1"}):
            benchmark = Controller(self.store)
        benchmark.session.update(state="ready", account_id=self.account, session_id="owned-session", notebook_id="owned-notebook")
        benchmark.companion = Mock()
        benchmark.colab = Mock()
        before = self.store.accounts()
        try:
            for action in ("session.start", "session.stop", "session.reconnect", "account.connect", "account.importLegacy", "account.credentials", "account.update", "download.add", "drive.copy", "node.catalog", "workflow.list", "session.cancel"):
                result = benchmark.dispatch({"id": "bench", "action": action, "params": {"confirmed": True}})
                self.assertEqual(result["error"]["code"], "benchmark_readonly")
            benchmark.poll()
            benchmark.companion.call.assert_not_called()
            benchmark.colab.allocate.assert_not_called()
            benchmark.colab.stop.assert_not_called()
            self.assertEqual(benchmark.session["state"], "ready")
            self.assertEqual(self.store.accounts(), before)
            self.assertEqual(self.store.history(), [])
            for action in ("app.snapshot", "app.diagnostics", "history.list", "app.preferences"):
                self.assertTrue(benchmark.dispatch({"id": "local", "action": action, "params": {}})["ok"])
        finally:
            benchmark.close()

    def test_stale_auto_stop_request_cannot_stop_current_session(self):
        self.controller.session.update(state="ready", account_id=self.account, session_id="new-session", notebook_id="new-notebook")
        with patch.object(self.controller, "stop") as stop:
            result = self.request("session.stop", {"account_id": self.account, "session_id": "old-session"})
        self.assertEqual(result["error"]["code"], "session_changed")
        self.assertEqual(self.controller.session["state"], "ready")
        stop.assert_not_called()

    def test_page_delegation_download_names_and_snapshot_cache_runtime(self):
        remote = self.remote()
        remote.call.return_value = {"items": [{"id": "job", "filename": "João.gguf", "status": "complete"}], "total": 100, "offset": 50, "limit": 50, "busy": False}
        result = self.request("download.list", {"offset": 50, "limit": 50})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["items"][0]["name"], "João.gguf")
        self.assertEqual(remote.call.call_args.args[0], "/downloads?offset=50&limit=50")
        self.controller.remote_snapshot.update(cache={"busy": False, "status": "complete", "items": []}, runtime={"installed": True})
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["downloads_total"], 100)
        self.assertEqual(snapshot["cache"]["status"], "complete")
        self.assertTrue(snapshot["runtime"]["installed"])
        self.assertIn("controller_rss", snapshot["local_metrics"])

    def test_registry_delegation_saved_workflow_boundary_and_query_encoding(self):
        remote = self.remote()
        remote.call.return_value = {"items": [], "total": 0, "offset": 0, "limit": 50}
        self.assertTrue(self.request("node.catalog", {"query": "name & João"})["ok"])
        self.assertEqual(remote.call.call_args.args[0], "/nodes/catalog?offset=0&limit=50&query=name+%26+Jo%C3%A3o")
        self.assertTrue(self.request("node.versions", {"id": "registered-node", "limit": 20})["ok"])
        self.assertEqual(remote.call.call_args.args[0], "/nodes/versions?offset=0&limit=20&id=registered-node")
        self.assertTrue(self.request("workflow.list")["ok"])
        self.assertEqual(remote.call.call_args.args[0], "/workflows?offset=0&limit=50")
        self.assertTrue(self.request("workflow.check", {"relative_path": "saved/João.json"})["ok"])
        self.assertEqual(remote.call.call_args.args[1], {"relative_path": "saved/João.json"})
        count = remote.call.call_count
        for path in ("../legacy.json", "C:/legacy.json", "/content/drive/other.json", "saved\\other.json"):
            self.assertFalse(self.request("workflow.check", {"relative_path": path})["ok"])
        self.assertEqual(remote.call.call_count, count)

    def test_legacy_file_actions_preserve_fixed_remote_roots_and_sanitized_receipt(self):
        remote = self.remote()
        remote.call.return_value = {"items": [], "total": 0, "offset": 0, "limit": 50}
        self.assertTrue(self.request("legacy.list", {"kind": "model", "category": "loras"})["ok"])
        self.assertEqual(remote.call.call_args.args[0], "/legacy/files?offset=0&limit=50&kind=model&category=loras")
        self.assertTrue(self.request("legacy.import", {"kind": "workflow", "relative_path": "saved/João.json", "destination_name": "Cópia.json", "root": "ignored-user-root", "token": "synthetic-secret"})["ok"])
        self.assertEqual(remote.call.call_args.args, ("/legacy/import", {"kind": "workflow", "relative_path": "saved/João.json", "destination_name": "Cópia.json"}))
        self.assertEqual(remote.call.call_args.kwargs["timeout"], 1200)
        self.assertNotIn("synthetic-secret", json.dumps(self.store.history()))
        self.assertEqual(self.store.history()[0]["account_id"], self.account)
        self.assertTrue(self.request("legacy.import", {"kind": "model", "category": "llm_gguf", "source_category": "LLM", "relative_path": "model.gguf"})["ok"])
        self.assertEqual(remote.call.call_args.args[1]["source_category"], "LLM")
        self.assertFalse(self.request("legacy.list", {"kind": "model"})["ok"])
        self.assertFalse(self.request("legacy.import", {"kind": "model", "category": "loras", "source_category": "LLM", "relative_path": "model.gguf"})["ok"])
        count = remote.call.call_count
        for changes in ({"relative_path": "../runtime.tar"}, {"relative_path": "/content/old/runtime.tar"}, {"relative_path": "C:/source.json"}, {"relative_path": "saved\\file.json"}, {"destination_name": "../copy.json"}, {"kind": "image"}, {"category": "loras"}):
            self.assertFalse(self.request("legacy.import", {"kind": "workflow", "relative_path": "saved.json", **changes})["ok"])
        self.assertEqual(remote.call.call_count, count)

    def test_old_history_schema_migrates_without_losing_receipts(self):
        self.controller.close(); self.store.close()
        path = self.root / "previous"
        path.mkdir()
        db = sqlite3.connect(path / "state.sqlite3")
        db.execute("CREATE TABLE history(id INTEGER PRIMARY KEY,time REAL,operation TEXT,account_id TEXT,result TEXT,details TEXT)")
        db.execute("INSERT INTO history VALUES(1,1,'session.start','account','complete','{}')")
        db.commit(); db.close()
        migrated = Store(path, crypt=crypt)
        try:
            self.assertEqual(migrated.history_page()["total"], 1)
            self.assertIsNone(migrated.history()[0]["session_id"])
            migrated.event("session.stop", "account", "complete", session_id="new-session")
            self.assertEqual(migrated.history()[0]["session_id"], "new-session")
        finally:
            migrated.close()
        self.store = Store(self.root, crypt=crypt)
        self.controller = Controller(self.store)


if __name__ == "__main__":
    unittest.main()
