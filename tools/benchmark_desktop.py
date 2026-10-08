"""Measure a release desktop process tree with isolated, empty application data."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import psutil


def process_metrics(root_pid: int, known: dict[int, psutil.Process]):
    try:
        root = psutil.Process(root_pid)
        known[root.pid] = root
        for child in root.children(recursive=True):
            known[child.pid] = child
    except psutil.Error:
        pass
    private = rss = cpu_seconds = 0
    live = []
    for pid, process in list(known.items()):
        try:
            info = process.memory_info()
            private += info.private if hasattr(info, "private") else process.memory_full_info().uss
            rss += info.rss
            cpu = process.cpu_times()
            cpu_seconds += cpu.user + cpu.system
            live.append({"pid": pid, "name": process.name()})
        except psutil.Error:
            continue
    return {"private_bytes": private, "working_set_bytes": rss,
            "cpu_seconds": cpu_seconds, "processes": live}


def summarize(samples, logical_cpus: int, elapsed: float):
    if not samples or elapsed <= 0 or logical_cpus < 1:
        raise ValueError("A benchmark needs samples and positive elapsed time/CPU count")
    # Each process delta is sampled separately, including children born during measurement.
    used = sum(sample.get("cpu_delta", 0) for sample in samples)
    return {
        "private_mib_mean": sum(s["private_bytes"] for s in samples) / len(samples) / 2**20,
        "private_mib_peak": max(s["private_bytes"] for s in samples) / 2**20,
        "working_set_mib_mean": sum(s["working_set_bytes"] for s in samples) / len(samples) / 2**20,
        "machine_cpu_percent": used / elapsed / logical_cpus * 100,
        "sample_seconds": elapsed,
        "process_names": sorted({p["name"] for s in samples for p in s["processes"]}),
    }


def budget_passed(result):
    return (result["private_mib_peak"] <= 250
            and result["machine_cpu_percent"] <= 1
            and result["opening_seconds"] <= 3)


def cpu_delta(process, previous, started_at):
    cpu = process.cpu_times()
    current = cpu.user + cpu.system
    baseline = previous.get(process.pid, 0 if process.create_time() >= started_at else current)
    previous[process.pid] = current
    return max(0, current - baseline)


def measure(executable: Path, seconds: int, settle: int, arguments: list[str]):
    if not executable.is_file():
        raise FileNotFoundError(executable)
    with tempfile.TemporaryDirectory(prefix="ecc-desktop-benchmark-") as directory:
        isolated = Path(directory)
        marker = isolated / "ready.json"
        environment = dict(os.environ, ECC_DATA_DIR=str(isolated / "data"),
                           ECC_BENCHMARK_READY_FILE=str(marker), ECC_BENCHMARK="1")
        launch = time.perf_counter()
        process = subprocess.Popen([str(executable), *arguments], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        known = {}
        try:
            deadline = launch + 45
            while not marker.exists():
                if process.poll() is not None:
                    raise RuntimeError(f"Desktop exited before readiness (code {process.returncode})")
                if time.perf_counter() > deadline:
                    raise TimeoutError("Desktop did not produce the interactive readiness marker")
                time.sleep(.05)
            opening = time.perf_counter() - launch
            print(json.dumps({"phase": "ready", "opening_seconds": opening}), flush=True)
            until = time.perf_counter() + settle
            while time.perf_counter() < until:
                if process.poll() is not None:
                    raise RuntimeError("Desktop exited while settling")
                process_metrics(process.pid, known)
                time.sleep(.5)
            previous = {}
            for pid, child in list(known.items()):
                try:
                    cpu = child.cpu_times()
                    previous[pid] = cpu.user + cpu.system
                except psutil.Error:
                    pass
            samples = []
            started_at = time.time()
            beginning = time.perf_counter()
            print(json.dumps({"phase": "idle_measurement", "seconds": seconds}), flush=True)
            for _ in range(seconds):
                sample = process_metrics(process.pid, known)
                delta = 0
                for pid, child in list(known.items()):
                    try:
                        delta += cpu_delta(child, previous, started_at)
                    except psutil.Error:
                        pass
                sample["cpu_delta"] = delta
                samples.append(sample)
                time.sleep(1)
            result = summarize(samples, psutil.cpu_count() or 1, time.perf_counter() - beginning)
            result.update({"executable": str(executable), "opening_seconds": opening,
                           "settle_seconds": settle, "isolated_data": True,
                           "accounts": 0, "real_vm_operations": False})
            result["resource_budget_passed"] = budget_passed(result)
            return result
        finally:
            # This private process tree uses empty data and cannot own a cloud VM.
            process_metrics(process.pid, known)
            for child in reversed(list(known.values())):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            psutil.wait_procs(list(known.values()), timeout=5)
            for child in known.values():
                try:
                    if child.is_running():
                        child.kill()
                except psutil.Error:
                    pass
            process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--settle", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    if args.seconds < 1 or args.settle < 0:
        parser.error("seconds must be positive and settle nonnegative")
    result = measure(args.executable.resolve(), args.seconds, args.settle, args.arguments)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
