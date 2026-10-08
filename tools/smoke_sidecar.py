"""Check a packaged JSONL controller using disposable, empty account data."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


def check(executable: Path):
    with tempfile.TemporaryDirectory(prefix="ecc-sidecar-smoke-") as directory:
        started = time.perf_counter()
        process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=dict(os.environ, ECC_DATA_DIR=directory),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        replies = queue.Queue()
        def read():
            for line in process.stdout:
                replies.put(json.loads(line.decode("utf-8")))
        threading.Thread(target=read, daemon=True).start()
        try:
            def request(action, params):
                process.stdin.write(json.dumps({"id":action, "action":action, "params":params}).encode() + b"\n")
                process.stdin.flush()
                while True:
                    reply = replies.get(timeout=15)
                    if reply.get("id") == action:
                        assert reply.get("ok"), reply.get("error")
                        return reply["result"]
            first = request("app.snapshot", {})
            elapsed = time.perf_counter() - started
            assert first["accounts"] == [] and first["session"]["state"] == "idle"
            assert first["capabilities"]["native"] is True
            request("app.preferences", {"language":"en"})
            assert request("app.snapshot", {})["preferences"]["language"] == "en"
            diagnostics = request("app.diagnostics", {})
            assert diagnostics["session"]["state"] == "idle"
            return {"packaged_controller":True, "first_snapshot_seconds":elapsed,
                    "jsonl_utf8":True, "preferences_roundtrip":True,
                    "empty_accounts":True, "real_vm_operations":False}
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.executable.resolve())))
