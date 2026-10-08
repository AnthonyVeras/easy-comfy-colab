"""Conservative, nonblocking sampling of this PC and controller process."""
from __future__ import annotations

import threading
import time


UNKNOWN = {"cpu_percent": None, "ram_used": None, "ram_total": None,
           "controller_rss": None, "controller_cpu_percent": None, "sampled_at": None}


class LocalMonitor:
    def __init__(self, backend=None, clock=time.monotonic):
        self.backend = backend
        self.clock = clock
        self.process = None
        self.last_sample = None
        self.primed = False
        self.previous_cpu = None
        self.value = dict(UNKNOWN)
        self.lock = threading.Lock()

    def sample(self):
        with self.lock:
            now = self.clock()
            if self.last_sample is not None and now - self.last_sample < 5:
                return dict(self.value)
            self.last_sample = now
            try:
                if self.backend is None:
                    import psutil
                    self.backend = psutil
                if self.process is None:
                    self.process = self.backend.Process()
                ram = self.backend.virtual_memory()
                cpu_times = self.backend.cpu_times()
                cpu = None
                if self.previous_cpu is not None:
                    deltas = {name: max(0, getattr(cpu_times, name) - getattr(self.previous_cpu, name)) for name in cpu_times._fields}
                    # Linux guest counters are already included in user/nice. Windows has none.
                    total_delta = sum(deltas.values()) - deltas.get("guest", 0) - deltas.get("guest_nice", 0)
                    idle_delta = deltas.get("idle", 0) + deltas.get("iowait", 0)
                    if total_delta > 0:
                        cpu = round(max(0, min(100, (total_delta-idle_delta)/total_delta*100)), 1)
                self.previous_cpu = cpu_times
                process_cpu = self.process.cpu_percent(interval=None)
                self.value = {"cpu_percent": cpu,
                              "ram_used": ram.total - ram.available, "ram_total": ram.total,
                              "controller_rss": self.process.memory_info().rss,
                              "controller_cpu_percent": process_cpu if self.primed else None,
                              "sampled_at": time.time()}
                # psutil's first CPU sample has no elapsed baseline. Keep it unknown.
                self.primed = True
            except Exception:
                self.value = dict(UNKNOWN)
                self.primed = False
                self.previous_cpu = None
            return dict(self.value)
