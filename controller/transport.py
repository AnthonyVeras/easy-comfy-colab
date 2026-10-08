from __future__ import annotations

import base64
import hashlib
import io
import json
import select
import socket
import socketserver
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlencode

import paramiko
import websocket
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from controller.state import AppError

PORTS = {"comfy": 28188, "companion": 28190, "mcp": 28189}
REMOTE = "/content/easy-comfy-colab-2"


def proxy_ws_url(proxy):
    """Accept the HTTPS runtime domains returned by verified Google assignments."""
    try:
        parsed = urlsplit(proxy.url)
        trusted = parsed.hostname and any(parsed.hostname.endswith(suffix) for suffix in (".googleusercontent.com", ".prod.colab.dev"))
        if parsed.scheme != "https" or not trusted or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
        if not isinstance(proxy.token, str) or not proxy.token or len(proxy.token) > 16384:
            raise ValueError()
        return "wss://" + parsed.netloc + "/colab/ssh?" + urlencode({"colab-runtime-proxy-token": proxy.token})
    except (ValueError, TypeError, AttributeError):
        raise AppError("proxy_invalid", "Google returned an unexpected runtime proxy host.") from None


def reserved_listeners():
    result = {}
    try:
        for key, port in PORTS.items():
            sock = socket.socket()
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(16)
            result[key] = sock
    except OSError:
        sock.close()
        for listener in result.values():
            listener.close()
        raise AppError("port_busy", "An Easy Comfy port is already in use. Close the conflicting app before starting a VM.") from None
    return result


class WebSocketSocket:
    """The upstream binary WebSocket wire contract, exposed as a Paramiko socket."""
    def __init__(self, ws):
        self.ws = ws
        self.buffer = bytearray()

    def send(self, value):
        self.ws.send_binary(value)
        return len(value)

    def recv(self, size):
        try:
            while not self.buffer:
                opcode, value = self.ws.recv_data(control_frame=True)
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    return b""
                if opcode in (websocket.ABNF.OPCODE_BINARY, websocket.ABNF.OPCODE_TEXT):
                    self.buffer.extend(value.encode() if isinstance(value, str) else value)
        except websocket.WebSocketTimeoutException:
            raise socket.timeout() from None
        except websocket.WebSocketConnectionClosedException:
            return b""
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def settimeout(self, value):
        self.ws.settimeout(value)

    def gettimeout(self):
        return self.ws.gettimeout()

    def close(self):
        self.ws.close()


class ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class Transport:
    def __init__(self, store, account_id, proxy, listeners, expected_fingerprint=None):
        self.store, self.account_id = store, account_id
        self.listeners, self.servers = listeners, []
        self.ssh = None
        self.ws = None
        self.sftp = None
        url = proxy_ws_url(proxy)
        private = store.secret(account_id, "ssh_private_key")
        if not private:
            private = Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode()
            store.secret(account_id, "ssh_private_key", private)
        key = paramiko.Ed25519Key.from_private_key(io.StringIO(private))
        public = key.get_name() + " " + key.get_base64()
        try:
            self.ws = websocket.create_connection(url, header=["X-Colab-Ssh-Pubkey: " + public], timeout=30, enable_multithread=True, redirect_limit=0)
            self.ssh = paramiko.Transport(WebSocketSocket(self.ws))
            self.ssh.start_client(timeout=30)
            host_key = self.ssh.get_remote_server_key()
            self.fingerprint = base64.b64encode(hashlib.sha256(host_key.asbytes()).digest()).decode().rstrip("=")
            if expected_fingerprint and self.fingerprint != expected_fingerprint:
                raise AppError("host_changed", "The owned VM SSH identity changed. Connection preserved for manual recovery.")
            self.ssh.auth_publickey("root", key)
            self.ssh.set_keepalive(20)
            self.sftp = paramiko.SFTPClient.from_transport(self.ssh)
        except Exception:
            self.close()
            raise

    def forward(self, remote_ports):
        ssh = self.ssh
        for key, listener in self.listeners.items():
            remote_port = remote_ports.get(key)
            if not remote_port:
                listener.close()
                continue

            class Handler(socketserver.BaseRequestHandler):
                def handle(handler, port=remote_port):
                    channel = None
                    try:
                        channel = ssh.open_channel("direct-tcpip", ("127.0.0.1", port), handler.request.getpeername(), timeout=20)
                        while True:
                            ready, _, _ = select.select([handler.request, channel], [], [], 20)
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (channel if source is handler.request else handler.request).sendall(data)
                    except (OSError, EOFError, paramiko.SSHException):
                        pass
                    finally:
                        if channel:
                            channel.close()

            server = ForwardServer(("127.0.0.1", PORTS[key]), Handler, bind_and_activate=False)
            server.socket.close()
            server.socket = listener
            server.server_address = listener.getsockname()
            self.servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def command(self, command, payload=None, timeout=1200):
        channel = self.ssh.open_session(timeout=30)
        channel.settimeout(timeout)
        try:
            channel.exec_command(command)
            if payload is not None:
                channel.sendall(json.dumps(payload).encode() + b"\n")
            channel.shutdown_write()
            # Drain both streams to prevent SSH flow-control deadlock; never expose raw diagnostics.
            output = bytearray()
            deadline = time.monotonic() + timeout
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if time.monotonic() > deadline:
                    raise AppError("remote_timeout", "Remote operation timed out. The VM remains available for recovery.")
                if channel.recv_ready():
                    output.extend(channel.recv(65536))
                    if len(output) > 8 * 1024 * 1024:
                        raise AppError("remote_response_large", "Remote response exceeded its safe size.")
                if channel.recv_stderr_ready():
                    channel.recv_stderr(65536)
                if not channel.recv_ready() and not channel.recv_stderr_ready():
                    if not select.select([channel], [], [], 1)[0] and channel.closed:
                        break
            status = channel.recv_exit_status()
            if status:
                raise AppError("remote_command_failed", "The remote operation failed. The VM remains available for recovery.")
            try:
                return json.loads(output)
            except ValueError:
                raise AppError("remote_response_invalid", "Remote operation returned an invalid response.") from None
        finally:
            channel.close()

    def bootstrap(self, payload):
        return self.command(f"python3 {REMOTE}/remote/bootstrap.py --stdin", payload)

    def mkdir(self, path):
        parent = ""
        for segment in PurePosixPath(path).parts:
            if segment == "/":
                parent = "/"
                continue
            parent = parent.rstrip("/") + "/" + segment
            try:
                self.sftp.stat(parent)
            except FileNotFoundError:
                self.sftp.mkdir(parent, mode=0o700)

    def upload_remote(self, directory):
        directory = Path(directory)
        if any(not (directory / name).is_file() for name in ("bootstrap.py", "runtime.json", "web/remote_download.js")):
            raise AppError("runtime_resources_missing", "The app's reviewed runtime resources are missing. Reinstall before starting.")
        self.mkdir(REMOTE + "/remote")
        sources = list(directory.rglob("*.py")) + [directory / "runtime.json", directory / "web/remote_download.js"]
        for source in sorted(sources):
            if "tests" in source.parts or "__pycache__" in source.parts:
                continue
            relative = source.relative_to(directory).as_posix()
            target = REMOTE + "/remote/" + relative
            self.mkdir(str(PurePosixPath(target).parent))
            self.sftp.put(str(source), target)

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.servers.clear()
        for sock in self.listeners.values():
            sock.close()
        if self.sftp:
            self.sftp.close()
        if self.ssh:
            self.ssh.close()
        if self.ws:
            self.ws.close()
