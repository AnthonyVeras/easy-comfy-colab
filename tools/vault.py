"""Development-only note access through an isolated MCPVault stdio server."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path


class Vault:
    def __init__(self, config_path: Path | None = None):
        config_path = config_path or Path(__file__).resolve().parents[1] / ".private" / "vault.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.root = Path(config["vault"]).resolve()
        self.process = subprocess.Popen(
            [config["node"], config["server"], str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.messages: queue.Queue = queue.Queue()
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()
        self._request("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "easy-comfy-project-notes", "version": "1.0.0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.messages.put({"closed": True})

    def _send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            response = self.messages.get(timeout=45)
            if response.get("closed"):
                raise RuntimeError("Obsidian MCP closed before responding")
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(str(response["error"].get("message", "MCP error")))
            return response["result"]

    def call(self, name, arguments):
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("Obsidian MCP rejected " + name)
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    data = json.loads(item["text"])
                except json.JSONDecodeError:
                    data = item["text"]
                if isinstance(data, dict) and data.get("success") is False:
                    raise RuntimeError("Obsidian MCP operation failed: " + name)
                return data
        return result

    def read(self, path):
        return self.call("read_note", {"path": path})

    def write(self, path, content, frontmatter=None):
        result = self.call("write_note", {"path": path, "content": content,
                            "frontmatter": frontmatter or {}, "mode": "overwrite"})
        readback = self.read(path)
        if isinstance(readback, dict):
            text = readback.get("content", readback.get("c", ""))
            if content.strip() not in text:
                raise RuntimeError("MCP readback did not match written note: " + path)
        return result

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        raise SystemExit('Usage: vault.py read PATH | write PATH (UTF-8 stdin) | stats')
    with Vault() as vault:
        if sys.argv[1] == "stats":
            result = vault.call("get_vault_stats", {})
        elif sys.argv[1] == "read" and len(sys.argv) == 3:
            result = vault.read(sys.argv[2])
        elif sys.argv[1] == "write" and len(sys.argv) == 3:
            result = vault.write(sys.argv[2], sys.stdin.read())
        else:
            raise SystemExit("Invalid vault command")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
