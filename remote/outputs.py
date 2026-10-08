"""Admission barrier, immutable output manifests and controller acknowledgements."""
import hashlib
import json
import os
import stat
from pathlib import Path
from contextlib import contextmanager

try:
    from .common import Fault, atomic_json, digest, inside, queue_idle, read_json, relative_path
except ImportError:
    from common import Fault, atomic_json, digest, inside, queue_idle, read_json, relative_path


class Outputs:
    def __init__(self, paths, config, idle=queue_idle):
        self.paths, self.config, self.idle = paths, config, idle
        self.root = paths.drive / "output" if config["output_mode"] == "drive" else paths.root / "output-pc" / config["session_id"]
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def ack_path(self): return self.paths.state / ("output-ack-"+self.config["output_mode"]+".json")

    def acknowledged(self):
        saved = read_json(self.ack_path, {})
        return saved if saved.get("output_root") == str(self.root) else {}

    def candidates(self):
        count = 0
        def inspected(file):
            try: return file.lstat()
            except OSError: raise Fault("unsafe_output", "An output path could not be inspected; preserve the VM for recovery.") from None
        root = inspected(self.root)
        if not stat.S_ISDIR(root.st_mode): raise Fault("unsafe_output", "The owned output root must be a readable directory without symbolic links.")
        def unreadable(error): raise Fault("unsafe_output", "An output directory could not be read; preserve the VM for recovery.")
        for directory, dirs, names in os.walk(self.root, followlinks=False, onerror=unreadable):
            dirs.sort()
            for name in dirs:
                if not stat.S_ISDIR(inspected(Path(directory)/name).st_mode): raise Fault("unsafe_output", "An output directory changed or contains a symbolic link.")
            for name in sorted(names):
                file = Path(directory)/name
                if not stat.S_ISREG(inspected(file).st_mode): raise Fault("unsafe_output", "An output changed or contains an unsupported link or device.")
                count += 1
                if count > 10000: raise Fault("output_limit", "Deliver outputs in batches before this session exceeds 10,000 files.")
                yield file

    @contextmanager
    def barrier(self):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        marker = self.paths.state / "stopping"
        marker.touch()
        with (self.paths.state / "admission.lock").open("a") as lock:
            if os.name != "nt":
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            if self.config.get("gpu") != "CPU" and not self.idle():
                marker.unlink(missing_ok=True)
                raise Fault("generation_busy", "Wait for the generation queue before stopping or capturing outputs.")
            yield

    def file_row(self, file):
        if file.is_symlink(): raise Fault("unsafe_output", "An output links outside the owned output directory.")
        name = file.relative_to(self.root).as_posix()
        relative_path(name)
        if len(name.encode("utf-8")) > 1024: raise Fault("output_limit", "Output relative paths must fit 1,024 UTF-8 bytes for verified delivery.")
        if file.suffix in {".part", ".tmp", ".partial"}: raise Fault("output_incomplete", "An output is still being written; the VM remains active.")
        with file.open("rb+") as committed: os.fsync(committed.fileno())
        before = file.stat(); checksum = digest(file); after = file.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns): raise Fault("output_changed", "Output changed while verifying; retry the final copy.")
        return {"relative_path": name, "remote_path": str(file), "size": before.st_size, "sha256": checksum}, after.st_mtime_ns

    def files(self):
        rows = []
        for file in self.candidates():
            rows.append(self.file_row(file)[0])
        return rows

    def prepare(self):
        with self.barrier():
            rows = self.files()
            manifest_id = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            result = {"manifest_id": manifest_id, "files": rows, "pending_outputs": self.pending(rows), "paused": True}
            atomic_json(self.paths.state / "output-manifest.json", result)
            return result

    def pending(self, files=None, quick=False):
        if self.config["output_mode"] == "drive": return 0
        saved = self.acknowledged()
        ack = saved.get("files", {})
        if quick:
            pending = 0
            for file in self.candidates():
                name = file.relative_to(self.root).as_posix(); stat = file.stat()
                if file.is_symlink() or ack.get(name, {}).get("size") != stat.st_size or saved.get("mtimes", {}).get(name) != stat.st_mtime_ns: pending += 1
            return pending
        return sum(ack.get(row["relative_path"]) != {"size": row["size"], "sha256": row["sha256"]} for row in (self.files() if files is None else files))

    def ack(self, data):
        if data.get("delivery") is True: return self.delivery_ack(data)
        if not (self.paths.state / "stopping").exists(): raise Fault("output_not_paused", "Capture outputs before acknowledging their delivery.")
        manifest = read_json(self.paths.state / "output-manifest.json", {})
        if data.get("manifest_id") != manifest.get("manifest_id") or not isinstance(data.get("files"), list) or len(data["files"]) > 10000:
            raise Fault("output_mismatch", "Output acknowledgement does not match the captured manifest.")
        expected = {r["relative_path"]: {"size": r["size"], "sha256": r["sha256"]} for r in manifest["files"]}
        supplied = {}
        for row in data["files"]:
            if not isinstance(row, dict): raise Fault("output_mismatch", "Each output acknowledgement must be an object.")
            name = row.get("relative_path")
            inside(self.root, name)
            if name in supplied: raise Fault("output_mismatch", "Duplicate output acknowledgements are not accepted.")
            supplied[name] = {"size": row.get("size"), "sha256": row.get("sha256")}
        if expected != supplied or self.files() != manifest["files"]:
            raise Fault("output_mismatch", "Copy every output durably and verify size and SHA-256 before acknowledging.")
        atomic_json(self.ack_path, {"manifest_id": manifest["manifest_id"], "output_root": str(self.root), "files": supplied, "mtimes": {name: inside(self.root, name).stat().st_mtime_ns for name in supplied}})
        return {"verified": True, "pending_outputs": 0}

    def delivery(self):
        if self.config["output_mode"] != "pc": raise Fault("invalid_mode", "Continuous PC delivery requires PC output mode.")
        if (self.paths.state / "stopping").exists(): raise Fault("session_paused", "Finish the current output capture before continuous delivery.")
        if self.config.get("gpu") != "CPU" and not self.idle(): raise Fault("generation_busy", "Continuous delivery waits for the generation queue to become idle.")
        saved = self.acknowledged(); rows, mtimes = [], {}
        # Capture only pending files; unchanged verified outputs are not rehashed on each poll.
        for file in self.candidates():
            name = file.relative_to(self.root).as_posix(); stat = file.stat()
            if saved.get("files", {}).get(name, {}).get("size") == stat.st_size and saved.get("mtimes", {}).get(name) == stat.st_mtime_ns: continue
            if file.suffix in {".part", ".tmp", ".partial"}: continue
            try: row, modified = self.file_row(file)
            except Fault as error:
                if error.code == "output_changed": continue
                raise
            rows.append(row); mtimes[name] = modified
            if len(rows) == 100: break
        manifest_id = hashlib.sha256(json.dumps([str(self.root),rows], sort_keys=True).encode()).hexdigest()
        report = {"manifest_id": manifest_id, "output_root": str(self.root), "files": rows, "mtimes": mtimes, "delivery": True, "paused": False}
        atomic_json(self.paths.state / "output-delivery.json", report)
        return {**{key: report[key] for key in ("manifest_id", "files", "delivery", "paused")}, "output_mode":"pc", "pending_outputs":self.pending(quick=True)}

    def delivery_ack(self, data):
        manifest = read_json(self.paths.state / "output-delivery.json", {})
        rows = data.get("files")
        if self.config["output_mode"] != "pc" or manifest.get("output_root") != str(self.root) or data.get("manifest_id") != manifest.get("manifest_id") or not isinstance(rows, list) or len(rows) > 100:
            raise Fault("output_mismatch", "Continuous delivery acknowledgement does not match its captured batch.")
        expected = {row["relative_path"]: {"size": row["size"], "sha256": row["sha256"]} for row in manifest["files"]}
        supplied, mtimes = {}, {}
        for row in rows:
            if not isinstance(row, dict): raise Fault("output_mismatch", "Each output acknowledgement must be an object.")
            name = row.get("relative_path")
            file = inside(self.root, name)
            if name in supplied: raise Fault("output_mismatch", "Duplicate output acknowledgements are not accepted.")
            actual, modified = self.file_row(file)
            supplied[name] = {"size": row.get("size"), "sha256": row.get("sha256")}
            if supplied[name] != {"size": actual["size"], "sha256": actual["sha256"]} or manifest.get("mtimes", {}).get(name) != modified:
                raise Fault("output_mismatch", "An output changed during continuous delivery; its source was preserved for retry.")
            mtimes[name] = modified
        if supplied != expected: raise Fault("output_mismatch", "Copy and verify the entire continuous delivery batch before acknowledging.")
        saved = self.acknowledged()
        atomic_json(self.ack_path, {"output_root":str(self.root), "files":{**saved.get("files", {}), **supplied}, "mtimes":{**saved.get("mtimes", {}), **mtimes}})
        # A live batch never claims the admission barrier or final shutdown gate was verified.
        return {"verified":True, "delivered":len(supplied), "paused":False, "pending_outputs":self.pending(quick=True)}

    def resume(self):
        (self.paths.state / "stopping").unlink(missing_ok=True)
        return {"paused": False}
