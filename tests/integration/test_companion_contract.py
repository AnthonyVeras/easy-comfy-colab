"""Controller/remote contracts over real local HTTP, without cloud credentials."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from controller.core import Companion, Controller
from controller.state import Store
from remote.common import Paths
from remote.companion import Server, Service


class CompanionContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / "local", crypt=lambda data, decrypt=False:data)
        account = self.store.add("Conta de João")
        self.store.bind(account, {"sub":"subject", "email":"test@example.invalid", "email_verified":True})
        self.controller = Controller(self.store)
        self.controller.session.update(state="ready", account_id=account, session_id="test-session",
            nonce="test-nonce", endpoint="owned-test-endpoint", subject="subject", gpu="CPU", output_mode="drive")
        self.config = {key:self.controller.session[key] for key in ("account_id","session_id","nonce","gpu","output_mode")}
        self.config["bearer_token"] = "b" * 48
        paths = Paths(root / "runtime", root / "drive")
        paths.state.mkdir(parents=True)
        paths.models.mkdir(parents=True)
        model = paths.models / "checkpoints" / "modelo-acento-ã.safetensors"
        model.parent.mkdir()
        model.write_bytes(b"synthetic-model")
        self.service = Service(paths, self.config)
        self.server = Server(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.controller.companion = Companion(self.controller.session, self.config["bearer_token"])
        self.controller.companion.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.controller.transport = SimpleNamespace(close=Mock())

    def tearDown(self):
        self.controller.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_model_list_reaches_public_snapshot_with_utf8(self):
        response = self.controller.dispatch({"id":"models", "action":"model.list", "params":{}})
        self.assertTrue(response["ok"], response)
        models = self.controller.snapshot()["models"]
        self.assertIsInstance(models, list)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["name"], "modelo-acento-ã.safetensors")

    def test_polled_remote_catalog_stays_an_array(self):
        self.controller.poll()
        snapshot = self.controller.snapshot()
        self.assertIsInstance(snapshot["models"], list)
        self.assertEqual(len(snapshot["models"]), 1)


if __name__ == "__main__":
    unittest.main()
