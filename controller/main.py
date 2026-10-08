"""JSONL-only native sidecar. No shell command or secret crosses the UI bridge."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path
from typing import TypedDict

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.core import Controller
from controller.state import AppError, InstanceLock, Store, data_dir


class JsonlRequest(TypedDict):
    id: str
    action: str
    params: dict


class JsonlEvent(TypedDict):
    event: str
    data: dict


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-legacy", action="store_true")
    parser.add_argument("--legacy-home", help="Optional UNC source home for private legacy migration")
    parser.add_argument("--legacy-windows-root", help="Optional legacy profile directory")
    parser.add_argument("--selected-only", action="store_true")
    args = parser.parse_args()
    # Upstream exception logs can contain proxy-token URLs. UI receives classified errors only.
    logging.disable(logging.CRITICAL)
    output_lock = threading.Lock()

    def write(payload):
        with output_lock:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)

    lock = None
    controller = None
    try:
        lock = InstanceLock(data_dir())
        controller = Controller(Store(), lambda event, data: write({"event": event, "data": data}))
        if args.import_legacy:
            result = controller.dispatch({"id": "private-import", "action": "account.importLegacy", "params": {
                "confirmed": True, "source_root": args.legacy_home, "windows_root": args.legacy_windows_root, "all_accounts": not args.selected_only}})
            write(result)
            return 0 if result["ok"] else 1
        workers = threading.BoundedSemaphore(4)
        threads = []

        def monitor():
            while not controller.closed.wait(5):
                controller.poll()

        threading.Thread(target=monitor, daemon=True).start()

        def request(item):
            try:
                write(controller.dispatch(item))
            finally:
                workers.release()

        while raw := sys.stdin.buffer.readline(1024 * 1024 + 1):
            if len(raw) > 1024 * 1024:
                while raw and not raw.endswith(b"\n"):
                    raw = sys.stdin.buffer.readline(65536)
                write({"id": None, "ok": False, "error": {"code": "request_too_large", "message": "Request exceeds 1 MiB."}})
                continue
            try:
                item: JsonlRequest = json.loads(raw)
            except (ValueError, UnicodeError):
                write({"id": None, "ok": False, "error": {"code": "invalid_json", "message": "Send one valid JSON request per line."}})
                continue
            if not workers.acquire(blocking=False):
                write({"id": item.get("id") if isinstance(item, dict) else None, "ok": False, "error": {"code": "operation_busy", "message": "Wait for pending controller requests."}})
                continue
            threads = [thread for thread in threads if thread.is_alive()]
            thread = threading.Thread(target=request, args=(item,), daemon=True)
            threads.append(thread)
            thread.start()
        # Preserve the singleton until every allocation worker has recorded its outcome.
        # Interrupt transport I/O and ask mount/start workers to return; never unassign on EOF.
        controller.close()
        for thread in threads:
            thread.join()
        return 0
    except AppError as error:
        write({"event": "state", "data": {"error": {"code": error.code, "message": error.message}}})
        return 1
    except Exception:
        write({"event": "state", "data": {"error": {"code": "controller_start_failed", "message": "The native controller could not start. Check installation."}}})
        return 1
    finally:
        if controller:
            controller.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
