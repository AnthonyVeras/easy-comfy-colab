from __future__ import annotations

import hashlib
import copy
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from controller.core import Companion, Controller
from controller.state import AppError, Store


class OutputLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state", crypt=lambda data, decrypt=False: data)
        self.account = self.store.add("Teste")
        self.store.bind(self.account, {"sub": "subject", "email": "test@example.invalid", "email_verified": True})
        self.store.update(self.account, output_mode="pc")
        self.events = []
        self.controller = Controller(self.store, lambda event, data: self.events.append((event, data)))
        self.controller.session = {**self.controller.idle(), "state": "ready", "account_id": self.account, "session_id": "owned-session", "nonce": "owned-nonce", "endpoint": "exact-endpoint", "subject": "subject", "notebook_id": "00000000-0000-0000-0000-000000000001", "gpu": "CPU", "output_mode": "pc", "drive_mounted": True, "prepared": True, "services_started": True, "ssh_fingerprint": "owned-key"}
        self.remote, self.transport, self.colab = Mock(), Mock(), Mock()
        self.controller.companion, self.controller.transport, self.controller.colab = self.remote, self.transport, self.colab
        self.controller.last_balance = float("inf")
        self.controller.transport.sftp.get.side_effect = lambda _, destination: Path(destination).write_bytes(b"abc")
        self.home = patch("controller.core.Path.home", return_value=self.root)
        self.home.start()
        self.readiness = patch.object(self.controller, "ready", side_effect=lambda _: self.controller.session.update(state="ready", pending_outputs=0))
        self.readiness.start()
        self.row = {"relative_path": "João/image.png", "remote_path": "/content/easy-comfy-colab-2/output-pc/owned-session/João/image.png", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
        self.manifest = {"manifest_id": "manifest", "files": [self.row], "pending_outputs": 1}

    def tearDown(self):
        self.controller.close()
        self.readiness.stop()
        self.home.stop()
        self.store.close()
        self.temp.cleanup()

    def identity(self, mode):
        return {"account_id": self.account, "session_id": "owned-session", "nonce": "owned-nonce", "output_mode": mode, "remote_ports": {"comfy": None, "companion": 8190, "mcp": None}, "ready": True}

    def restart(self, target):
        return self.controller.dispatch({"id": "restart", "action": "session.restart", "params": {"output_mode": target}})

    def verified_old(self):
        self.remote.call.side_effect = [self.manifest, {"verified": True}, {"verified": True}]

    def target(self):
        return self.root / "Easy Comfy Colab 2 Results" / self.account / "output/João/image.png"

    def test_pc_to_drive_durably_delivers_old_outputs_before_same_vm_restart(self):
        self.verified_old()
        def bootstrap(payload):
            self.assertEqual(self.target().read_bytes(), b"abc")
            self.assertEqual(self.store.account(self.account)["output_mode"], "pc")
            self.assertEqual(payload["previous_output_mode"], "pc")
            self.assertEqual(payload["output_mode"], "drive")
            self.assertEqual(payload["session_id"], "owned-session")
            return {"ok": True, "result": self.identity("drive")}
        self.transport.bootstrap.side_effect = bootstrap
        self.assertTrue(self.restart("drive")["ok"])
        self.assertEqual([call.args[0] for call in self.remote.call.call_args_list], ["/outputs/prepare", "/outputs/ack", "/runtime/save"])
        self.assertEqual(self.controller.session["output_mode"], "drive")
        self.assertEqual(self.store.account(self.account)["output_mode"], "drive")
        self.assertNotIn("output_transition", self.controller.session)
        self.colab.allocate.assert_not_called(); self.colab.stop.assert_not_called()

    def test_drive_to_pc_reverifies_drive_ack_without_downloading_old_drive_files(self):
        self.controller.session["output_mode"] = "drive"
        self.store.update(self.account, output_mode="drive")
        self.row["remote_path"] = "/content/drive/MyDrive/EasyComfyColab2/output/João/image.png"
        self.verified_old()
        self.transport.bootstrap.return_value = {"ok": True, "result": self.identity("pc")}
        self.assertTrue(self.restart("pc")["ok"])
        self.transport.sftp.get.assert_not_called()
        self.assertEqual(self.remote.call.call_args_list[1].args[1]["files"], [{key: self.row[key] for key in ("relative_path", "size", "sha256")}])
        self.assertEqual(self.store.account(self.account)["output_mode"], "pc")

    def test_pc_delivery_corruption_keeps_previous_mode_vm_and_original_outputs(self):
        self.remote.call.side_effect = [self.manifest, {"paused": False}]
        self.transport.sftp.get.side_effect = lambda _, destination: Path(destination).write_bytes(b"bad")
        result = self.restart("drive")
        self.assertEqual(result["error"]["code"], "output_integrity")
        self.assertEqual(self.store.account(self.account)["output_mode"], "pc")
        self.assertEqual(self.controller.session["output_mode"], "pc")
        self.assertFalse(self.target().exists())
        self.transport.bootstrap.assert_not_called(); self.colab.stop.assert_not_called()
        self.assertEqual(self.remote.call.call_args_list[-1].args[0], "/outputs/resume")

    def test_unverified_runtime_blocks_mode_change_after_output_copy(self):
        self.remote.call.side_effect = [self.manifest, {"verified": True}, {"verified": False}, {"paused": False}]
        result = self.restart("drive")
        self.assertEqual(result["error"]["code"], "runtime_unverified")
        self.transport.bootstrap.assert_not_called()
        self.assertEqual(self.store.account(self.account)["output_mode"], "pc")
        self.assertNotIn("output_transition", self.controller.session)

    def test_committed_remote_mode_after_failure_is_reconciled_and_reconnect_starts_same_vm(self):
        self.verified_old()
        self.transport.bootstrap.side_effect = [{"ok": False, "error": {"code": "readiness_timeout"}}, {"ok": True, "result": self.identity("drive")}]
        result = self.restart("drive")
        self.assertEqual(result["error"]["code"], "readiness_timeout")
        self.assertEqual(self.controller.session["output_mode"], "drive")
        self.assertFalse(self.controller.session["services_started"])
        self.assertEqual(self.store.account(self.account)["output_mode"], "pc")
        self.assertIn("output_transition", self.store.get("active_session"))
        self.assertNotIn("/outputs/resume", [call.args[0] for call in self.remote.call.call_args_list])
        self.controller.colab_factory = lambda *_: self.colab
        self.controller.transport_factory = lambda *_: self.transport
        self.transport.fingerprint = "owned-key"
        self.colab.recover.return_value = SimpleNamespace(endpoint="exact-endpoint", runtime_proxy_info=Mock())
        self.transport.bootstrap.side_effect = [{"ok": True, "result": self.identity("drive")}, {"ok": True, "result": self.identity("drive")}]
        self.transport.bootstrap.reset_mock()
        with patch("controller.transport.reserved_listeners", return_value={name: Mock() for name in ("comfy", "companion", "mcp")}), patch.object(self.controller, "connect_companion", side_effect=lambda: setattr(self.controller, "companion", self.remote)):
            recovery = self.controller.dispatch({"id": "recover", "action": "session.reconnect", "params": {}})
        self.assertTrue(recovery["ok"])
        self.assertEqual([call.args[0]["action"] for call in self.transport.bootstrap.call_args_list], ["health", "start"])
        self.assertEqual(self.store.account(self.account)["output_mode"], "drive")
        self.assertNotIn("output_transition", self.controller.session)
        self.colab.allocate.assert_not_called(); self.colab.stop.assert_not_called()

    def test_lost_reply_and_unknown_health_never_assume_old_mode_is_safe_to_resume(self):
        self.verified_old()
        self.transport.bootstrap.side_effect = OSError("synthetic-private-network-error")
        self.assertFalse(self.restart("drive")["ok"])
        self.assertIn("output_transition", self.store.get("active_session"))
        self.assertEqual(self.store.account(self.account)["output_mode"], "pc")
        self.assertNotIn("/outputs/resume", [call.args[0] for call in self.remote.call.call_args_list])

    def test_unprepared_failure_can_stop_only_exact_notebook_after_remote_empty_runtime_gate(self):
        self.controller.session.update(prepared=False, services_started=False, state="error")
        self.controller.companion = None
        self.transport.fingerprint = "owned-key"
        self.colab.recover.return_value = SimpleNamespace(endpoint="exact-endpoint", runtime_proxy_info=Mock())
        self.transport.bootstrap.return_value = {"ok": True, "result": {**self.identity("pc"), "safe_to_stop": True, "unprepared": True, "installed": False, "runtime_required": False, "pending_outputs": 0}}
        response = self.controller.dispatch({"id": "stop", "action": "session.stop", "params": {}})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["state"], "idle")
        self.assertEqual(self.transport.bootstrap.call_args.args[0]["action"], "stop_unprepared")
        self.colab.recover.assert_called_once()
        self.colab.allocate.assert_not_called()
        self.colab.stop.assert_called_once_with("exact-endpoint")
        self.assertEqual(self.store.get("active_session")["state"], "idle")

    def test_unprepared_gate_refusal_or_incomplete_proof_preserves_vm(self):
        self.controller.session.update(prepared=False, services_started=False, state="error")
        self.transport.fingerprint = "owned-key"
        self.colab.recover.return_value = SimpleNamespace(endpoint="exact-endpoint", runtime_proxy_info=Mock())
        for reply in ({"ok": False, "error": {"code": "runtime_already_prepared"}}, {"ok": True, "result": {**self.identity("pc"), "safe_to_stop": True, "unprepared": True, "installed": False, "runtime_required": False, "pending_outputs": 1}}, {"ok": True, "result": {**self.identity("pc"), "safe_to_stop": True, "installed": False, "runtime_required": False, "pending_outputs": 0}}, {"ok": True, "result": {**self.identity("pc"), "nonce": "other-nonce", "safe_to_stop": True, "unprepared": True, "installed": False, "runtime_required": False, "pending_outputs": 0}}, {"ok": True, "result": {**self.identity("pc"), "safe_to_stop": True, "unprepared": True, "runtime_verified": True, "pending_outputs": 0}}):
            self.transport.bootstrap.return_value = reply
            response = self.controller.dispatch({"id": "stop", "action": "session.stop", "params": {}})
            self.assertFalse(response["ok"])
            self.assertEqual(self.controller.session["endpoint"], "exact-endpoint")
        self.colab.stop.assert_not_called()

    def test_prepared_or_services_started_contradiction_cannot_use_unprepared_shortcut(self):
        self.controller.companion = None
        for prepared, started in ((True, False), (False, True)):
            self.controller.session.update(prepared=prepared, services_started=started, state="error")
            response = self.controller.dispatch({"id": "stop", "action": "session.stop", "params": {}})
            self.assertFalse(response["ok"])
        self.transport.bootstrap.assert_not_called()
        self.colab.recover.assert_not_called()
        self.colab.stop.assert_not_called()

    def test_unprepared_shutdown_requires_matching_endpoint_and_fingerprint(self):
        self.controller.session.update(prepared=False, services_started=False, state="error")
        self.transport.fingerprint = "owned-key"
        self.colab.recover.return_value = SimpleNamespace(endpoint="different-endpoint", runtime_proxy_info=Mock())
        response = self.controller.dispatch({"id": "stop", "action": "session.stop", "params": {}})
        self.assertEqual(response["error"]["code"], "owned_vm_missing")
        self.colab.recover.return_value.endpoint = "exact-endpoint"
        self.transport.fingerprint = "different-key"
        response = self.controller.dispatch({"id": "stop", "action": "session.stop", "params": {}})
        self.assertEqual(response["error"]["code"], "host_changed")
        self.transport.upload_remote.assert_not_called()
        self.transport.bootstrap.assert_not_called()
        self.colab.stop.assert_not_called()

    def snapshot(self, queue=None):
        return {"queue": {"running": 0, "pending": 0} if queue is None else queue, "downloads": {"busy": False, "items": []}, "cache": {"busy": False}, "pending_outputs": 1}

    def live(self):
        return {**self.manifest, "delivery": True, "output_mode": "pc", "paused": False}

    def test_continuous_pc_delivery_acks_durable_copy_without_pausing_or_final_gate(self):
        self.remote.call.side_effect = [self.snapshot(), self.live(), {"verified": True, "pending_outputs": 0, "paused": False}]
        self.controller.poll()
        self.assertEqual(self.target().read_bytes(), b"abc")
        self.assertEqual([call.args[0] for call in self.remote.call.call_args_list], ["/snapshot", "/outputs/delivery", "/outputs/ack"])
        self.assertTrue(self.remote.call.call_args_list[-1].args[1]["delivery"])
        self.assertEqual(self.controller.output_delivery["status"], "complete")
        self.assertEqual(self.controller.session["pending_outputs"], 0)
        self.colab.stop.assert_not_called()

    def test_continuous_copy_failure_does_not_ack_pause_or_erase_pending_outputs(self):
        self.remote.call.side_effect = [self.snapshot(), self.live()]
        self.transport.sftp.get.side_effect = lambda _, destination: Path(destination).write_bytes(b"bad")
        self.controller.poll()
        self.assertEqual([call.args[0] for call in self.remote.call.call_args_list], ["/snapshot", "/outputs/delivery"])
        self.assertEqual(self.controller.output_delivery["error_code"], "output_integrity")
        self.assertEqual(self.controller.session["pending_outputs"], 1)
        self.assertEqual(self.controller.session["state"], "ready")
        self.colab.stop.assert_not_called()

    def test_live_delivery_is_skipped_for_unknown_queue_running_queue_or_drive_mode(self):
        for mode, queue in (("pc", {}), ("pc", {"running": 1, "pending": 0}), ("drive", {"running": 0, "pending": 0})):
            self.controller.session["output_mode"] = mode
            self.remote.call.reset_mock()
            self.remote.call.return_value = self.snapshot(queue)
            self.controller.poll()
            self.remote.call.assert_called_once_with("/snapshot", timeout=15)
        self.transport.sftp.get.assert_not_called()

    def test_session_change_after_live_manifest_prevents_cross_account_file_copy(self):
        def reply(route, *_, **__):
            if route == "/snapshot": return self.snapshot()
            self.controller.session["session_id"] = "different-session"
            self.controller.remote_snapshot = {"different-session": True}
            return self.live()
        self.remote.call.side_effect = reply
        self.controller.poll()
        self.transport.sftp.get.assert_not_called()
        self.assertEqual(self.controller.remote_snapshot, {"different-session": True})
        self.assertEqual(self.events, [])

    def test_real_local_http_companion_live_ack_and_final_stop_use_separate_proofs(self):
        from remote.common import Paths, read_json
        from remote.companion import Server, Service
        paths = Paths(self.root / "runtime", self.root / "drive")
        paths.state.mkdir(parents=True); paths.models.mkdir(parents=True)
        config = {key: self.controller.session[key] for key in ("account_id", "session_id", "nonce", "gpu", "output_mode")}
        config["bearer_token"] = "b" * 48
        service = Service(paths, config)
        original_route = service.route
        def route(method, path, query, data):
            result = original_route(method, path, query, data)
            if path == "/snapshot": result["queue"] = {"running": 0, "pending": 0}
            if path in {"/outputs/delivery", "/outputs/prepare"}:
                result = copy.deepcopy(result)
                for row in result["files"]:
                    row["remote_path"] = "/content/easy-comfy-colab-2/output-pc/owned-session/" + row["relative_path"]
            return result
        service.route = route
        source = service.outputs.root / "João/image.png"
        source.parent.mkdir(); source.write_bytes(b"abc")
        server = Server(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        companion = Companion(self.controller.session, config["bearer_token"])
        companion.base = "http://127.0.0.1:" + str(server.server_port)
        self.controller.companion = companion
        def sftp_get(remote_path, destination):
            name = remote_path.removeprefix("/content/easy-comfy-colab-2/output-pc/owned-session/")
            shutil.copyfile(service.outputs.root / name, destination)
        self.transport.sftp.get.side_effect = sftp_get
        try:
            self.controller.poll()
            self.assertEqual(self.target().read_bytes(), b"abc")
            self.assertEqual(self.controller.output_delivery["status"], "complete")
            self.assertEqual(service.outputs.pending(), 0)
            self.assertFalse((paths.state / "stopping").exists())
            self.assertFalse((paths.state / "output-manifest.json").exists())
            ack = read_json(paths.state / "output-ack-pc.json")
            self.assertEqual(ack["output_root"], str(service.outputs.root))
            self.controller.bootstrap = Mock(side_effect=lambda action: service.route("POST", "/stop/prepare", {}, {}))
            response = self.controller.dispatch({"id": "stop", "action": "session.stop", "params": {}})
            self.assertTrue(response["ok"], response)
            self.assertTrue((paths.state / "output-manifest.json").is_file())
            self.assertEqual(source.read_bytes(), b"abc")
            self.colab.stop.assert_called_once_with("exact-endpoint")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            service.downloads.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
