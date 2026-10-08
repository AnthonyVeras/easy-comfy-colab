"""Explicit single-file copies from the same mounted account's legacy Drive root."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

try:
    from .common import CATEGORIES, SUFFIXES, Fault, Paths, atomic_json, digest, inside, read_json, relative_path
except ImportError:
    from common import CATEGORIES, SUFFIXES, Fault, Paths, atomic_json, digest, inside, read_json, relative_path

WORKFLOW_LIMIT = 16*1024*1024


def publish_no_replace(source, target):
    """Atomic publication must never replace a file created by another Drive writer."""
    try:
        if os.name == "nt":
            source.rename(target)  # Windows rename refuses an existing destination.
            return
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is not None:
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0: return
            code = ctypes.get_errno()
            if code == errno.EEXIST: raise FileExistsError(code, "Destination exists")
            if code not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}: raise OSError(code, "Atomic publication failed")
        # Local filesystems can provide the same atomic no-clobber property via a hard link.
        os.link(source, target, follow_symlinks=False)
        source.unlink()
    except FileExistsError:
        raise Fault("legacy_conflict", "The destination appeared during import. Choose another filename; no file was overwritten.") from None
    except OSError:
        raise Fault("atomic_publication_unavailable", "This Drive filesystem cannot safely publish without replacing existing files. Originals remain preserved.") from None


class Legacy:
    def __init__(self, paths, invalidate=lambda relative: None):
        self.paths, self.invalidate = paths, invalidate
        self.root = paths.drive.parent / "ComfyColab"

    def selection(self, kind, category=None, source_category=None):
        if kind not in {"model", "workflow"}: raise Fault("legacy_kind_invalid", "Choose models or workflows for explicit import.")
        if kind == "workflow":
            if category is not None or source_category is not None: raise Fault("legacy_kind_invalid", "Workflows do not have a model category.")
            return self.root / "user/default/workflows", self.paths.drive / "user/default/workflows", None
        if category not in CATEGORIES: raise Fault("invalid_model", "Choose a supported model category before listing legacy files.")
        source_category = source_category or category
        if source_category != category and not (category == "llm_gguf" and source_category == "LLM"):
            raise Fault("invalid_model", "The legacy category must match the selected destination category.")
        return self.root / "models" / source_category, self.paths.models / category, source_category

    def checked(self, root, value=None):
        if self.paths.drive == Paths().drive and not os.path.ismount(self.paths.drive.parents[1]):
            raise Fault("drive_required", "Mount this account's Google Drive before importing legacy files.")
        target = inside(root, value) if value is not None else root
        if value is not None and len(value.encode("utf-8")) > 1024: raise Fault("invalid_path", "Selected relative paths must fit 1,024 UTF-8 bytes.")
        parent = self.paths.drive.parent
        if not target.resolve().is_relative_to(parent.resolve()): raise Fault("invalid_path", "Legacy files must stay inside this account's mounted Drive.")
        for part in (parent, *[parent.joinpath(*target.relative_to(parent).parts[:i]) for i in range(1, len(target.relative_to(parent).parts)+1)]):
            if part.is_symlink(): raise Fault("invalid_path", "Legacy import does not accept symbolic links.")
        return target

    def list(self, kind, category=None, offset=0, limit=50):
        source, _, source_category = self.selection(kind, category)
        roots = [(source, source_category)]
        if kind == "model" and category == "llm_gguf": roots.insert(0, (self.root/"models/LLM", "LLM"))
        rows, total = [], 0
        for root, selected_category in roots:
            self.checked(root)
            for directory, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(directory)/d).is_symlink())
                for name in sorted(names):
                    file = Path(directory)/name
                    if file.is_symlink() or not file.is_file() or file.suffix.lower() not in (SUFFIXES if kind == "model" else {".json"}): continue
                    relative = file.relative_to(root).as_posix()
                    if len(relative.encode("utf-8")) > 1024: continue
                    self.checked(root, relative)
                    if offset <= total < offset+limit:
                        stat = file.stat()
                        rows.append({"id":hashlib.sha256(json.dumps([kind, selected_category, relative]).encode()).hexdigest(),
                                     "kind":kind, "category":category, "source_category":selected_category,
                                     "name":name, "relative_path":relative, "size":stat.st_size, "modified":stat.st_mtime})
                    total += 1
                    if total > 10000: raise Fault("legacy_library_limit", "This legacy category exceeds 10,000 files. Organize it before importing.")
        return {"items":rows, "total":total, "offset":offset, "limit":limit}

    @staticmethod
    def stamp(stat): return stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev

    def import_file(self, data):
        kind, category = data.get("kind"), data.get("category")
        source_root, destination_root, source_category = self.selection(kind, category, data.get("source_category"))
        relative = data.get("relative_path")
        relative_path(relative)
        source = self.checked(source_root, relative)
        if not source.is_file(): raise Fault("legacy_source_missing", "The selected legacy file is missing. Refresh the list.")
        allowed = SUFFIXES if kind == "model" else {".json"}
        if source.suffix.lower() not in allowed: raise Fault("invalid_model" if kind == "model" else "invalid_workflow", "Choose a supported model or JSON workflow file.")
        name = data.get("destination_name", source.name)
        parsed = relative_path(name)
        if len(parsed.parts) != 1 or len(name.encode("utf-8")) > 255 or Path(name).suffix.lower() != source.suffix.lower():
            raise Fault("invalid_path", "Choose a basename up to 255 UTF-8 bytes and preserve the selected file extension.")
        self.checked(destination_root)
        destination_root.mkdir(parents=True, exist_ok=True)
        target = self.checked(destination_root, name)
        before = source.stat()
        if kind == "workflow" and before.st_size > WORKFLOW_LIMIT: raise Fault("workflow_too_large", "Legacy workflows must fit the 16 MiB JSON limit.")
        if shutil.disk_usage(destination_root).free < before.st_size+64*1024*1024: raise Fault("drive_full", "The Drive has insufficient free space to copy this legacy file.")
        fd, temp_name = tempfile.mkstemp(prefix=".legacy-", suffix=".partial", dir=destination_root)
        partial = Path(temp_name)
        checksum = hashlib.sha256(); copied = 0
        try:
            with os.fdopen(fd, "wb") as outgoing, source.open("rb") as incoming:
                if self.stamp(os.fstat(incoming.fileno())) != self.stamp(before): raise Fault("legacy_source_changed", "The legacy source changed before copying. Refresh and retry.")
                while chunk := incoming.read(4*1024*1024):
                    outgoing.write(chunk); checksum.update(chunk); copied += len(chunk)
                    if copied > before.st_size: raise Fault("legacy_source_changed", "The legacy source changed during copying. Refresh and retry.")
                outgoing.flush(); os.fsync(outgoing.fileno())
            checksum = checksum.hexdigest()
            self.checked(source_root, relative); self.checked(destination_root, name)
            if copied != before.st_size or self.stamp(source.stat()) != self.stamp(before) or digest(source) != checksum:
                raise Fault("legacy_source_changed", "The legacy source changed during copying. Refresh and retry.")
            if digest(partial) != checksum: raise Fault("integrity_failed", "The copied file failed SHA-256 verification; the source remains preserved.")
            if kind == "workflow":
                try:
                    workflow = json.loads(partial.read_bytes(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(workflow, dict) or not workflow: raise ValueError()
                except (ValueError, UnicodeError): raise Fault("invalid_workflow", "The selected legacy workflow is not a valid JSON object.") from None
            unchanged = False
            if target.exists():
                if not target.is_file() or target.stat().st_size != copied or digest(target) != checksum:
                    raise Fault("legacy_conflict", "This destination filename contains different content. Choose another filename; no file was overwritten.")
                unchanged = True
            else: publish_no_replace(partial, target)
            final = self.checked(destination_root, name)
            if not final.is_file() or final.stat().st_size != copied or digest(final) != checksum:
                raise Fault("integrity_failed", "The published destination failed verification; the legacy original remains preserved.")
            new_relative = (category+"/"+name) if kind == "model" else name
            if kind == "model":
                if not unchanged: self.invalidate(new_relative)
                index_path = self.paths.drive/"model-identities.json"
                self.checked(self.paths.drive, "model-identities.json")
                index = read_json(index_path,{})
                index[new_relative] = {"identity":hashlib.sha256(json.dumps(["legacy",source_category,relative,checksum]).encode()).hexdigest(),"sha256":checksum,"size":copied}
                atomic_json(index_path,index)
            return {"imported":True,"kind":kind,"category":category,"name":name,"relative_path":new_relative,
                    "newrelative_path":new_relative,"remote_path":str(target),"size":copied,"sha256":checksum,
                    "unchanged":unchanged,"source_preserved":True}
        finally: partial.unlink(missing_ok=True)
