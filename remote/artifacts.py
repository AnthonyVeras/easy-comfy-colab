"""Deterministic private runtime artifacts with compatibility and last-good gates."""
from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import configparser
import re
import shutil
import stat
import sys
import sysconfig
import tarfile
import tempfile

try:
    from .common import Fault, atomic_json, digest, read_json
except ImportError:
    from common import Fault, atomic_json, digest, read_json

COMPONENTS = ("ComfyUI", "venv", "mcp-venv", "locks")
COMFY_DATA = {"models", "input", "output", "outputs", "user", "temp", "logs"}
EXCLUDED = {"__pycache__", ".cache", ".env", "credentials.json", "token.json", "tokens.json", "secrets.json"}


def compatibility():
    versions = {}
    for name in ("torch", "torchvision", "torchaudio"):
        try: versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name] = None
    return {"python": list(sys.version_info[:2]), "abi": sysconfig.get_config_var("SOABI"), "machine": platform.machine(), "libc": list(platform.libc_ver()), "torch": versions}


def recipe():
    home = Path(__file__).parent
    manifest = read_json(home / "runtime.json")
    # The companion is uploaded fresh; only installation inputs and installed guard affect the archive.
    inputs = {key: manifest[key] for key in ("install_revision", "core", "frontend", "manager", "mcp", "comfy_cli", "resolver", "packages")}
    inputs["artifact_schema"] = 2
    inputs["guard"] = {name: digest(home / name) for name in ("admission.py", "gate.py")}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def files(root):
    def inspected(path):
        try: return path.lstat()
        except OSError: raise Fault("unsafe_artifact", "A runtime path could not be inspected; the prior artifact remains selected.") from None
    def unreadable(error): raise Fault("unsafe_artifact", "A runtime directory could not be read; the prior artifact remains selected.")
    for component in COMPONENTS:
        source = root / component
        if not stat.S_ISDIR(inspected(source).st_mode): raise Fault("runtime_incomplete", "Runtime components are incomplete or linked; no artifact was saved.")
        for directory, dirs, names in os.walk(source, followlinks=False, onerror=unreadable):
            location = Path(directory)
            # Names such as transformers/models are dependency source, not Comfy model data.
            direct_data = COMFY_DATA if component == "ComfyUI" and location == source else set()
            git_metadata = {"hooks", "logs"} if location == source / ".git" else set()
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED | direct_data | git_metadata and not d.startswith(".env"))
            for name in dirs:
                mode = inspected(Path(directory) / name).st_mode
                if stat.S_ISLNK(mode):
                    if name == "lib64" and component in {"venv", "mcp-venv"}: continue
                    raise Fault("unsafe_artifact", "A runtime directory contains an unsupported external link.")
                if not stat.S_ISDIR(mode): raise Fault("unsafe_artifact", "A runtime directory changed during preservation.")
            for name in sorted(names):
                if name in EXCLUDED or name.startswith(".env") or name.endswith((".pyc", ".log", ".part", ".partial")): continue
                file = Path(directory) / name
                mode = inspected(file).st_mode
                if stat.S_ISLNK(mode):
                    if component in {"venv", "mcp-venv"} and file.parent.name == "bin" and name.startswith("python"): continue
                    raise Fault("unsafe_artifact", "A runtime file contains an unsupported external link.")
                if not stat.S_ISREG(mode): raise Fault("unsafe_artifact", "A runtime file contains an unsupported device or changed during preservation.")
                if file.name == "config" and file.parent.name == ".git":
                    config = configparser.RawConfigParser()
                    try:
                        config.read(file, encoding="utf-8")
                        for section in config.sections():
                            for key, value in config.items(section):
                                if key not in {"repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase", "precomposeunicode", "url", "fetch", "remote", "merge"}:
                                    raise ValueError()
                                if key == "url" and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value): raise ValueError()
                    except (ValueError, configparser.Error): raise Fault("unsafe_artifact", "Repository configuration contains unsupported credentials or local settings.") from None
                yield file


def source_signature(root):
    h = hashlib.sha256()
    for file in sorted(files(root), key=lambda p: p.relative_to(root).as_posix()):
        h.update(file.relative_to(root).as_posix().encode()); h.update(digest(file).encode()); h.update(b"x" if file.stat().st_mode & 0o111 else b"-")
    h.update(json.dumps(read_json(root / "state/nodes.json", {}), sort_keys=True, separators=(",", ":")).encode())
    return h.hexdigest()


def inherited(root):
    result = {}
    for component in ("venv", "mcp-venv"):
        values = read_json(root / "locks" / (component + "-inherited.json"), {})
        for name, version in values.items():
            if name in result and result[name] != version: raise Fault("runtime_incompatible", "Runtime components require conflicting inherited packages.")
            result[name] = version
    return result


def inherited_compatible(versions):
    try: return all(importlib.metadata.version(name) == version for name, version in versions.items())
    except importlib.metadata.PackageNotFoundError: return False


def archive_bytes(root, destination):
    total = 0
    with destination.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=1) as compressed, tarfile.open(fileobj=compressed, mode="w") as archive:
        for file in sorted(files(root), key=lambda p: p.relative_to(root).as_posix()):
            before = file.stat()
            info = archive.gettarinfo(str(file), arcname=file.relative_to(root).as_posix())
            info.uid = info.gid = 0; info.uname = info.gname = ""; info.mtime = 0
            info.mode = 0o755 if before.st_mode & 0o111 else 0o644
            with file.open("rb") as stream: archive.addfile(info, stream)
            after = file.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns): raise Fault("runtime_changed", "Runtime changed during preservation; wait for installations and retry.")
            total += before.st_size
    return total


def verify_pointer(paths, pointer):
    if not isinstance(pointer, dict): raise Fault("invalid_artifact", "Runtime artifact manifest is invalid.")
    checksum = pointer.get("sha256", "")
    if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum) or pointer.get("archive") != checksum + ".tar.gz":
        raise Fault("invalid_artifact", "Runtime artifact manifest is invalid.")
    archive = paths.artifacts / pointer["archive"]
    if not archive.is_file() or archive.is_symlink() or archive.stat().st_size != pointer.get("bytes") or digest(archive) != checksum:
        raise Fault("artifact_corrupt", "Runtime preservation could not be verified; the VM remains active.")
    return archive


def save(paths, validate=lambda root: None):
    validate(paths.root)
    signature = source_signature(paths.root)
    base, current_recipe = compatibility(), recipe()
    selected_path = paths.artifacts / "selected.json"
    selected = read_json(selected_path, {})
    if selected.get("source_signature") == signature and selected.get("base") == base and selected.get("recipe") == current_recipe:
        verify_pointer(paths, selected)
        return {"verified": True, "changed": False, "manifest": selected}
    paths.artifacts.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(paths.root).free < 2 * 1024**3: raise Fault("disk_full", "Free at least 2 GiB before saving the runtime.")
    with tempfile.TemporaryDirectory(prefix="artifact-", dir=paths.root) as folder:
        archive = Path(folder) / "runtime.tar.gz"
        unpacked = archive_bytes(paths.root, archive)
        checksum = digest(archive)
        target = paths.artifacts / (checksum + ".tar.gz")
        partial = target.with_suffix(".partial")
        if shutil.disk_usage(paths.artifacts).free < archive.stat().st_size + 1024**3: raise Fault("drive_full", "The Drive has insufficient space for runtime preservation.")
        shutil.copyfile(archive, partial)
        if digest(partial) != checksum: raise Fault("artifact_corrupt", "Runtime artifact copy failed verification; the prior artifact remains selected.")
        if source_signature(paths.root) != signature or compatibility() != base: raise Fault("runtime_changed", "Runtime changed during preservation; the prior artifact remains selected.")
        partial.replace(target)
        manifest = {"schema": 1, "base": base, "inherited": inherited(paths.root), "recipe": current_recipe, "source_signature": signature, "archive": target.name, "sha256": checksum, "bytes": target.stat().st_size, "unpacked_bytes": unpacked, "node_manifest": read_json(paths.state / "nodes.json", {})}
        if selected:
            verify_pointer(paths, selected)
            atomic_json(paths.artifacts / "last-good.json", selected)
        atomic_json(selected_path, manifest)
        atomic_json(paths.state / "runtime-preserved.json", manifest)
        # Keep only archives previously selected by this product, never arbitrary Drive files.
        retained = {manifest["archive"], selected.get("archive")}
        known = read_json(paths.artifacts / "history.json", [])
        for name in known:
            if isinstance(name, str) and name not in retained and re.fullmatch(r"[0-9a-f]{64}\.tar\.gz", name):
                (paths.artifacts / name).unlink(missing_ok=True)
        atomic_json(paths.artifacts / "history.json", sorted(x for x in retained if x))
    return {"verified": True, "changed": True, "manifest": manifest}


def safe_extract(archive, destination, declared):
    total, seen = 0, set()
    for member in archive:
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts or ":" in member.name or "\\" in member.name or not relative.parts or relative.parts[0] not in COMPONENTS or member.name in seen or not member.isfile() or member.size < 0:
            raise Fault("unsafe_artifact", "Runtime artifact contains unsupported paths, links or devices.")
        seen.add(member.name); total += member.size
        if total > declared: raise Fault("unsafe_artifact", "Runtime artifact exceeds its declared extraction size.")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.extractfile(member) as stream, target.open("wb") as out: shutil.copyfileobj(stream, out, 1024*1024)
        target.chmod(member.mode & 0o755)
    if total != declared: raise Fault("artifact_corrupt", "Runtime artifact extraction size does not match its manifest.")


def restore_candidate(paths, selected, validate, pointer, previous_error):
    required = selected.get("unpacked_bytes")
    if type(required) is not int or required <= 0 or required > 40 * 1024**3: raise Fault("invalid_artifact", "Runtime artifact size is invalid.")
    source = verify_pointer(paths, selected)
    if shutil.disk_usage(paths.root).free < required + source.stat().st_size + 1024**3: raise Fault("disk_full", "The VM has insufficient space to restore this runtime.")
    moved = []
    with tempfile.TemporaryDirectory(prefix="restore-", dir=paths.root) as folder:
        stage = Path(folder)
        local = stage / "runtime.tar.gz"
        shutil.copyfile(source, local)
        if digest(local) != selected["sha256"]: raise Fault("artifact_corrupt", "The runtime copy changed while restoring.")
        try:
            with tarfile.open(local, "r:gz") as archive: safe_extract(archive, stage, required)
        except (tarfile.TarError, EOFError, gzip.BadGzipFile):
            raise Fault("artifact_corrupt", "The runtime archive could not be extracted; preserve it and try a verified last-good artifact.") from None
        # A detached checkout can have an empty refs directory. Files-only archives omit it,
        # but Git still requires refs/ and objects/ to recognize the extracted repository.
        git = stage / "ComfyUI/.git"
        if (git / "HEAD").is_file():
            for name in ("refs", "objects"): (git / name).mkdir(exist_ok=True)
        for component in ("venv", "mcp-venv"):
            binary = stage / component / "bin/python"
            binary.parent.mkdir(parents=True, exist_ok=True)
            if not binary.exists(): binary.symlink_to(sys.executable)
            for name in ("python3", f"python{sys.version_info.major}.{sys.version_info.minor}"):
                alias = binary.with_name(name)
                if not alias.exists(): alias.symlink_to("python")
        validate(stage)
        try:
            for component in COMPONENTS:
                (stage / component).rename(paths.root / component); moved.append(component)
            atomic_json(paths.state / "runtime-preserved.json", selected)
            atomic_json(paths.state / "nodes.json", selected.get("node_manifest", {}))
            recovery = {"pointer": pointer, "sha256": selected["sha256"], "fallback": pointer == "last-good.json", "previous_error": previous_error}
            atomic_json(paths.state / "runtime-restored.json", recovery)
            if pointer == "last-good.json":
                # Promote only after checksum, staged extraction and runtime validation have succeeded.
                atomic_json(paths.artifacts / "selected.json", selected)
            atomic_json(paths.state / "installed.json", {"recipe": recipe(), "base": compatibility()})
        except Exception:
            for component in reversed(moved): (paths.root / component).rename(stage / component)
            raise
    return True


def restore(paths, validate=lambda root: None):
    if any((paths.root / c).exists() for c in COMPONENTS): return False
    first_error = None
    for pointer in ("selected.json", "last-good.json"):
        try:
            selected = read_json(paths.artifacts / pointer, None)
            if selected is None: continue
            if not isinstance(selected, dict) or selected.get("schema") != 1:
                raise Fault("invalid_artifact", "Runtime artifact manifest is invalid.")
            if selected.get("base") != compatibility() or selected.get("recipe") != recipe(): continue
            if not isinstance(selected.get("inherited", {}), dict): raise Fault("invalid_artifact", "Runtime dependency compatibility is invalid.")
            if not inherited_compatible(selected.get("inherited", {})): continue
            return restore_candidate(paths, selected, validate, pointer, first_error.code if first_error else None)
        except Fault as error:
            if error.code in {"disk_full", "drive_full"}: raise
            if first_error is None: first_error = error
    if first_error: raise first_error
    return False
