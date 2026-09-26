"""Cache não pode servir parciais, sair da biblioteca ou perder o modelo original."""
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from services import workflow_cache_selection, IdleGuard

spec = importlib.util.spec_from_file_location("model_cache", ROOT / "custom_nodes/comfy_colab_remote_download/model_cache.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "drive/models"
        (self.source / "diffusion_models").mkdir(parents=True)
        self.original = self.source / "diffusion_models/model.gguf"
        self.original.write_bytes(b"weights" * 100)
        self.cache = module.ModelCache(self.source, {"diffusion_models", "loras"}, {".gguf", ".safetensors"}, root=self.base / "cache")
        self.plan = dict(models=["diffusion_models/model.gguf"], enabled=True)

    def tearDown(self):
        self.cache.cancel()
        if self.cache.worker:
            self.cache.worker.join(5)
        self.temp.cleanup()

    def prepare(self, **options):
        self.cache.start(dict(self.plan, **options))
        self.cache.worker.join(5)
        self.assertFalse(self.cache.busy)
        return self.cache.snapshot()

    def test_loaders_use_complete_copy_and_fall_back_after_source_change(self):
        before = self.original.read_bytes()
        state = self.prepare()
        self.assertEqual(state["status"], "complete")
        folder_paths = SimpleNamespace(get_full_path=lambda *_: str(self.original))
        module.install_resolver(folder_paths, self.cache)
        cached = Path(folder_paths.get_full_path("unet_gguf", "model.gguf"))
        self.assertNotEqual(cached, self.original)
        self.assertEqual(cached.read_bytes(), before)
        self.assertEqual(self.original.read_bytes(), before)
        self.original.write_bytes(b"new model")
        self.assertEqual(folder_paths.get_full_path("unet_gguf", "model.gguf"), str(self.original))
        self.prepare()
        self.assertEqual(cached.read_bytes(), b"new model")
        self.cache.configure(dict(enabled=False))
        self.assertEqual(folder_paths.get_full_path("unet_gguf", "model.gguf"), str(self.original))

    def test_warm_limit_and_pressure_preserve_disk_cache(self):
        with patch("psutil.virtual_memory", return_value=SimpleNamespace(total=32 * module.GIB, available=20 * module.GIB)):
            state = self.prepare(warm_ram=True)
        self.assertEqual(state["items"][0]["status"], "warm")
        self.assertEqual(state["items"][0]["warm_bytes"], self.original.stat().st_size)
        with patch("psutil.virtual_memory", return_value=SimpleNamespace(total=32 * module.GIB, available=4 * module.GIB)):
            state = self.prepare(warm_ram=True)
        self.assertEqual(state["items"][0]["status"], "cached")
        self.assertIn("RAM", state["items"][0]["note"])

    def test_busy_queue_waits_cancel_works_and_idle_guard_does_not_stop(self):
        self.cache.queue_status = lambda: True
        self.cache.start(self.plan)
        self.assertTrue(self.cache.busy)
        guard = IdleGuard()
        metrics = dict(queue_busy=False, downloads_busy=self.cache.busy, timestamp=time.time())
        self.assertFalse(guard.update(metrics, 1))
        self.cache.cancel()
        self.cache.worker.join(5)
        self.assertEqual(self.cache.snapshot()["status"], "cancelled")
        self.assertEqual(self.cache.resolve(str(self.original)), str(self.original))
        self.assertTrue(self.original.is_file())
        self.assertEqual(list(self.cache.root.rglob("*.part")), [])

    def test_disk_full_and_invalid_paths_do_not_touch_source(self):
        with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            state = self.prepare()
        self.assertEqual(state["status"], "error")
        self.assertTrue(self.original.is_file())
        for path in ("../outside.gguf", "/etc/passwd", "loras/../../x.gguf", "loras\\x.gguf", "loras/x.py"):
            with self.assertRaises(ValueError):
                self.cache.configure(dict(models=[path]))
        with self.assertRaises(ValueError):
            self.cache.configure(dict(ram_gib=0))

    def test_atomic_publication_reuse_and_restart_plan(self):
        self.prepare()
        cached = Path(self.cache.resolve(str(self.original)))
        timestamp = cached.stat().st_mtime_ns
        self.prepare()
        self.assertEqual(cached.stat().st_mtime_ns, timestamp)
        restored = module.ModelCache(self.source, {"diffusion_models", "loras"}, {".gguf"}, root=self.cache.root)
        self.assertEqual(restored.settings["models"], self.plan["models"])
        self.assertEqual(restored.resolve(str(self.original)), str(cached))
        cached.write_bytes(b"truncated")
        self.assertEqual(restored.resolve(str(self.original)), str(self.original))

    def test_clear_preserves_drive_and_refuses_during_generation(self):
        self.prepare()
        cached = Path(self.cache.resolve(str(self.original)))
        self.cache.queue_status = lambda: True
        with self.assertRaises(ValueError):
            self.cache.clear()
        self.assertTrue(cached.exists())
        self.cache.queue_status = lambda: False
        self.cache.clear()
        self.assertFalse(cached.exists())
        self.assertTrue(self.original.exists())

    def test_parallel_copy_is_bounded_at_two(self):
        for i in range(4):
            (self.source / "diffusion_models" / f"{i}.gguf").write_bytes(b"data")
        self.plan["models"] = [f"diffusion_models/{i}.gguf" for i in range(4)]
        lock = threading.Lock()
        running = peak = 0
        original = self.cache.copy_model

        def observed(item):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.03)
            original(item)
            with lock:
                running -= 1

        with patch.object(self.cache, "copy_model", observed):
            state = self.prepare()
        self.assertEqual(peak, 2)
        self.assertEqual(state["status"], "complete")

    def test_workflow_selection_handles_subgraphs_and_ambiguous_names(self):
        workflow = self.base / "workflow.json"
        workflow.write_text(json.dumps(dict(nodes=[dict(type="loader", widgets_values=["model.gguf"]),
                                                 dict(type="loader", mode=4, widgets_values=["bypass.gguf"])],
                                           definitions=dict(subgraphs=[dict(nodes=[dict(type="loader", widgets_values=["duplicate.safetensors", "missing.gguf"])])]))))
        models = [dict(category="diffusion_models", name="model.gguf"),
                  dict(category="loras", name="duplicate.safetensors"),
                  dict(category="vae", name="duplicate.safetensors")]
        selected, unresolved = workflow_cache_selection(workflow, models)
        self.assertEqual(selected, {"diffusion_models/model.gguf"})
        self.assertEqual(len(unresolved), 2)
        self.assertTrue(any("ambíguo" in name for name in unresolved))


if __name__ == "__main__":
    unittest.main()
