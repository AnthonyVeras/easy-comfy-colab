from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from controller.core import Controller, request_body, safe_output
from controller.google import Colab
from controller.state import AppError
from pathlib import Path


class MountTests(unittest.TestCase):
    def fixture(self, consent_required=True):
        colab = object.__new__(Colab)
        colab.account_id = "account"
        colab.session_id = "session"
        colab.language = "pt-BR"
        colab.mount_pending = False
        colab.mount_confirmation = threading.Event()
        colab.emit = Mock()
        colab.client = SimpleNamespace(colab_domain="https://colab.research.google.com")
        colab.session = Mock()
        response = Mock(text=json.dumps({"token": "synthetic-token"}))
        colab.session.get.return_value = response
        calls = []

        def post(_, **kwargs):
            calls.append(dict(kwargs["params"]))
            body = {"success": True} if not consent_required else {"success": False, "unauthorized_redirect_uri": "https://accounts.google.com/synthetic-consent"}
            return Mock(text=json.dumps(body))

        colab.session.post.side_effect = post
        cancel = threading.Event()
        ws = Mock()
        ws.session.msg.side_effect = lambda msgtype, content: {"msg_type": msgtype, "content": content}
        runtime = Mock()

        def execute(code, **kwargs):
            self.assertIn("timeout_ms=600000", code)
            self.assertEqual(kwargs["timeout"], 660)
            message = {"content": {"request": {"authType": "dfs_ephemeral"}}, "metadata": {"colab_msg_id": 7}, "header": {"session": "kernel-owned"}}
            self.assertTrue(runtime.colab_request_hook(message, ws))
            return []

        runtime.execute_code.side_effect = execute
        assignment = SimpleNamespace(endpoint="exact-owned-endpoint", runtime_proxy_info=SimpleNamespace(url="https://owned.googleusercontent.com", token="synthetic-proxy"))
        return colab, cancel, ws, runtime, assignment, calls

    def test_explicit_browser_confirmation_propagates_once_and_replies(self):
        colab, cancel, ws, runtime, assignment, calls = self.fixture()

        def emit(event, data):
            if data.get("needs_confirmation"):
                self.assertTrue(colab.mount_pending)
                self.assertEqual(data["session_id"], "session")
                colab.confirm_mount()

        colab.emit.side_effect = emit
        with patch("controller.google.upstream", return_value=(None, None, None, None, lambda *_: runtime)):
            colab.mount(assignment, cancel)
        self.assertEqual([call["dryrun"] for call in calls], ["true", "false"])
        value = ws.stdin_channel.send.call_args.args[0]["content"]["value"]
        self.assertEqual(value, {"type": "colab_reply", "colab_msg_id": 7})
        self.assertFalse(colab.mount_pending)
        self.assertTrue(colab.emit.call_args.args[1]["completed"])

    def test_existing_grant_does_not_prompt_or_poll(self):
        colab, cancel, ws, runtime, assignment, calls = self.fixture(False)
        with patch("controller.google.upstream", return_value=(None, None, None, None, lambda *_: runtime)):
            colab.mount(assignment, cancel)
        self.assertEqual([call["dryrun"] for call in calls], ["true", "false"])
        self.assertFalse(any(call.args[1].get("needs_confirmation") for call in colab.emit.call_args_list))

    def test_fallback_native_input_uses_selected_language(self):
        colab, cancel, _, runtime, assignment, _ = self.fixture(False)
        runtime.execute_code.side_effect = lambda _, **options: [] if options["stdin_hook"]("input") else []
        with patch("controller.google.upstream", return_value=(None, None, None, None, lambda *_: runtime)), patch("controller.google.secret_prompt", return_value="synthetic-code") as prompt:
            colab.mount(assignment, cancel)
        prompt.assert_called_once_with("Google Drive authorization code", "pt-BR")

    def test_cancel_sends_correlated_sanitized_error_instead_of_hanging(self):
        colab, cancel, ws, runtime, assignment, calls = self.fixture()
        colab.emit.side_effect = lambda _, data: cancel.set() if data.get("needs_confirmation") else None
        with patch("controller.google.upstream", return_value=(None, None, None, None, lambda *_: runtime)), self.assertRaises(AppError) as error:
            colab.mount(assignment, cancel)
        self.assertEqual(error.exception.code, "drive_mount_failed")
        value = ws.stdin_channel.send.call_args.args[0]["content"]["value"]
        self.assertEqual(value["colab_msg_id"], 7)
        self.assertIn("error", value)
        self.assertNotIn("synthetic-token", json.dumps(value))
        self.assertEqual([call["dryrun"] for call in calls], ["true"])
        self.assertFalse(colab.mount_pending)

    def test_transport_failure_sends_correlated_error_and_no_raw_detail(self):
        colab, cancel, ws, runtime, assignment, _ = self.fixture()
        colab.session.get.side_effect = OSError("synthetic-secret-in-url")
        with patch("controller.google.upstream", return_value=(None, None, None, None, lambda *_: runtime)), self.assertRaises(AppError):
            colab.mount(assignment, cancel)
        self.assertNotIn("synthetic-secret-in-url", str(ws.stdin_channel.send.call_args))
        self.assertTrue(any(call.args[1].get("error_code") == "drive_mount_failed" for call in colab.emit.call_args_list))

    def test_confirmation_is_bound_and_can_bypass_startup_operation_lock(self):
        controller = object.__new__(Controller)
        controller.benchmark = False
        controller.session = {"account_id": "account", "session_id": "session"}
        controller.colab = Mock()
        controller.colab.confirm_mount.return_value = {"confirmed": True}
        controller.operation = threading.Lock()
        controller.operation.acquire()
        good = {"purpose": "mount_drive", "account_id": "account", "session_id": "session", "confirmed": True}
        self.assertEqual(controller.action("account.submitCode", good), {"confirmed": True})
        for changes in ({"account_id": "other"}, {"session_id": "other"}, {"confirmed": False}, {"code": "synthetic"}):
            with self.assertRaises(AppError):
                controller.action("account.submitCode", {**good, **changes})
        controller.colab.confirm_mount.assert_called_once()

    def test_reset_does_not_reuse_previous_account_balance(self):
        controller = object.__new__(Controller)
        controller.account_metrics = {"balance": 999, "rate": 3}
        controller.last_balance = 1234
        controller.remote_snapshot = {"metrics": {"balance": 999}}
        controller.idle_since = 12
        controller.reset_metrics()
        self.assertEqual(controller.account_metrics, {"balance": None, "rate": None})
        self.assertEqual(controller.last_balance, 0)
        self.assertEqual(controller.remote_snapshot, {})
        self.assertIsNone(controller.idle_since)

    def test_request_size_matches_utf8_route_limits(self):
        payload = {"workflow": {"label": "João", "text": "x" * (1024 * 1024)}}
        self.assertIn("João".encode(), request_body("/workflows/check", payload))
        self.assertTrue(request_body("/outputs/ack", payload))
        with self.assertRaises(AppError):
            request_body("/downloads", payload)
        with self.assertRaises(AppError):
            request_body("/workflows/import", {"text": "x" * (16 * 1024 * 1024)})

    def test_output_manifest_relative_path_has_byte_limit(self):
        relative = "á" * 513 + ".png"
        entry = {"relative_path": relative, "remote_path": "/content/easy-comfy-colab-2/output-pc/session/" + relative, "size": 0, "sha256": "a" * 64}
        with self.assertRaises(AppError):
            safe_output(Path("C:/temp/results"), entry, "session")


if __name__ == "__main__":
    unittest.main()
