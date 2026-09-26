"""Serviços locais, persistência e inspeção de workflows da interface v2."""

from __future__ import annotations
from i18n import LANGUAGES, tr
import json
import os
import math
import time
from pathlib import Path
import psutil
from backend import app_data_dir
from model_download import _json_request, EXTENSIONS

VERSION = "2.0.7"
DEFAULTS = {"parallel": 3, "idle_minutes": 0, "balance_alert": 20, "auto_open": True, "language": "pt-BR"}


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


class Preferences:
    def __init__(self):
        self.path = app_data_dir() / "preferences.json"
        self.values = dict(DEFAULTS)
        data = read_json(self.path, {})
        if isinstance(data, dict):
            for name, maximum in [
                ("parallel", 6),
                ("idle_minutes", 1440),
                ("balance_alert", 100000),
            ]:
                try:
                    value = float(data.get(name, self.values[name]))
                    minimum = 1 if name == "parallel" else 0
                    if math.isfinite(value) and minimum <= value <= maximum:
                        self.values[name] = (
                            int(value) if name != "balance_alert" else value
                        )
                except (ValueError, TypeError):
                    pass
            if isinstance(data.get("auto_open"), bool):
                self.values["auto_open"] = data["auto_open"]
            if isinstance(data.get("language"), str) and data["language"] in LANGUAGES:
                self.values["language"] = data["language"]

    def save(self):
        write_json(self.path, self.values)


class History:
    def __init__(self):
        self.path = app_data_dir() / "sessions.json"
        self.rows = read_json(self.path, [])
        if not isinstance(self.rows, list):
            self.rows = []

    def event(self, profile, operation, code, seconds, balance, rate, **details):
        self.rows.append(
            dict(
                time=time.time(),
                account=profile.email or profile.label,
                profile=profile.id,
                operation=operation,
                gpu=profile.gpu,
                result="Concluído" if code == 0 else "Falhou",
                seconds=round(seconds, 1),
                balance=balance,
                rate=rate,
                **details,
            )
        )
        self.rows = self.rows[-2000:]
        write_json(self.path, self.rows)


class SessionLedger:
    """Tempo observado e integral da taxa da conta; não é uma fatura por GPU."""

    def __init__(self):
        self.path = app_data_dir() / "active-sessions.json"
        self.active = read_json(self.path, {})
        if not isinstance(self.active, dict):
            self.active = {}

    def observe(self, profile, snapshot, now=None):
        now = time.time() if now is None else now
        if not snapshot.status_known:
            return None
        item = self.active.get(profile.id)
        if snapshot.session_exists:
            if item is None:
                item = dict(
                    started=now,
                    last=now,
                    cost=0.0,
                    rate=snapshot.rate,
                    measured=0,
                    gaps=0,
                )
                self.active[profile.id] = item
            elapsed = max(0, now - item["last"])
            if elapsed <= 90 and item["rate"] is not None:
                item["cost"] += elapsed * item["rate"] / 3600
                item["measured"] += elapsed
            else:
                item["gaps"] += elapsed
            item.update(last=now, rate=snapshot.rate)
            write_json(self.path, self.active)
            return item
        return None

    def finish(self, profile):
        item = self.active.pop(profile.id, None)
        if item:
            write_json(self.path, self.active)
            item["duration"] = max(0, time.time() - item["started"])
        return item


def local_metrics():
    ram = psutil.virtual_memory()
    process = psutil.Process()
    return dict(
        cpu_percent=psutil.cpu_percent(interval=0.2),
        ram_total=ram.total,
        ram_used=ram.total - ram.available,
        ram_available=ram.available,
        app_ram=process.memory_info().rss,
        process_id=os.getpid(),
    )


def release_vram():
    queue = _json_request("queue")
    if "queue_running" not in queue or "queue_pending" not in queue:
        raise RuntimeError(
            tr("Não foi possível verificar a fila. Atualize e tente novamente.")
        )
    if queue["queue_running"] or queue["queue_pending"]:
        raise RuntimeError(tr("Aguarde a fila terminar antes de liberar a VRAM."))
    return _json_request("free", {"unload_models": True, "free_memory": True})


def check_workflow(path, info, models):
    """Percorre UI, subgrafos e formato API. Não executa o workflow."""
    data = read_json(path, None)
    if not isinstance(data, dict):
        raise ValueError(tr("Selecione um workflow JSON válido."))
    nodes = []

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("nodes"), list):
                nodes.extend(n for n in obj["nodes"] if isinstance(n, dict))
            if "class_type" in obj:
                nodes.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    subgraphs = set()
    for sub in data.get("definitions", {}).get("subgraphs", []):
        if isinstance(sub, dict):
            subgraphs.add(sub.get("id"))
    known = {m["name"].replace("\\", "/") for m in models}
    basenames = {Path(n).name for n in known}
    results = []
    seen = set()
    for node in nodes:
        kind = node.get("class_type") or node.get("type", "")
        if (
            kind not in info
            and kind not in subgraphs
            and kind not in {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}
        ):
            key = ("node", kind)
            if key not in seen:
                results.append((tr("Node ausente"), kind, tr("Instale pelo Manager na VM.")))
                seen.add(key)
        values = node.get("widgets_values", node.get("inputs", {}))

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for v in value.values():
                    yield from strings(v)
            elif isinstance(value, list):
                for v in value:
                    yield from strings(v)

        for value in strings(values):
            if (
                len(value) > 1024
                or "\n" in value
                or Path(value).suffix.lower() not in EXTENSIONS
            ):
                continue
            key = ("model", value)
            if (
                value.replace("\\", "/") not in known
                and Path(value.replace("\\", "/")).name not in basenames
                and key not in seen
            ):
                results.append(
                    (tr("Modelo ausente"), value, tr("Adicione o link na aba Modelos."))
                )
                seen.add(key)
    return results or [
        (
            tr("Verificação concluída"),
            tr("{count} nodes inspecionados", count=len(nodes)),
            tr("Nenhuma dependência ausente identificada. Não substitui um teste de execução."),
        )
    ]


def workflow_cache_selection(path, models):
    """Sugere arquivos existentes, sem adivinhar nomes ambíguos nem executar nodes."""
    data = read_json(path, None)
    if not isinstance(data, dict):
        raise ValueError(tr("Selecione um workflow JSON válido."))
    names = set()

    def strings(value):
        if isinstance(value, str):
            if len(value) <= 1024 and "\n" not in value and Path(value).suffix.lower() in EXTENSIONS:
                names.add(value.replace("\\", "/"))
        elif isinstance(value, dict):
            for item in value.values():
                strings(item)
        elif isinstance(value, list):
            for item in value:
                strings(item)

    def walk(value):
        if isinstance(value, dict):
            if "type" in value or "class_type" in value:
                if value.get("mode") in (2, 4) or value.get("type") in ("Note", "MarkdownNote"):
                    return
                strings(value.get("widgets_values", value.get("inputs", {})))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    selected, unresolved = set(), []
    for name in sorted(names):
        matches = [m for m in models if name in (m["name"], m["category"] + "/" + m["name"])]
        if not matches:
            matches = [m for m in models if Path(m["name"]).name == Path(name).name]
        if len(matches) == 1:
            selected.add(matches[0]["category"] + "/" + matches[0]["name"])
        else:
            unresolved.append(name + (tr(" (ambíguo)") if matches else tr(" (ausente)")))
    return selected, unresolved


class IdleGuard:
    """Decisão conservadora: nunca usa métricas antigas nem fila desconhecida."""

    def __init__(self):
        self.since = None

    def update(self, metrics, minutes, blocked=False, now=None):
        now = time.time() if now is None else now
        valid = (
            isinstance(metrics, dict) and 0 <= now - metrics.get("timestamp", 0) < 20
        )
        idle = (
            valid
            and metrics.get("queue_busy") is False
            and metrics.get("downloads_busy") is False
        )
        if blocked or not minutes or not idle:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= minutes * 60


def size(value):
    if value is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"
