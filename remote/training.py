"""MOSS project recipes, validated datasets and durable training admission.

Training always saves to the new Drive tree, including when image delivery is PC.
No user Python, shell fragments, model URLs or external checkpoint paths execute.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile

try:
    from .common import Fault, atomic_json, digest, inside, read_json, relative_path
except ImportError:
    from common import Fault, atomic_json, digest, inside, read_json, relative_path

TRAINER_REVISION = "da5dd97e15492e4dfb3caa171f023388004884ff"
MODEL_REVISION = "ee64003102c8470ee32501cf883419235db81884"
RECIPE = 1
MAX_ARCHIVE = 1024**3
MAX_EXPANDED = 3 * 1024**3
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def group_active(pid, ignore_current=False):
    """A killed worker can leave a trainer child alive in its process group."""
    if type(pid) is not int or pid <= 1: return False
    if os.name != "posix": raise Fault("training_owner", "Worker process groups require the Linux runtime.")
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit(): continue
            candidate = int(entry.name)
            if ignore_current and candidate == os.getpid(): continue
            try:
                if os.getpgid(candidate) == pid: return True
            except ProcessLookupError: continue
    except OSError: raise Fault("training_owner", "Worker process group could not be inspected; preserve the VM.") from None
    return False


def settings(data):
    result = {}
    for key, default, low, high in (("steps", 500, 10, 20000), ("rank", 16, 4, 128), ("resolution", 768, 256, 1536), ("seed", 42, 0, 2**31-1), ("save_every", 100, 10, 2000)):
        value = data.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise Fault("training_settings", f"{key}: expected integer {low}–{high}.")
        result[key] = value
    if result["resolution"] % 32: raise Fault("training_settings", "Resolution must be a multiple of 32.")
    lr = data.get("lr", 0.0001)
    if type(lr) not in (int, float) or not math.isfinite(lr) or not 1e-7 <= lr <= 0.001:
        raise Fault("training_settings", "Learning rate must be between 1e-7 and 0.001.")
    result["lr"] = lr
    for key, default, pattern in (("name", "identity", r"[A-Za-z0-9_-]{1,48}"), ("trigger", "hrn_person", r"[A-Za-z0-9_-]{1,64}"), ("profile", "auto", r"auto|low|balanced")):
        value = data.get(key, default)
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise Fault("training_settings", f"Invalid {key}.")
        result[key] = value
    return result


def extract_dataset(archive, target, trigger):
    """Accept one leaf of image/caption pairs; ignore selection-review assets."""
    if archive.is_symlink() or archive.stat().st_size > MAX_ARCHIVE:
        raise Fault("training_dataset", "Choose a regular ZIP of at most 1 GiB.")
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 15000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
            raise Fault("training_dataset", "Dataset exceeds the extraction limit.")
        names, seen = {}, set()
        for item in entries:
            name = item.filename.rstrip("/")
            relative_path(name)
            mode = item.external_attr >> 16
            if item.flag_bits & 1 or stat.S_ISLNK(mode) or item.file_size > 100 * 1024**2 or item.compress_size and item.file_size/item.compress_size > 1000:
                raise Fault("training_dataset", "ZIP has an unsupported or oversized entry.")
            folded = name.casefold()
            if folded in seen: raise Fault("training_dataset", "ZIP contains duplicate paths.")
            seen.add(folded)
            if not item.is_dir(): names[name] = item
        folders = {}
        for name in names:
            file = Path(name)
            if file.suffix.lower() in IMAGE_SUFFIXES and file.with_suffix(".txt").as_posix() in names:
                folders.setdefault(file.parent.as_posix(), []).append(name)
        preferred = {k: v for k, v in folders.items() if Path(k).name in {"imagens", "images"}}
        candidates = preferred or folders
        if len(candidates) != 1: raise Fault("training_dataset", "ZIP must contain one image folder with matching .txt captions.")
        folder, images = next(iter(candidates.items()))
        all_images = [n for n in names if Path(n).parent.as_posix() == folder and Path(n).suffix.lower() in IMAGE_SUFFIXES]
        if len(images) != len(all_images) or not 5 <= len(images) <= 5000:
            raise Fault("training_dataset", "Every image needs its own caption; use 5–5,000 images.")
        target.mkdir(parents=True, exist_ok=False)
        rows = []
        for index, name in enumerate(sorted(images), 1):
            caption = bundle.read(names[Path(name).with_suffix(".txt").as_posix()])
            if len(caption) > 16384: raise Fault("training_dataset", "Caption exceeds 16 KiB.")
            try: text = caption.decode("utf-8-sig").strip()
            except UnicodeError: raise Fault("training_dataset", "Captions must be UTF-8.") from None
            if not text or re.search(r"(?<!\w)"+re.escape(trigger)+r"(?!\w)", text) is None:
                raise Fault("training_dataset", "Each caption must contain the selected trigger.")
            dest = target / f"{index:05d}{Path(name).suffix.lower()}"
            dest.write_bytes(bundle.read(names[name]))
            dest.with_suffix(".txt").write_text(text+"\n", encoding="utf-8")
            rows.append({"name": dest.name, "sha256": digest(dest), "caption_sha256": digest(dest.with_suffix(".txt"))})
        return rows


class Training:
    def __init__(self, paths):
        self.paths = paths
        self.file = paths.state / "training.json"
        self.marker = paths.state / "training-active"
        self.projects = paths.drive / "training/projects"
        self.projects.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def current(self): return read_json(self.file, {"phase": "idle", "busy": False, "preserved": True})

    @property
    def busy(self): return self.current().get("busy") is not False

    def require_safe(self):
        value = self.current()
        if value.get("busy") is not False or value.get("preserved") is not True or self.marker.exists():
            raise Fault("training_busy", "Finish or cancel training and verify its Drive results before stopping or changing the runtime.")

    def project(self, project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r"[a-f0-9]{24}", project_id):
            raise Fault("training_project", "Select a saved training project.")
        value = read_json(self.projects / (project_id+".json"))
        if not value: raise Fault("training_project", "Training project was not found on this Drive.")
        params = settings(value.get("settings", {}))
        sha = value.get("dataset_sha256")
        expected = hashlib.sha256(json.dumps([RECIPE, TRAINER_REVISION, MODEL_REVISION, sha, params], sort_keys=True).encode()).hexdigest()[:24]
        run = str(self.paths.drive / "output/training" / project_id / params["name"])
        if expected != project_id or value.get("id") != project_id or value.get("run") != run or value.get("recipe") != RECIPE or value.get("trainer_revision") != TRAINER_REVISION or value.get("model_revision") != MODEL_REVISION or not isinstance(sha, str) or re.fullmatch(r"[a-f0-9]{64}", sha) is None:
            raise Fault("training_project", "Saved project metadata changed; prepare a new project.")
        return value

    def snapshot(self):
        value = self.current()
        if value.get("busy") and value.get("pid") and not group_active(value["pid"]):
            value = {**value, "phase": "error", "error": "Worker exited. Verify Drive results before continuing."}
        projects = [self.project(p.stem) for p in sorted(self.projects.glob("*.json"))[:100]]
        project = next((p for p in projects if p["id"] == value.get("project_id")), None)
        metrics = None
        checkpoints = []
        if project:
            root = Path(project["run"])
            log = root / "metrics.jsonl"
            if log.is_file():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size-65536))
                    for line in stream.read().splitlines()[-30:]:
                        try: metrics = json.loads(line)
                        except ValueError: continue
            for folder in sorted((root / "checkpoints").glob("step_*")):
                if re.fullmatch(r"step_\d{8}", folder.name) and all((folder / n).is_file() for n in ("state.json", "trainable.safetensors", "optim.pt", "rng.0.pt")):
                    checkpoints.append(folder.name)
        if metrics and project:
            seconds = metrics.get("step_time")
            if isinstance(seconds, (int, float)) and seconds > 0:
                value["eta_seconds"] = max(0, project["settings"]["steps"]-metrics.get("step", 0))*seconds
        return {**value, "projects": projects, "metrics": metrics, "checkpoints": checkpoints}

    def launch(self, action, data):
        with self.lock:
            self.require_safe()
            if action == "prepare":
                params = settings(data)
                sha = data.get("dataset_sha256", "")
                if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha): raise Fault("training_dataset", "Upload the dataset ZIP first.")
                archive = self.paths.drive / "training/uploads" / (sha+".zip")
                if not archive.is_file() or digest(archive) != sha: raise Fault("training_dataset", "Dataset upload checksum did not match.")
                project_id = hashlib.sha256(json.dumps([RECIPE, TRAINER_REVISION, MODEL_REVISION, sha, params], sort_keys=True).encode()).hexdigest()[:24]
                project = {"id": project_id, "settings": params, "dataset_sha256": sha, "recipe": RECIPE, "trainer_revision": TRAINER_REVISION, "model_revision": MODEL_REVISION,
                           "run": str(self.paths.drive / "output/training" / project_id / params["name"])}
                atomic_json(self.projects / (project_id+".json"), project)
            else:
                project = self.project(data.get("project_id"))
                receipt = read_json(self.paths.root / "training/prepared" / (project["id"]+".json"), {})
                if receipt.get("recipe") != RECIPE: raise Fault("training_not_prepared", "Prepare this saved project on the current VM first.")
                run = Path(project["run"])
                if action == "start" and run.exists() and any(run.iterdir()): raise Fault("training_exists", "Resume the saved run, or prepare a project with a new name.")
                if action == "resume" and not any((p / "optim.pt").is_file() and (p / "state.json").is_file() and (p / "trainable.safetensors").is_file() and (p / "rng.0.pt").is_file() for p in (run / "checkpoints").glob("step_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]")):
                    raise Fault("training_checkpoint", "No complete resumable checkpoint exists yet.")
            job = uuid.uuid4().hex
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.touch()
            state = {"job": job, "project_id": project["id"], "phase": "launching", "busy": True, "preserved": False, "started_at": time.time()}
            atomic_json(self.file, state)
            try:
                log = (self.paths.state / "training-worker.log").open("ab")
                try:
                    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("training_worker.py")), "--job", job], stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
                finally: log.close()
                state["pid"] = process.pid
                atomic_json(self.file, state)
                process.stdin.write(json.dumps({"root": str(self.paths.root), "drive": str(self.paths.drive), "action": action, "project": project}).encode()); process.stdin.close()
                threading.Thread(target=self._watch, args=(process, job), daemon=True).start()
            except Exception:
                atomic_json(self.file, {**state, "busy": False, "phase": "error", "error": "Worker launch failed; verify results before continuing."})
                raise
            return state

    def _watch(self, process, job):
        code = process.wait()
        with self.lock:
            value = self.current()
            if value.get("job") == job and value.get("busy"):
                atomic_json(self.file, {**value, "busy": False, "preserved": False, "phase": "error", "error": f"Worker exited ({code}); verify Drive results."})

    def cancel(self):
        value = self.current()
        if not value.get("busy"): return value
        pid, job = value.get("pid"), value.get("job")
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if str(Path(__file__).with_name("training_worker.py")).encode() not in command or job.encode() not in command or os.getpgid(pid) != pid:
                raise Fault("training_owner", "Worker ownership is unknown; preserve this VM for recovery.")
            # Cancellation is cooperative. Never report stopped until all child processes exit.
            os.killpg(pid, signal.SIGINT)
        except (OSError, TypeError): raise Fault("training_owner", "Worker could not be verified. Reconnect before verifying results.") from None
        return {**value, "cancel_requested": True}

    def preserve(self):
        with self.lock:
            value = self.current()
            if self.marker.exists() and (not value.get("job") or not value.get("project_id")):
                raise Fault("training_owner", "Training journal is missing. Preserve the VM and recover its worker identity before releasing admission.")
            pid = value.get("pid")
            if group_active(pid, ignore_current=pid == os.getpid()) or value.get("busy") and pid is None:
                raise Fault("training_busy", "Wait for the worker and all trainer children to exit before verifying results.")
            if value.get("project_id"):
                project = self.project(value["project_id"])
                root = Path(project["run"])
                rows = verify_results(root)
                atomic_json(self.projects / (project["id"]+".receipt"), {"files": rows})
            value.update(busy=False, preserved=True)
            if value.get("phase") not in {"ready", "complete", "error"}: value["phase"] = "stopped"
            atomic_json(self.file, value)
            self.marker.unlink(missing_ok=True)
            return value

    def export(self, project_id):
        self.require_safe()
        project = self.project(project_id)
        root = Path(project["run"])
        # Checkpoint exports are atomic only after their parent step is published.
        candidates = list((root / "export").glob("*-comfy.safetensors"))
        for folder in sorted((root / "checkpoints").glob("step_*")):
            if re.fullmatch(r"step_\d{8}", folder.name) and (folder / "state.json").is_file():
                candidates.extend((folder / "export").glob("*-comfy.safetensors"))
        if not candidates: raise Fault("training_export", "No ComfyUI adapter export exists yet.")
        source = candidates[-1]
        if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()): raise Fault("training_export", "Invalid adapter path.")
        checksum = digest(source)
        target = self.paths.models / "loras" / (project["settings"]["name"]+"_"+checksum[:12]+".safetensors")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if digest(target) != checksum: raise Fault("training_export", "Adapter destination conflict.")
        else:
            temporary = target.with_suffix(".part")
            shutil.copyfile(source, temporary)
            with temporary.open("rb+") as stream: os.fsync(stream.fileno())
            if digest(temporary) != checksum: raise Fault("training_export", "Adapter copy checksum failed.")
            os.replace(temporary, target)
        return {"path": str(target), "sha256": checksum, "verified": True}


def verify_results(root):
    rows = []
    if root.is_symlink(): raise Fault("training_preserve", "Training output root cannot be a link.")
    if not root.exists(): return rows
    for directory, dirs, names in os.walk(root):
        if any((Path(directory)/d).is_symlink() for d in dirs): raise Fault("training_preserve", "Training output contains a link.")
        for name in sorted(names):
            file = Path(directory)/name
            if not file.is_file() or file.is_symlink(): raise Fault("training_preserve", "Training output must contain regular files.")
            before = file.stat()
            with file.open("rb+") as stream: os.fsync(stream.fileno())
            checksum = digest(file)
            after = file.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns): raise Fault("training_preserve", "Training output changed during verification.")
            rows.append({"name": file.relative_to(root).as_posix(), "size": after.st_size, "sha256": checksum})
            if len(rows) > 10000: raise Fault("training_preserve", "Training output exceeds the verification limit.")
    return rows
