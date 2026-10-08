"""Real local HTTP bridge boundaries, with synthetic bytes and no cloud services."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from remote.common import Paths
from remote.companion import Server, Service


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.status = 200
        self.headers = {"Content-Length": str(len(payload)), "ETag": "synthetic-etag"}


class ComfyModelBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = Paths(root / "runtime", root / "drive")
        self.paths.state.mkdir(parents=True)
        self.paths.models.mkdir(parents=True)
        self.config = dict(account_id="owned-account", session_id="owned-session", nonce="owned-nonce", bearer_token="b"*48, gpu="CPU", output_mode="drive")
        self.service = Service(self.paths, self.config)
        self.server = Server(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.headers = {"Authorization": "Bearer " + self.config["bearer_token"], "X-Easy-Account": "owned-account", "X-Easy-Session": "owned-session", "X-Easy-Nonce": "owned-nonce", "Content-Type": "application/json"}
        self.model = {"url": "https://huggingface.co/org/repo/resolve/main/model.safetensors", "name": "model.safetensors", "directory": "checkpoints"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.downloads.executor.shutdown(wait=True)
        self.temp.cleanup()

    def call(self, route, data=None, headers=None):
        request = Request(self.base + route, data=None if data is None else json.dumps(data).encode(), headers=self.headers if headers is None else headers)
        try:
            with urlopen(request, timeout=5) as response: return response.status, json.load(response)
        except HTTPError as error:
            with error: return error.code, json.load(error)

    def fault(self, code, route, data, **kwargs):
        status, reply = self.call(route, data, **kwargs)
        self.assertGreaterEqual(status, 400)
        self.assertEqual(reply["error"]["code"], code)

    def test_authenticated_enqueue_uses_memory_credentials_and_verified_remote_bytes(self):
        token = "synthetic-hf-private"
        self.assertEqual(self.call("/credentials/models", {"tokens": {"huggingface": token}})[1]["result"], {"updated": True})
        observed = []
        def source(url, credential, headers):
            observed.append(credential)
            return Response(b"synthetic-model")
        self.service.downloads.opener = source
        item = {**self.model, "sha256": hashlib.sha256(b"synthetic-model").hexdigest()}
        status, reply = self.call("/comfy/downloads", item)
        self.assertEqual(status, 200, reply)
        job_id = reply["result"]["job_id"]
        self.service.downloads.executor.shutdown(wait=True)
        _, progress = self.call("/comfy/downloads/" + job_id)
        self.assertEqual(progress["result"]["status"], "complete")
        self.assertEqual(progress["result"]["sha256"], item["sha256"])
        self.assertEqual((self.paths.models / "checkpoints/model.safetensors").read_bytes(), b"synthetic-model")
        self.assertEqual(observed, [token])
        self.assertEqual(self.service.downloads.tokens, {})
        serialized = json.dumps([reply, progress, self.service.snapshot()]) + (self.paths.state / "downloads.json").read_text()
        self.assertNotIn(token, serialized)
        self.assertNotIn("url", progress["result"])
        self.assertNotIn("bearer_token", progress["result"])

    def test_wrong_session_and_unauthenticated_requests_never_store_or_enqueue(self):
        for route, data in (("/credentials/models", {"tokens": {"huggingface": "synthetic"}}), ("/comfy/downloads", self.model)):
            self.fault("unauthorized", route, data, headers={"Content-Type": "application/json"})
            self.fault("identity_mismatch", route, data, headers={**self.headers, "X-Easy-Session": "different"})
        self.assertEqual(self.service.model_credentials, {})
        self.assertEqual(self.service.downloads.jobs, [])

    def test_credentials_replace_clear_validate_and_never_enter_state(self):
        self.call("/credentials/models", {"tokens": {"huggingface": "synthetic-old", "civitai": "synthetic-other"}})
        for data in ({"tokens": {"other": "synthetic"}}, {"tokens": {"huggingface": "bad\n"}}, {"tokens": {"civitai": 42}}, {"tokens": {}, "extra": True}):
            self.fault("invalid_credentials", "/credentials/models", data)
        self.assertEqual(self.service.model_credentials["huggingface"], "synthetic-old")
        self.call("/credentials/models", {"tokens": {"huggingface": "synthetic-new"}})
        self.assertEqual(self.service.model_credentials, {"huggingface": "synthetic-new"})
        self.call("/credentials/models", {"tokens": {}})
        self.assertEqual(self.service.model_credentials, {})
        restarted = Service(self.paths, self.config)
        self.addCleanup(restarted.downloads.executor.shutdown, True)
        self.assertEqual(restarted.model_credentials, {})
        self.assertEqual(list(self.paths.state.iterdir()), [])

    def test_invalid_frontend_fields_urls_paths_or_tokens_are_rejected(self):
        rows = [({**self.model, "tokens": {"huggingface": "synthetic"}}, "invalid_download"),
                ({**self.model, "url": "https://example.invalid/model.safetensors"}, "invalid_url"),
                ({**self.model, "url": self.model["url"] + "?%2574oken=synthetic"}, "invalid_url"),
                ({**self.model, "name": "../model.safetensors"}, "invalid_url"),
                ({**self.model, "directory": "/tmp"}, "invalid_model"),
                ({**self.model, "sha256": "bad"}, "invalid_integrity"),
                ({**self.model, "name": "ã"*130 + ".safetensors"}, "invalid_download")]
        for item, code in rows:
            with self.subTest(code=code): self.fault(code, "/comfy/downloads", item)
        self.assertEqual(self.service.downloads.jobs, [])

    def test_paused_or_busy_session_never_enqueues_but_allows_secret_revocation(self):
        (self.paths.state / "stopping").write_text("paused")
        self.fault("session_paused", "/comfy/downloads", self.model)
        self.assertTrue(self.call("/credentials/models", {"tokens": {}})[1]["ok"])
        self.service.mutation.acquire()
        try: self.fault("runtime_busy", "/comfy/downloads", self.model)
        finally: self.service.mutation.release()
        self.assertEqual(self.service.downloads.jobs, [])

    def test_duplicate_click_maps_one_queue_job_and_llm_category(self):
        with patch.object(self.service.downloads.executor, "submit", return_value=Mock()):
            item = {**self.model, "directory": "LLM"}
            first = self.call("/comfy/downloads", item)[1]["result"]
            second = self.call("/comfy/downloads", item)[1]["result"]
        self.assertEqual(first, second)
        self.assertEqual(len(self.service.downloads.jobs), 1)
        self.assertEqual(self.service.downloads.jobs[0]["category"], "llm_gguf")

    def test_status_lookup_reaches_old_job_beyond_first_page_and_requires_auth(self):
        self.service.downloads.jobs = [{"id": f"{i:032x}", "status": "complete", "filename": str(i) + ".safetensors", "url": "must-not-expose"} for i in range(75)]
        route = "/comfy/downloads/" + "0"*32
        status, reply = self.call(route)
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["filename"], "0.safetensors")
        self.assertNotIn("url", reply["result"])
        self.fault("unauthorized", route, None, headers={})
        self.fault("download_missing", "/comfy/downloads/" + "f"*32, None)

    def test_http_body_limits_are_enforced_before_processing(self):
        for route, value in (("/credentials/models", {"tokens": {"huggingface": "x"*20000}}), ("/comfy/downloads", {**self.model, "url": "x"*20000})):
            self.fault("request_too_large", route, value)
        self.assertEqual(self.service.model_credentials, {})
        self.assertEqual(self.service.downloads.jobs, [])


if __name__ == "__main__":
    unittest.main()
