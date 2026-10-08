"""Model credentials reach only the authenticated companion of the owned account."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from controller.core import Controller, packages
from controller.state import AppError, Store


class ModelCredentialBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), crypt=lambda value, decrypt=False: value)
        self.account = self.store.add("Conta ativa")
        self.other = self.store.add("Outra conta")
        self.events = []
        self.controller = Controller(self.store, emit=lambda *event: self.events.append(event))
        self.controller.session.update(state="ready", account_id=self.account, session_id="owned-session", nonce="owned-nonce", endpoint="owned-endpoint")
        self.remote = Mock()
        self.remote.call.return_value = {"updated": True}
        self.controller.companion = self.remote
        self.store.secret(self.account, "Hugging Face", "synthetic-active-hf")
        self.store.secret(self.other, "Hugging Face", "synthetic-other-hf")
        self.store.secret(self.account, "session:owned-session", "synthetic-session-bearer")

    def tearDown(self):
        self.controller.close()
        self.store.close()
        self.temp.cleanup()

    def request(self, **params):
        return self.controller.dispatch({"id": "credential", "action": "account.credentials", "params": params})

    def test_pending_base_metadata_is_separate_from_installed_nodes_and_keeps_protection(self):
        resources = Path(self.temp.name)/"resources"
        (resources/"remote").mkdir(parents=True)
        (resources/"remote/runtime.json").write_text(json.dumps({"packages": [{"id":"ready-node","folder":"Ready"},{"id":"pending-node","folder":"Pending","status":"unsupported"}]}))
        with patch("controller.core.resources", return_value=resources): controller = Controller(self.store)
        try:
            snapshot = controller.snapshot()
            self.assertEqual(snapshot["capabilities"]["pending_base_packages"], ["Pending"])
            self.assertEqual(snapshot["packages"], [])
            self.assertNotIn("synthetic-active-hf", json.dumps(snapshot))
            self.assertTrue(packages([{"id":"ready-node","protected":True}])[0]["protected"])
        finally: controller.close()

    def test_attach_pushes_owned_account_credentials_after_identity_health(self):
        with patch("controller.core.Companion", return_value=self.remote) as factory:
            self.controller.connect_companion()
        factory.assert_called_once_with(self.controller.session, "synthetic-session-bearer")
        self.assertEqual(self.remote.method_calls[0][0], "health")
        self.remote.call.assert_called_once_with("/credentials/models", {"tokens": {"huggingface": "synthetic-active-hf", "civitai": ""}})
        self.assertEqual(self.events, [])

    def test_attach_identity_failure_never_pushes_credentials(self):
        self.remote.health.side_effect = AppError("session_identity_mismatch", "Wrong session.")
        with patch("controller.core.Companion", return_value=self.remote), self.assertRaises(AppError):
            self.controller.connect_companion()
        self.remote.call.assert_not_called()

    def test_native_credential_change_pushes_without_jsonl_or_history_secret(self):
        with patch("controller.core.secret_prompt", return_value="synthetic-civitai-secret"):
            reply = self.request(account_id=self.account, provider="Civitai")
        self.assertTrue(reply["ok"], reply)
        self.remote.call.assert_called_once_with("/credentials/models", {"tokens": {"huggingface": "synthetic-active-hf", "civitai": "synthetic-civitai-secret"}})
        output = json.dumps([reply, self.events, self.store.history()])
        for value in ("synthetic-civitai-secret", "synthetic-active-hf", "synthetic-other-hf"):
            self.assertNotIn(value, output)

    def test_remove_replaces_memory_token_with_empty_value(self):
        reply = self.request(account_id=self.account, provider="Hugging Face", remove=True)
        self.assertEqual(reply["result"], {"provider": "Hugging Face", "configured": False})
        self.remote.call.assert_called_once_with("/credentials/models", {"tokens": {"huggingface": "", "civitai": ""}})
        self.assertIsNone(self.store.secret(self.account, "Hugging Face"))

    def test_other_account_change_cannot_replace_active_account_tokens(self):
        with patch("controller.core.secret_prompt", return_value="synthetic-other-new"):
            reply = self.request(account_id=self.other, provider="Civitai")
        self.assertTrue(reply["ok"], reply)
        self.remote.call.assert_not_called()

    def test_failed_memory_sync_reports_error_without_secret(self):
        self.remote.call.side_effect = AppError("runtime_busy", "Retry synchronization.")
        with patch("controller.core.secret_prompt", return_value="synthetic-new-secret"):
            reply = self.request(account_id=self.account, provider="Civitai")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "runtime_busy")
        self.assertNotIn("synthetic-new-secret", json.dumps([reply, self.events, self.store.history()]))


if __name__ == "__main__":
    unittest.main()
