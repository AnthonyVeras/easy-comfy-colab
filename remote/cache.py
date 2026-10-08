"""Disposable SSD copies exposed through ComfyUI's standard extra-model paths."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import threading
import time

try:
    from .common import CATEGORIES, SUFFIXES, Fault, atomic_json, digest, inside, queue_idle, read_json
except ImportError:
    from common import CATEGORIES, SUFFIXES, Fault, atomic_json, digest, inside, queue_idle, read_json

GIB = 1024**3


def model_paths(paths):
    for root in (paths.cache, paths.models):
        for category in CATEGORIES: (root / category).mkdir(parents=True, exist_ok=True)
    config = {}
    for name, root, preferred in (("easy_ssd", paths.cache, True), ("easy_drive", paths.models, False)):
        config[name] = dict(base_path=str(root), is_default=preferred, **{c: c for c in sorted(CATEGORIES)})
    atomic_json(paths.root / "extra-model-paths.yaml", config)


def memory():
    entries = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return int(entries["MemTotal"].split()[0])*1024, int(entries["MemAvailable"].split()[0])*1024


class Cache:
    def __init__(self, paths, idle=queue_idle):
        self.paths, self.idle = paths, idle
        self.lock = threading.RLock(); self.cancelled = threading.Event(); self.worker = None
        self.records = read_json(paths.state / "cache.json", {})
        self.state = {"status": "idle", "items": [], "error": None, "busy": False, "requires_restart": False}

    def snapshot(self):
        with self.lock: return dict(self.state, items=[dict(x) for x in self.state["items"]])

    def invalidate(self, relative):
        with self.lock:
            target = inside(self.paths.cache, relative)
            target.unlink(missing_ok=True); self.records.pop(relative, None)
            atomic_json(self.paths.state / "cache.json", self.records)

    def reconcile(self):
        for relative, record in list(self.records.items()):
            source = inside(self.paths.models, relative)
            target = inside(self.paths.cache, relative)
            if not source.is_file() or (source.stat().st_size, source.stat().st_mtime_ns) != (record["size"], record["mtime_ns"]) or not target.is_file() or digest(target) != record["sha256"]:
                self.invalidate(relative)

    def checkpoint(self):
        if self.cancelled.is_set(): raise Fault("cancelled", "Cache preparation cancelled.")
        while not self.idle():
            self.state["status"] = "waiting"
            if self.cancelled.wait(0.5): raise Fault("cancelled", "Cache preparation cancelled.")

    def prepare(self, data):
        selected = data.get("models")
        ram_gib = data.get("ram_gib", 1)
        warm = data.get("warm_ram", False)
        if not isinstance(selected, list) or not 1 <= len(selected) <= 100 or type(warm) is not bool or type(ram_gib) is not int or not 1 <= ram_gib <= 128:
            raise Fault("invalid_cache", "Select 1–100 models and a 1–128 GiB pre-read budget.")
        for name in selected:
            path = inside(self.paths.models, name)
            if Path(name).parts[0] not in CATEGORIES or path.suffix.lower() not in SUFFIXES or not path.is_file(): raise Fault("model_missing", "A selected model is missing from the library.")
        if sum(inside(self.paths.models, n).stat().st_size for n in set(selected)) + 2*GIB > shutil.disk_usage(self.paths.root).free:
            raise Fault("disk_full", "Selected models exceed the SSD cache budget; Drive originals are preserved.")
        with self.lock:
            if self.state["busy"]: raise Fault("cache_busy", "Wait or cancel the current cache preparation.")
            self.cancelled.clear()
            self.state.update(status="queued", busy=True, error=None, items=[{"path": name, "bytes": 0, "status": "queued"} for name in dict.fromkeys(selected)])
            self.worker = threading.Thread(target=self.run, args=(warm, ram_gib), daemon=True); self.worker.start()
        return self.snapshot()

    def copy(self, item):
        source, target = inside(self.paths.models, item["path"]), inside(self.paths.cache, item["path"])
        partial = target.with_name(target.name + ".part")
        if partial.is_symlink(): raise Fault("invalid_path", "Cache partial files cannot be symbolic links.")
        try:
            self.checkpoint(); before = source.stat()
            if before.st_size + 2*GIB > shutil.disk_usage(self.paths.root).free: raise Fault("disk_full", "SSD cache has insufficient free space; Drive originals are preserved.")
            target.parent.mkdir(parents=True, exist_ok=True); item["status"] = "copying"
            with source.open("rb") as reader, partial.open("wb") as writer:
                while True:
                    self.checkpoint(); chunk = reader.read(8*1024**2)
                    if not chunk: break
                    if shutil.disk_usage(self.paths.root).free < 2*GIB + len(chunk): raise Fault("disk_full", "SSD reserve reached; Drive originals are preserved.")
                    writer.write(chunk); item["bytes"] += len(chunk)
                writer.flush(); os.fsync(writer.fileno())
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or partial.stat().st_size != before.st_size:
                raise Fault("model_changed", "Model changed during preparation; no cache was published.")
            checksum = digest(partial)
            self.checkpoint()
            with self.lock:
                if (source.stat().st_size, source.stat().st_mtime_ns) != (before.st_size, before.st_mtime_ns): raise Fault("model_changed", "Model changed before cache publication.")
                partial.replace(target)
                self.records[item["path"]] = {"size": before.st_size, "mtime_ns": before.st_mtime_ns, "sha256": checksum}
                atomic_json(self.paths.state / "cache.json", self.records)
            item["status"] = "cached"
        finally: partial.unlink(missing_ok=True)

    def run(self, warm, ram_gib):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(self.copy, self.state["items"]))
            if warm:
                total, available = memory(); reserve = max(8*GIB, total//4); budget = min(ram_gib*GIB, max(0, available-reserve))
                for item in self.state["items"]:
                    target = inside(self.paths.cache, item["path"])
                    if target.stat().st_size > budget: continue
                    with target.open("rb") as reader:
                        while budget:
                            self.checkpoint()
                            if memory()[1] < reserve: break
                            chunk = reader.read(min(8*1024**2, budget))
                            if not chunk: break
                            budget -= len(chunk); item["warm_bytes"] = item.get("warm_bytes", 0) + len(chunk)
            self.state["status"] = "complete"
        except Fault as error: self.state.update(status="cancelled" if error.code == "cancelled" else "error", error=error.message)
        except Exception: self.state.update(status="error", error="Cache preparation failed; Drive originals were preserved.")
        finally: self.state["busy"] = False

    def cancel(self):
        self.cancelled.set(); return self.snapshot()

    def clear(self):
        with self.lock:
            if self.state["busy"] or not self.idle(): raise Fault("cache_busy", "Wait for cache preparation and generation before clearing SSD copies.")
            for name in list(self.records): self.invalidate(name)
            self.state.update(status="idle", items=[], error=None)
            return self.snapshot()
