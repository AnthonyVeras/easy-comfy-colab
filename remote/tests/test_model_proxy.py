import json
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from remote.common import Fault, Paths
from remote.model_bridge import LIMIT, NoRedirect, install_model_bridge, register, relay, same_origin, validate_model_bridge


class Routes:
    def __init__(self): self.handlers = {}
    def add(self, method, path):
        def save(handler):
            self.handlers[method, path] = handler
            return handler
        return save
    def post(self, path): return self.add("POST", path)
    def get(self, path): return self.add("GET", path)


class Body:
    def __init__(self, value): self.value = value
    async def iter_chunked(self, size):
        for offset in range(0, len(self.value), 7): yield self.value[offset:offset+7]


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.routes = Routes()
        web = SimpleNamespace(json_response=lambda body, status=200: SimpleNamespace(body=body, status=status))
        with patch.dict(sys.modules, {"aiohttp": SimpleNamespace(web=web)}): register(self.routes, {"bearer_token": "synthetic-private"})
        self.enqueue = self.routes.handlers["POST", "/easy-comfy/model-download"]
        self.progress = self.routes.handlers["GET", "/easy-comfy/model-download/{job_id}"]

    def request(self, body=None, **headers):
        return SimpleNamespace(headers={"Host": "127.0.0.1:28188", "Origin": "http://127.0.0.1:28188", **headers}, content_type="application/json", content=Body(json.dumps(body).encode()), match_info={"job_id": "a"*32})

    async def test_fragmented_browser_request_queues_same_model_only_on_vm_route(self):
        model = {"url": "https://huggingface.co/org/repo/resolve/main/model.safetensors", "name": "model.safetensors", "directory": "vae"}
        relay = Mock(return_value=(200, {"ok": True, "result": {"job_id": "a"*32}}))
        with patch("remote.model_bridge.relay", relay): reply = await self.enqueue(self.request(model))
        self.assertEqual(reply.status, 200)
        self.assertEqual(relay.call_args.args[1:], ("/comfy/downloads", model))
        self.assertNotIn("synthetic-private", json.dumps(reply.body))

    async def test_cross_origin_missing_origin_and_oversized_requests_never_relay(self):
        with patch("remote.model_bridge.relay") as relay:
            for origin in ("http://evil.example", "null", "", "http://127.0.0.1:28189"):
                self.assertEqual((await self.enqueue(self.request({}, Origin=origin))).status, 403)
            request = self.request({"name": "x"*(LIMIT+1)})
            self.assertEqual((await self.enqueue(request)).status, 413)
            request.content_type = "text/plain"
            self.assertEqual((await self.enqueue(request)).status, 415)
            relay.assert_not_called()

    async def test_status_cannot_become_arbitrary_proxy_path(self):
        with patch("remote.model_bridge.relay", return_value=(200, {"ok": True, "result": {"status": "complete"}})) as relay:
            request = self.request()
            request.match_info["job_id"] = "../credentials/models"
            self.assertEqual((await self.progress(request)).status, 404)
            relay.assert_not_called()
            request.match_info["job_id"] = "a"*32
            self.assertEqual((await self.progress(request)).body["result"]["status"], "complete")
            self.assertEqual(relay.call_args.args[1:], ("/comfy/downloads/"+"a"*32,))

    async def test_vm_error_propagates_instead_of_browser_download(self):
        error = {"ok": False, "error": {"code": "model_auth_required", "message": "Configure provider credentials."}}
        with patch("remote.model_bridge.relay", return_value=(403, error)):
            reply = await self.enqueue(self.request({}))
        self.assertEqual((reply.status, reply.body), (403, error))


class InstallationTests(unittest.TestCase):
    def test_proxy_uses_fixed_loopback_identity_headers_without_redirects_or_system_proxy(self):
        config = dict(account_id="synthetic-account", session_id="synthetic-session", nonce="synthetic-nonce", bearer_token="synthetic-private")
        response = io.BytesIO(b'{"ok":true,"result":{"job_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}')
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        with patch("remote.model_bridge.build_opener", return_value=opener) as factory:
            status, reply = relay(config, "/comfy/downloads", {"name": "model.safetensors"})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8190/comfy/downloads")
        self.assertEqual(dict((key.lower(), value) for key,value in request.header_items())["authorization"], "Bearer synthetic-private")
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], NoRedirect)
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,"",{},"https://evil.test"))
        self.assertEqual(status,200)
        self.assertNotIn("synthetic-private", json.dumps(reply))

    def test_fresh_bridge_has_web_and_restoration_detects_missing_or_changed_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory)/"runtime", Path(directory)/"drive")
            install_model_bridge(paths)
            validate_model_bridge(paths.root)
            extension = paths.comfy/"custom_nodes/easy_model_download/web/remote_download.js"
            extension.write_text("browser-download-regression")
            with self.assertRaises(Fault): validate_model_bridge(paths.root)
            install_model_bridge(paths)
            validate_model_bridge(paths.root)
            extension.unlink()
            with self.assertRaises(Fault): validate_model_bridge(paths.root)

    def test_local_host_and_origin_checks_reject_dns_rebinding_and_cross_site(self):
        self.assertTrue(same_origin({"Host": "127.0.0.1:28188", "Origin": "http://127.0.0.1:28188"}, True))
        self.assertTrue(same_origin({"Host": "[::1]:28188"}))
        for host in ("evil.test:28188", "127.0.0.1:bad", "127.0.0.1/path", "user@127.0.0.1"):
            self.assertFalse(same_origin({"Host": host}))
        self.assertFalse(same_origin({"Host": "127.0.0.1:28188", "Sec-Fetch-Site": "cross-site"}))


if __name__ == "__main__": unittest.main()
