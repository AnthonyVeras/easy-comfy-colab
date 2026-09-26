"""Cópias descartáveis na VM e pré-leitura limitada, sem alterar os modelos do Drive."""

from __future__ import annotations
import copy
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

GIB = 1024 ** 3
CHUNK = 8 * 1024 ** 2
ACTIVE = {"queued", "copying", "warming", "waiting"}


class Cancelled(Exception):
    pass


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class ModelCache:
    def __init__(self, source, categories, suffixes, *, root=None, queue_status=lambda: False):
        self.source = Path(source).resolve()
        self.root = Path(root or "/content/comfy-colab/model-cache")
        self.settings_path = Path(source).parent / "user/comfy-colab-cache.json"
        self.manifest_path = self.root / "manifest.json"
        self.categories, self.suffixes = categories, suffixes
        self.queue_status = queue_status
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.worker = None
        self.started = 0
        self.manifest = read_json(self.manifest_path, {})
        if not isinstance(self.manifest, dict):
            self.manifest = {}
        self.settings = dict(enabled=True, warm_ram=False, ram_gib=32, models=[])
        self.state = dict(status="idle", items=[], error="", seconds=0, hits=0, last_loaded="")
        try:
            self.settings = self.validate(read_json(self.settings_path, {}))
        except ValueError as exc:
            self.state["error"] = str(exc)

    def relative(self, value):
        if not isinstance(value, str) or len(value) > 1024 or "\\" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Caminho de modelo inválido.")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
            raise ValueError("Escolha um arquivo dentro da biblioteca de modelos.")
        if path.parts[0] not in self.categories or path.suffix.lower() not in self.suffixes:
            raise ValueError("Categoria ou formato não aceito pelo cache.")
        return path.as_posix()

    def paths(self, relative):
        relative = self.relative(relative)
        source = self.source / relative
        target = self.root / "models" / relative
        if not source.resolve().is_relative_to(self.source) or not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Arquivo ou link fora da biblioteca de modelos.")
        return source, target

    def validate(self, data):
        if not isinstance(data, dict):
            raise ValueError("Configuração de cache inválida.")
        result = dict(self.settings)
        for key in ("enabled", "warm_ram"):
            if key in data:
                if not isinstance(data[key], bool):
                    raise ValueError("Use verdadeiro ou falso para as opções do cache.")
                result[key] = data[key]
        if "ram_gib" in data:
            if type(data["ram_gib"]) is not int or not 1 <= data["ram_gib"] <= 128:
                raise ValueError("Limite de pré-leitura: de 1 a 128 GiB.")
            result["ram_gib"] = data["ram_gib"]
        if "models" in data:
            if not isinstance(data["models"], list) or len(data["models"]) > 100:
                raise ValueError("Escolha até 100 modelos por preparação.")
            result["models"] = list(dict.fromkeys(self.relative(v) for v in data["models"]))
        return result

    @property
    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(dict(self.state, settings=self.settings, busy=self.busy))
            if self.busy:
                result["seconds"] = round(time.monotonic() - self.started, 1)
            return result

    def configure(self, data):
        with self.lock:
            settings = self.validate(data)
            if self.busy and settings["enabled"]:
                raise ValueError("Cancele ou aguarde a preparação antes de alterar as opções.")
            write_json(self.settings_path, settings)
            self.settings = settings
            if not settings["enabled"]:
                self.cancelled.set()
            return self.snapshot()

    def start(self, data=None):
        with self.lock:
            if self.busy:
                raise ValueError("Já existe uma preparação em andamento.")
            if data is not None:
                self.configure(data)
            if not self.settings["enabled"] or not self.settings["models"]:
                return self.snapshot()
            self.cancelled.clear()
            self.started = time.monotonic()
            self.state.update(status="queued", error="", seconds=0,
                              items=[dict(path=p, status="queued", bytes=0, total=0, error="")
                                     for p in self.settings["models"]])
            self.worker = threading.Thread(target=self._run, args=(dict(self.settings),), daemon=True)
            self.worker.start()
            return self.snapshot()

    def cancel(self):
        self.cancelled.set()
        return self.snapshot()

    def clear(self):
        with self.lock:
            if self.busy or self.queue_status() is not False:
                raise ValueError("Aguarde as gerações e cancele a preparação antes de limpar o cache.")
            for relative in list(self.manifest):
                _, target = self.paths(relative)
                target.unlink(missing_ok=True)
                self.manifest.pop(relative)
            write_json(self.manifest_path, self.manifest)
            self.state.update(status="idle", items=[], error="", last_loaded="", hits=0, seconds=0)
            return self.snapshot()

    def update(self, item, **values):
        with self.lock:
            item.update(values)

    def checkpoint(self, item, stage):
        if self.cancelled.is_set():
            raise Cancelled()
        # Pausa o I/O entre blocos durante gerações; nunca trava a fila do ComfyUI.
        while self.queue_status() is not False:
            self.update(item, status="waiting")
            if self.cancelled.wait(0.5):
                raise Cancelled()
        self.update(item, status=stage)

    @staticmethod
    def fingerprint(path):
        stat = path.stat()
        if not path.is_file() or stat.st_size <= 0:
            raise ValueError("Modelo vazio ou indisponível.")
        return dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    def valid(self, relative, source, target):
        with self.lock:
            record = self.manifest.get(relative)
        try:
            return bool(record and record == self.fingerprint(source)
                        and target.is_file() and target.stat().st_size == record["size"])
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def resolve(self, original):
        """Ponto único usado pelos loaders, inclusive aliases GGUF/unet/clip."""
        if not original or not self.settings["enabled"]:
            return original
        try:
            source = Path(original).resolve()
            relative = source.relative_to(self.source).as_posix()
            _, target = self.paths(relative)
            if self.valid(relative, source, target):
                with self.lock:
                    self.state["hits"] += 1
                    self.state["last_loaded"] = relative
                return str(target)
        except (OSError, ValueError):
            pass
        return original

    def invalidate(self, path):
        try:
            relative = Path(path).resolve().relative_to(self.source).as_posix()
        except ValueError:
            return
        with self.lock:
            if relative in self.manifest:
                self.manifest.pop(relative)
                write_json(self.manifest_path, self.manifest)

    def copy_model(self, item):
        partial = None
        try:
            self.checkpoint(item, "copying")
            source, target = self.paths(item["path"])
            fingerprint = self.fingerprint(source)
            self.update(item, total=fingerprint["size"])
            if not self.valid(item["path"], source, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(target.name + ".part")
                with source.open("rb") as reader, partial.open("wb") as writer:
                    while True:
                        self.checkpoint(item, "copying")
                        if shutil.disk_usage(self.root).free < 2 * GIB:
                            raise ValueError("Disco da VM cheio. A cópia foi interrompida; o Drive foi preservado.")
                        chunk = reader.read(CHUNK)
                        if not chunk:
                            break
                        writer.write(chunk)
                        self.update(item, bytes=item["bytes"] + len(chunk))
                    writer.flush()
                    os.fsync(writer.fileno())
                if self.cancelled.is_set():
                    raise Cancelled()
                if self.fingerprint(source) != fingerprint or partial.stat().st_size != fingerprint["size"]:
                    raise ValueError("O modelo mudou durante a cópia. Prepare novamente.")
                partial.replace(target)
                with self.lock:
                    self.manifest[item["path"]] = fingerprint
                    write_json(self.manifest_path, self.manifest)
            self.update(item, status="cached", bytes=fingerprint["size"])
        except Cancelled:
            self.update(item, status="cancelled")
        except Exception as exc:
            self.update(item, status="error", error=str(exc)[:240])
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)

    def warm(self, settings):
        import psutil
        ram = psutil.virtual_memory()
        reserve = max(8 * GIB, int(ram.total * 0.25))
        budget = min(settings["ram_gib"] * GIB, max(0, ram.available - reserve))
        spent = 0
        for item in self.state["items"]:
            if item["status"] != "cached":
                continue
            if item["total"] > budget - spent:
                self.update(item, note="Pré-leitura omitida: limite de RAM. O cache em disco está pronto.")
                continue
            _, target = self.paths(item["path"])
            read = 0
            try:
                with target.open("rb") as reader:
                    while True:
                        self.checkpoint(item, "warming")
                        if psutil.virtual_memory().available < reserve:
                            self.update(item, status="cached", note="Pré-leitura interrompida para reservar RAM.")
                            break
                        chunk = reader.read(CHUNK)
                        if not chunk:
                            self.update(item, status="warm", warm_bytes=read)
                            break
                        read += len(chunk)
                        self.update(item, warm_bytes=read)
                spent += read
            except Cancelled:
                self.update(item, status="cached", note="Pré-leitura cancelada; cópia em disco preservada.")
                raise

    def _run(self, settings):
        started = time.monotonic()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            required = 0
            for item in self.state["items"]:
                source, target = self.paths(item["path"])
                fingerprint = self.fingerprint(source)
                self.update(item, total=fingerprint["size"])
                if not self.valid(item["path"], source, target):
                    required += fingerprint["size"]
            if required + 5 * GIB > shutil.disk_usage(self.root).free:
                raise ValueError("Espaço insuficiente no disco da VM. Escolha menos modelos (reserva de 5 GiB).")
            self.state["status"] = "copying"
            # ponytail: dois leitores do Drive; aumentar só após medir ganho real.
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(self.copy_model, self.state["items"]))
            if self.cancelled.is_set():
                raise Cancelled()
            if settings["warm_ram"]:
                self.state["status"] = "warming"
                self.warm(settings)
            self.state["status"] = "error" if any(i["status"] == "error" for i in self.state["items"]) else "complete"
        except Cancelled:
            self.state["status"] = "cancelled"
        except Exception as exc:
            self.state.update(status="error", error=str(exc)[:240])
        finally:
            with self.lock:
                for item in self.state["items"]:
                    if item["status"] in ACTIVE:
                        item["status"] = "cancelled" if self.cancelled.is_set() else "error"
                self.state["seconds"] = round(time.monotonic() - started, 1)


def install_resolver(folder_paths, cache):
    original = folder_paths.get_full_path

    def get_full_path(folder_name, filename):
        return cache.resolve(original(folder_name, filename))

    folder_paths.get_full_path = get_full_path
