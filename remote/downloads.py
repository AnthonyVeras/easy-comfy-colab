"""Bounded VM downloads; credentials live only in worker memory."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

try:
    from .common import Fault, atomic_json, digest, model_path, read_json
except ImportError:
    from common import Fault, atomic_json, digest, model_path, read_json


def source_url(value, filename=None):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise Fault("invalid_url", "Paste a Hugging Face file URL or Civitai version download URL.")
    try:
        parts = urlsplit(value)
        decoded = urlsplit(unquote(value))
        if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password or decoded.username or decoded.password or parts.fragment:
            raise ValueError()
        host = parts.hostname
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if any(k.lower() in {"token", "access_token", "api_key", "apikey", "authorization"} for k in query):
            raise ValueError()
        decoded_path = unquote(parts.path)
        if "\\" in decoded_path or any(p in {".", ".."} for p in decoded_path.split("/")):
            raise ValueError()
        if host == "huggingface.co":
            segments = parts.path.split("/")
            if len(segments) < 6 or segments[3] not in {"blob", "resolve"} or set(query) - {"download"}:
                raise ValueError()
            segments[3] = "resolve"
            name = filename or unquote(segments[-1])
            if "/" in name: raise ValueError()
            url = urlunsplit(("https", "huggingface.co", "/".join(segments), "", ""))
        elif host == "civitai.com" and re.fullmatch(r"/api/download/models/[0-9]+", parts.path):
            if not filename or set(query) - {"type", "format", "size", "fp"}: raise ValueError()
            name, url = filename, urlunsplit(("https", "civitai.com", parts.path, parts.query, ""))
        else: raise ValueError()
    except (ValueError, TypeError):
        raise Fault("invalid_url", "Use a direct HTTPS model file URL without embedded credentials or traversal.") from None
    return url, name, host


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def open_source(url, token, headers, timeout=30):
    original = urlsplit(url).hostname
    opener = build_opener(NoRedirect())
    for _ in range(6):
        parts = urlsplit(url)
        host = parts.hostname or ""
        allowed = host in {"huggingface.co", "civitai.com"} or any(host.endswith(suffix) for suffix in (".hf.co", ".huggingface.co", ".civitai.com", ".cloudflarestorage.com", ".amazonaws.com"))
        if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password or not allowed:
            raise Fault("unsafe_redirect", "The model origin redirected to an unsupported endpoint.")
        outgoing = dict(headers)
        if host == original and token: outgoing["Authorization"] = "Bearer " + token
        try: return opener.open(Request(url, headers=outgoing), timeout=timeout)
        except HTTPError as error:
            if error.code in {301, 302, 303, 307, 308} and error.headers.get("Location"):
                url = urljoin(url, error.headers["Location"])
                error.close()
                continue
            code = "model_auth_required" if error.code in {401, 403} else "download_failed"
            error.close()
            raise Fault(code, "Model origin refused the download; verify access, URL and credentials.") from None
    raise Fault("unsafe_redirect", "The model origin redirected too many times.")


class Downloads:
    def __init__(self, paths, invalidate=lambda p: None, opener=open_source):
        self.paths, self.invalidate, self.opener = paths, invalidate, opener
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="models")
        self.limit = threading.Semaphore(3)
        self.jobs = read_json(paths.state / "downloads.json", [])
        self.events = {}
        self.tokens = {}
        for job in self.jobs:
            if job["status"] in {"queued", "running", "cancelling"}: job["status"] = "paused"

    def persist(self):
        atomic_json(self.paths.state / "downloads.json", self.jobs[-200:])

    @property
    def busy(self):
        with self.lock: return any(j["status"] in {"queued", "running", "cancelling"} for j in self.jobs)

    def snapshot(self, offset=0, limit=50):
        with self.lock:
            rows = list(reversed(self.jobs))
            return {"items": [dict(j) for j in rows[offset:offset+limit]], "total": len(rows), "offset": offset, "limit": limit, "busy": self.busy}

    def add(self, data):
        urls = data.get("urls")
        parallel = data.get("parallel", 3)
        if not isinstance(urls, list) or not 1 <= len(urls) <= 50 or type(parallel) is not int or not 1 <= parallel <= 6:
            raise Fault("invalid_download", "Submit 1–50 URLs with 1–6 parallel downloads.")
        tokens = data.get("tokens", {})
        if not isinstance(tokens, dict) or any(k not in {"huggingface", "civitai"} or not isinstance(v, str) or len(v) > 4096 or any(ord(c) < 32 for c in v) for k, v in tokens.items()):
            raise Fault("invalid_credentials", "Select valid service credentials.")
        prepared = []
        for entry in urls:
            if isinstance(entry, str):
                url, separator, filename = entry.partition("|")
                entry = {"url": url.strip(), **({"filename": filename.strip()} if separator else {})}
            if not isinstance(entry, dict): raise Fault("invalid_download", "Each download requires a URL.")
            url, filename, host = source_url(entry.get("url"), entry.get("filename"))
            category = data.get("category")
            model_path(self.paths.models, category, filename)
            expected = entry.get("sha256")
            if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
                raise Fault("invalid_integrity", "SHA-256 must be a lowercase 64-character digest.")
            identity = hashlib.sha256(json.dumps([url, category, filename, expected]).encode()).hexdigest()
            prepared.append(dict(url=url, filename=filename, category=category, identity=identity, expected_sha256=expected, host=host))
        ids = []
        with self.lock:
            if self.busy:
                semaphore = self.limit
            else:
                semaphore = self.limit = threading.Semaphore(parallel)
            for value in prepared:
                existing = next((j for j in self.jobs if j["category"] == value["category"] and j["filename"] == value["filename"] and j["status"] in {"queued", "running", "cancelling"}), None)
                if existing:
                    if existing["identity"] != value["identity"]: raise Fault("model_conflict", "Another source already owns this model filename.")
                    ids.append(existing["id"]); continue
                if len(self.jobs) >= 200:
                    removable = next((j for j in self.jobs if j["status"] in {"complete", "cancelled", "error"}), None)
                    if removable is None: raise Fault("download_limit", "Finish or cancel existing downloads before adding more.")
                    self.jobs.remove(removable)
                job = dict(value, id=uuid.uuid4().hex, status="queued", bytes=0, total=None, sha256=None, error=None, created_at=time.time())
                self.jobs.append(job); ids.append(job["id"])
                self.tokens[job["id"]] = tokens.get("huggingface" if job["host"] == "huggingface.co" else "civitai", "")
                self.events[job["id"]] = threading.Event()
                self.executor.submit(self.run, job, semaphore)
            self.persist()
        return {"ids": ids}

    def action(self, identity, action, tokens=None):
        if tokens is not None and (not isinstance(tokens, dict) or any(k not in {"huggingface", "civitai"} or not isinstance(v, str) or len(v) > 4096 or any(ord(c) < 32 for c in v) for k, v in tokens.items())):
            raise Fault("invalid_credentials", "Select valid service credentials.")
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == identity), None)
            if not job: raise Fault("download_missing", "This download is no longer in the queue.")
            if action == "cancel":
                if job["status"] in {"queued", "running", "cancelling"}:
                    job["status"] = "cancelling"; self.events[identity].set()
            elif action == "resume":
                if job["status"] not in {"paused", "cancelled", "error"}: raise Fault("download_busy", "This download cannot be resumed yet.")
                self.tokens[identity] = (tokens or {}).get("huggingface" if job["host"] == "huggingface.co" else "civitai", "")
                self.events[identity] = threading.Event(); job.update(status="queued", error=None)
                self.executor.submit(self.run, job, self.limit)
            else: raise Fault("invalid_action", "Unknown download action.")
            self.persist()
            return {"id": identity, "status": job["status"]}

    def run(self, job, semaphore):
        try:
            with semaphore:
                event = self.events[job["id"]]
                if event.is_set(): raise Fault("cancelled", "Download cancelled; partial bytes are preserved.")
                with self.lock: job["status"] = "running"
                target = model_path(self.paths.models, job["category"], job["filename"])
                target.parent.mkdir(parents=True, exist_ok=True)
                index_path = self.paths.drive / "model-identities.json"
                with self.lock: index = read_json(index_path, {})
                relative = target.relative_to(self.paths.models).as_posix()
                if target.exists():
                    record = index.get(relative)
                    if not record or record.get("identity") != job["identity"] or digest(target) != record.get("sha256"):
                        raise Fault("model_conflict", "An existing model has a different or unverified identity; choose another filename.")
                    job.update(status="complete", bytes=target.stat().st_size, total=target.stat().st_size, sha256=record["sha256"])
                    return
                partial = target.with_name(target.name + ".part")
                metadata_path = partial.with_name(partial.name + ".json")
                if partial.is_symlink() or metadata_path.is_symlink(): raise Fault("invalid_path", "Partial files cannot be symbolic links.")
                metadata = read_json(metadata_path, {})
                if partial.exists() and metadata.get("identity") != job["identity"]:
                    raise Fault("model_conflict", "Partial bytes belong to another source; choose another filename.")
                offset = partial.stat().st_size if partial.exists() and metadata.get("etag") else 0
                headers = {"User-Agent": "EasyComfyColab2/1"}
                if offset: headers.update(Range=f"bytes={offset}-", **{"If-Range": metadata["etag"]})
                response = self.opener(job["url"], self.tokens.get(job["id"], ""), headers)
                with response:
                    status = response.status
                    etag = response.headers.get("ETag")
                    if offset and status == 206:
                        content_range = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                        if not match or int(match[1]) != offset or int(match[2])+1 != int(match[3]) or etag != metadata["etag"]:
                            raise Fault("resume_mismatch", "The origin changed; partial bytes were preserved for review.")
                    else:
                        if status != 200: raise Fault("download_failed", "The origin returned an unexpected response.")
                        offset = 0
                    length = response.headers.get("Content-Length")
                    total = offset + int(length) if length else None
                    atomic_json(metadata_path, {"identity": job["identity"], "etag": etag})
                    job.update(bytes=offset, total=total)
                    with partial.open("ab" if offset else "wb") as stream:
                        while True:
                            if event.is_set(): raise Fault("cancelled", "Download cancelled; partial bytes are preserved.")
                            chunk = response.read(1024 * 1024)
                            if not chunk: break
                            stream.write(chunk); job["bytes"] += len(chunk)
                        stream.flush(); __import__("os").fsync(stream.fileno())
                if not job["bytes"] or (total is not None and job["bytes"] != total): raise Fault("integrity_failed", "Downloaded size did not match the origin; no model was published.")
                checksum = digest(partial)
                if job["expected_sha256"] and checksum != job["expected_sha256"]:
                    raise Fault("integrity_failed", "Downloaded SHA-256 did not match; no model was published.")
                with self.lock:
                    if target.exists(): raise Fault("model_conflict", "Another file appeared at the destination; no model was overwritten.")
                    self.invalidate(relative)
                    partial.replace(target)
                    self.invalidate(relative)
                    index = read_json(index_path, {})
                    index[relative] = {"identity": job["identity"], "sha256": checksum, "size": job["bytes"]}
                    atomic_json(index_path, index)
                    metadata_path.unlink(missing_ok=True)
                    job.update(status="complete", sha256=checksum)
        except Fault as error: job.update(status="cancelled" if error.code == "cancelled" else "error", error={"code": error.code, "message": error.message})
        except Exception: job.update(status="error", error={"code": "download_failed", "message": "Download failed; partial bytes remain available for a retry."})
        finally:
            with self.lock:
                self.tokens.pop(job["id"], None)
                self.persist()
