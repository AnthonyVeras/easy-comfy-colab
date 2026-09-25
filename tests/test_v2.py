"""Regressões com risco de perder downloads, duplicar cópias ou desligar a VM."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from backend import ProfileStore, Snapshot
from model_download import prepare_download, CredentialStore
from services import IdleGuard, SessionLedger, History, check_workflow
from drive_transfer import copy_tree, verified, share

spec = importlib.util.spec_from_file_location(
    "service", ROOT / "custom_nodes/comfy_colab_remote_download/service.py"
)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class SafetyTests(unittest.TestCase):
    def test_ledger_does_not_charge_unobserved_time(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch("services.app_data_dir", return_value=Path(d)),
        ):
            profile = ProfileStore(Path(d) / "profiles.json").current()
            ledger = SessionLedger()
            snapshot = Snapshot(status_known=True, session_exists=True, rate=36)
            ledger.observe(profile, snapshot, now=100)
            first = ledger.observe(profile, snapshot, now=130)
            self.assertAlmostEqual(first["cost"], 0.3)
            missing = ledger.observe(profile, snapshot, now=1000)
            self.assertAlmostEqual(missing["cost"], 0.3)
            self.assertEqual(missing["gaps"], 870)
            snapshot.rate = None
            ledger.observe(profile, snapshot, now=1030)
            missing = ledger.observe(profile, snapshot, now=1060)
            self.assertEqual(missing["gaps"], 900)
            history = History()
            history.event(
                profile, "session", 0, 960, 10, None, estimated_cu=missing["cost"]
            )
            self.assertEqual(History().rows[0]["estimated_cu"], 0.6)

    def test_sharing_preserves_existing_writer(self):
        from unittest.mock import Mock

        source = Mock()
        source.request.return_value = {
            "permissions": [
                {"id": "p", "emailAddress": "dest@example.com", "role": "writer"}
            ]
        }
        share(source, "folder", "dest@example.com", "reader")
        self.assertEqual(source.request.call_count, 1)

    def test_no_checksum_needs_matching_source_version(self):
        original = dict(
            id="source",
            mimeType="application/octet-stream",
            size="12",
            modifiedTime="one",
        )
        self.assertFalse(
            verified(
                original, dict(id="other", mimeType=original["mimeType"], size="12")
            )
        )

    def test_unknown_and_stale_metrics_never_stop(self):
        guard = IdleGuard()
        for metrics in [
            None,
            {},
            dict(timestamp=100, queue_busy=None, downloads_busy=False),
            dict(timestamp=0, queue_busy=False, downloads_busy=False),
        ]:
            self.assertFalse(guard.update(metrics, 1, now=100))
            self.assertIsNone(guard.since)

    def test_activity_resets_idle_timer(self):
        guard = IdleGuard()

        def m(t, **kw):
            return dict(timestamp=t, queue_busy=False, downloads_busy=False, **kw)

        self.assertFalse(guard.update(m(100), 1, now=100))
        self.assertTrue(guard.update(m(161), 1, now=161))
        self.assertFalse(
            guard.update(
                dict(timestamp=162, queue_busy=True, downloads_busy=False), 1, now=162
            )
        )
        self.assertFalse(guard.update(m(200), 1, now=200))

    def test_profiles_keep_new_gpu_choices(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProfileStore(Path(d) / "profiles.json")
            for gpu in ["G4", "CPU", "A100"]:
                store.current().gpu = gpu
                store.save()
                self.assertEqual(ProfileStore(store.path).current().gpu, gpu)

    def test_credentials_are_encrypted_and_separate(self):
        if sys.platform != "win32":
            self.skipTest("DPAPI Windows")
        with tempfile.TemporaryDirectory() as d:
            c = CredentialStore(Path(d) / "secrets.json")
            c.set("one", "Hugging Face", "test-secret-123")
            self.assertNotIn("test-secret", c.path.read_text())
            self.assertEqual(c.get("one", "Hugging Face"), "test-secret-123")
            self.assertEqual(c.get("two", "Hugging Face"), "")

    def test_url_secrets_and_path_traversal_rejected(self):
        for url, name, cat in [
            (
                "https://huggingface.co/a/b/resolve/main/m.gguf?token=secret",
                "m.gguf",
                "loras",
            ),
            ("https://evil.example/m.gguf", "m.gguf", "loras"),
            ("https://huggingface.co/a/b/resolve/main/m.gguf", "../m.gguf", "loras"),
        ]:
            with self.assertRaises(ValueError):
                prepare_download(url, name, cat)
        with self.assertRaises(ValueError):
            service.destination(
                dict(
                    url="https://huggingface.co/a/b/resolve/main/m.gguf",
                    name="../../x.gguf",
                    directory="loras",
                )
            )

    def test_workflow_checks_nested_graph(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "w.json"
            path.write_text(
                json.dumps(
                    {
                        "nodes": [{"type": "known", "widgets_values": ["ready.gguf"]}],
                        "definitions": {
                            "subgraphs": [
                                {
                                    "id": "sub",
                                    "nodes": [
                                        {
                                            "type": "missing",
                                            "widgets_values": ["absent.gguf"],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                )
            )
            result = check_workflow(path, {"known": {}}, [dict(name="ready.gguf")])
            self.assertEqual({r[1] for r in result}, {"missing", "absent.gguf"})


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.MODEL_ROOT = Path(self.temp.name) / "models"
        service.MODEL_ROOT.mkdir()
        service.STATE_PATH = Path(self.temp.name) / "jobs.json"
        service.JOBS.clear()
        service.ACTIVE_TARGETS.clear()
        service.RUNNING = 0
        service.LIMIT = 2

    async def asyncTearDown(self):
        self.temp.cleanup()

    def job(self, id):
        job = dict(
            id=id,
            status="queued",
            bytes=0,
            total=None,
            name=id + ".gguf",
            directory="loras",
        )
        service.JOBS[id] = job
        return job

    async def test_parallel_limit_and_completion(self):
        running = 0
        peak = 0
        lock = threading.Lock()

        def fake(job, url, target, token):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.1)
            job.update(bytes=2, total=2)
            with lock:
                running -= 1

        with patch.object(service, "download_file", fake):
            await asyncio.gather(
                *(
                    service.run_download(
                        self.job(str(i)),
                        "url",
                        service.MODEL_ROOT / f"{i}.gguf",
                        "secret",
                    )
                    for i in range(5)
                )
            )
        self.assertEqual(peak, 2)
        self.assertTrue(all(j["status"] == "complete" for j in service.JOBS.values()))
        self.assertNotIn("secret", service.STATE_PATH.read_text())

    async def test_queued_cancellation_never_starts(self):
        job = self.job("cancel")
        job["cancel"] = True
        with patch.object(service, "download_file") as transfer:
            await service.run_download(
                job, "url", service.MODEL_ROOT / "cancel.gguf", ""
            )
        transfer.assert_not_called()
        self.assertEqual(job["status"], "cancelled")

    async def test_existing_file_is_not_overwritten(self):
        target = service.MODEL_ROOT / "a.gguf"
        target.write_bytes(b"original")
        with patch.object(service, "download_file") as transfer:
            await service.run_download(self.job("exists"), "url", target, "")
        transfer.assert_not_called()
        self.assertEqual(target.read_bytes(), b"original")

    async def test_partial_not_promoted_on_error(self):
        target = service.MODEL_ROOT / "bad.gguf"

        def fail(*args):
            target.with_suffix(".gguf.part").write_bytes(b"partial")
            raise ValueError("interrupted")

        with patch.object(service, "download_file", fail):
            await service.run_download(self.job("bad"), "url", target, "")
        self.assertFalse(target.exists())
        self.assertTrue(target.with_suffix(".gguf.part").exists())
        self.assertEqual(service.JOBS["bad"]["status"], "error")

    async def test_interrupted_jobs_restored(self):
        self.job("one")["status"] = "running"
        service.persist()
        service.JOBS.clear()
        service.restore()
        self.assertEqual(service.JOBS["one"]["status"], "interrupted")

    async def test_http_routes_and_origin_guard(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        routes = web.RouteTableDef()
        service.register(routes, mode="downloads", queue_status=lambda: False)
        app = web.Application()
        app.add_routes(routes)
        async with TestClient(TestServer(app)) as client:
            reply = await client.get("/comfy-colab/model-download/capabilities")
            self.assertEqual((await reply.json())["mode"], "downloads")
            blocked = await client.post(
                "/comfy-colab/downloads/settings",
                json={"parallel": 2},
                headers={"Origin": "https://evil.example"},
            )
            self.assertEqual(blocked.status, 403)
            reply = await client.post(
                "/comfy-colab/downloads/settings", json={"parallel": 4}
            )
            self.assertEqual((await reply.json())["parallel"], 4)

    async def test_range_resume_writes_correct_file(self):
        class Response:
            url = "https://huggingface.co/a/b/resolve/main/a.gguf"
            status_code = 206
            headers = {
                "Content-Length": "3",
                "Content-Range": "bytes 3-5/6",
                "ETag": "stable",
            }

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def raise_for_status(self):
                pass

            def iter_content(self, *args):
                yield b"def"

        target = service.MODEL_ROOT / "resume.gguf"
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(b"abc")
        partial.with_name(partial.name + ".json").write_text(
            json.dumps({"url": Response.url, "etag": "stable"})
        )
        with patch.object(service.requests, "get", return_value=Response()) as get:
            await asyncio.to_thread(
                service.download_file,
                self.job("resume"),
                Response.url,
                target,
                "test-token",
            )
        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertFalse(partial.exists())
        self.assertEqual(get.call_args.kwargs["headers"]["If-Range"], "stable")

    async def test_cancel_preserves_partial(self):
        job = self.job("cancel-partial")

        class Response:
            url = "https://huggingface.co/a/b/resolve/main/a.gguf"
            status_code = 200
            headers = {"Content-Length": "6", "ETag": "stable"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def raise_for_status(self):
                pass

            def iter_content(self, *args):
                yield b"abc"
                job["cancel"] = True
                yield b"def"

        target = service.MODEL_ROOT / "cancel.gguf"
        with patch.object(service.requests, "get", return_value=Response()):
            with self.assertRaises(service.Cancelled):
                await asyncio.to_thread(
                    service.download_file, job, Response.url, target, ""
                )
        self.assertFalse(target.exists())
        self.assertEqual(target.with_name(target.name + ".part").read_bytes(), b"abc")


class FakeDrive:
    def __init__(self, source=None):
        self.source = source
        self.files = {}
        self.counter = 0
        self.copies = 0

    def children(self, parent):
        return [f for f in self.files.values() if parent in f.get("parents", [])]

    def request(self, method, path, **kw):
        if path.endswith("/permissions"):
            return {"permissions": []}
        data = kw["json"]
        self.counter += 1
        if path.endswith("/copy"):
            self.copies += 1
            base = dict(self.source.files[path.split("/")[1]])
        else:
            base = {}
        base.update(data)
        base["id"] = str(self.counter)
        self.files[base["id"]] = base
        return base


class DriveTests(unittest.TestCase):
    def test_resume_does_not_duplicate_and_keeps_source(self):
        src = FakeDrive()
        item = dict(
            id="source-file",
            name="m.gguf",
            mimeType="application/octet-stream",
            size="5",
            md5Checksum="abcd",
            modifiedTime="today",
            parent_source="root",
            path=["m.gguf"],
        )
        src.files[item["id"]] = item.copy()
        dst = FakeDrive(src)
        for _ in range(2):
            copy_tree(
                src,
                dst,
                {"id": "root"},
                [item],
                {"id": "dest"},
                "destination@example.com",
            )
        self.assertEqual(dst.copies, 1)
        self.assertEqual(src.files[item["id"]], item)

    def test_corrupt_copy_is_not_accepted(self):
        self.assertFalse(
            verified(
                dict(mimeType="binary", size="10", md5Checksum="good"),
                dict(mimeType="binary", size="10", md5Checksum="bad"),
            )
        )

    def test_name_conflict_preserves_old_file(self):
        src = FakeDrive()
        item = dict(
            id="s",
            name="m.gguf",
            mimeType="binary",
            size="5",
            md5Checksum="new",
            modifiedTime="today",
            parent_source="root",
            path=["m.gguf"],
        )
        src.files["s"] = item
        dst = FakeDrive(src)
        dst.files["old"] = dict(
            id="old",
            name="m.gguf",
            mimeType="binary",
            size="9",
            md5Checksum="old",
            parents=["dest"],
        )
        copy_tree(
            src, dst, {"id": "root"}, [item], {"id": "dest"}, "destination@example.com"
        )
        self.assertEqual(dst.files["old"]["md5Checksum"], "old")
        self.assertTrue(any("(cópia " in f["name"] for f in dst.files.values()))


if __name__ == "__main__":
    unittest.main()
