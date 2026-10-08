from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from controller.state import AppError, secret_prompt

COLAB_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile", "https://www.googleapis.com/auth/cloud-platform",
                "https://www.googleapis.com/auth/colaboratory", "https://www.googleapis.com/auth/drive.file"]
DRIVE_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/drive"]


class NativeAuthorizedSession(AuthorizedSession):
    def request(self, method, url, *args, **kwargs):
        kwargs.setdefault("timeout", 30)
        return super().request(method, url, *args, **kwargs)


def authorized(info):
    if not isinstance(info, dict) or not all(isinstance(info.get(k), str) and info[k] for k in ("refresh_token", "client_id", "client_secret")):
        raise AppError("credential_format", "The saved Google login cannot be reused. Reconnect this account.")
    if info.get("type", "authorized_user") != "authorized_user":
        raise AppError("credential_type", "Only personal Google user credentials can be imported.")
    creds = Credentials.from_authorized_user_info(info)
    creds.refresh(Request())
    return NativeAuthorizedSession(creds)


def identity(session):
    response = session.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("sub") or not data.get("email") or data.get("email_verified") is not True:
        raise AppError("identity_unverified", "Google did not confirm the account identity.")
    return {k: data[k] for k in ("sub", "email", "email_verified")}


class GoogleAuth:
    def __init__(self, store, emit):
        self.store, self.emit = store, emit
        self.pending = {}
        self.sessions = {}

    def context(self, account_id, purpose="colab"):
        key = (account_id, purpose)
        if key not in self.sessions:
            info = self.store.secret(account_id, "google_" + purpose)
            if not info:
                raise AppError("account_not_connected", "Connect this account" + (" to Drive first." if purpose == "drive" else " to Google Colab first."))
            session = authorized(info)
            who = identity(session)
            if who["sub"] != self.store.account(account_id)["subject"]:
                raise AppError("account_mismatch", "Saved authorization belongs to a different Google account.")
            self.sessions[key] = session
            self.store.secret(account_id, "google_" + purpose, json.loads(session.credentials.to_json()))
        return self.sessions[key]

    def save(self, account_id, session, purpose="colab", expected_email=None):
        who = identity(session)
        if expected_email and who["email"].casefold() != expected_email.casefold():
            raise AppError("account_mismatch", "The imported credential belongs to another Google account.")
        account = self.store.account(account_id)
        if purpose == "drive" and account["subject"] != who["sub"]:
            raise AppError("account_mismatch", "Authorize Drive using the same Google account.")
        self.store.bind(account_id, who, credential=("google_" + purpose, json.loads(session.credentials.to_json())))
        self.sessions[(account_id, purpose)] = session
        return self.store.account(account_id)

    def begin(self, account_id, purpose="colab", client_path=None):
        if purpose not in ("colab", "drive"):
            raise AppError("invalid_purpose", "Choose Colab or Drive authorization.")
        if account_id in self.pending:
            raise AppError("auth_pending", "Finish the current authorization first.")
        self.store.account(account_id)
        path = Path(client_path or os.environ.get("EASY_COMFY_OAUTH_CLIENT", ".private/oauth-client.json"))
        private_client = self.store.secret(account_id, "google_drive_client") if purpose == "drive" and not client_path else None
        if not path.is_file() and not private_client:
            raise AppError("oauth_client_required", "Configure this app's Google Desktop OAuth client before connecting. Existing logins can use the private legacy importer.")
        config = private_client or json.loads(path.read_text(encoding="utf-8"))
        scopes = DRIVE_SCOPES if purpose == "drive" else COLAB_SCOPES
        flow = InstalledAppFlow.from_client_config(config, scopes=scopes, autogenerate_code_verifier=True)
        installed = config.get("installed", config.get("web", {}))
        redirects = installed.get("redirect_uris", [])
        if not any(urlsplit(uri).hostname in ("127.0.0.1", "localhost") for uri in redirects):
            # Imported private remote-copy clients keep their original registered redirect.
            if not redirects:
                raise AppError("oauth_redirect_required", "The OAuth client has no registered redirect.")
            flow.redirect_uri = redirects[0]
            url, state = flow.authorization_url(prompt="select_account consent", access_type="offline", token_usage="remote")
            self.pending[account_id] = (flow, state, purpose, None)
            self.emit("auth", {"account_id": account_id, "purpose": purpose, "authorization_url": url, "needs_code": True})
            return {"account_id": account_id, "authorization_url": url, "needs_code": True}

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                data = parse_qs(urlsplit(self.path).query)
                try:
                    if data.get("state", [None])[0] != state or "code" not in data:
                        raise AppError("oauth_callback_invalid", "Google authorization was cancelled or could not be verified.")
                    flow.fetch_token(code=data["code"][0], timeout=30)
                    owner.save(account_id, NativeAuthorizedSession(flow.credentials), purpose)
                    owner.emit("state", {"account_id": account_id, "connected": True, "purpose": purpose})
                    message = b"Google authorized. Return to Easy Comfy Colab 2."
                except Exception:
                    owner.emit("auth", {"account_id": account_id, "purpose": purpose, "error": "Authorization failed. Reconnect this account."})
                    message = b"Authorization failed. Return to Easy Comfy Colab 2."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(message)
                owner.pending.pop(account_id, None)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 600
        flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        url, state = flow.authorization_url(prompt="select_account consent", access_type="offline")
        self.pending[account_id] = (flow, state, purpose, server)

        def finish():
            try:
                server.handle_request()
            finally:
                server.server_close()
                self.pending.pop(account_id, None)

        threading.Thread(target=finish, daemon=True).start()
        result = {"account_id": account_id, "authorization_url": url, "needs_code": False, "purpose": purpose}
        self.emit("auth", result)
        return result

    def submit(self, account_id):
        pending = self.pending.get(account_id)
        if not pending or pending[3]:
            raise AppError("auth_not_pending", "Complete authorization in the browser.")
        flow, _, purpose, _ = pending
        code = secret_prompt("Google authorization code", self.store.get("preferences", {}).get("language", "pt-BR"))
        flow.fetch_token(code=code, timeout=30)
        self.save(account_id, NativeAuthorizedSession(flow.credentials), purpose)
        self.pending.pop(account_id, None)
        self.emit("state", {"account_id": account_id, "connected": True, "purpose": purpose})
        return {"account_id": account_id, "connected": True}


def upstream():
    vendor = str(Path(__file__).resolve().parent / "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    from colab_cli.client import Client, Prod, Accelerator, Variant
    from colab_cli.runtime import ColabRuntime
    return Client, Prod, Accelerator, Variant, ColabRuntime


class Colab:
    def __init__(self, auth, account_id, emit):
        Client, Prod, _, _, _ = upstream()
        self.account_id, self.emit = account_id, emit
        self.language = auth.store.get("preferences", {}).get("language", "pt-BR")
        self.session = auth.context(account_id)
        self.client = Client(Prod(), self.session)
        self.runtime = None
        self.endpoint = None
        self.session_id = None
        self.mount_confirmation = threading.Event()
        self.mount_pending = False

    def confirm_mount(self):
        if not self.mount_pending:
            raise AppError("auth_not_pending", "Drive mount confirmation is not pending.")
        self.mount_confirmation.set()
        return {"confirmed": True, "purpose": "mount_drive"}

    def allocate(self, notebook_id, gpu):
        _, _, Accelerator, Variant, _ = upstream()
        return self.client.assign(notebook_id, variant=Variant.DEFAULT if gpu == "CPU" else Variant.GPU,
                                  accelerator=None if gpu == "CPU" else Accelerator(gpu))

    def recover(self, notebook_id, gpu):
        """Read the exact notebook assignment; this never sends allocation POST."""
        _, _, Accelerator, Variant, _ = upstream()
        assignment = self.client._get_assignment(notebook_id, Variant.DEFAULT if gpu == "CPU" else Variant.GPU,
                                                 None if gpu == "CPU" else Accelerator(gpu))
        if not getattr(assignment, "endpoint", None):
            raise AppError("owned_allocation_absent", "Google confirmed there is no allocation for the exact recorded notebook.")
        self.owned(assignment.endpoint)
        return assignment

    def owned(self, endpoint):
        matches = [item for item in self.client.list_assignments() if item.endpoint == endpoint]
        if len(matches) != 1:
            raise AppError("owned_vm_missing", "The recorded owned allocation could not be verified. No VM was stopped.")
        return matches[0]

    def mount(self, assignment, cancel):
        _, _, _, _, ColabRuntime = upstream()
        self.endpoint = assignment.endpoint
        proxy = assignment.runtime_proxy_info
        self.runtime = ColabRuntime(proxy.url, proxy.token)
        failed = []

        def drive_hook(message, wsclient):
            if message.get("content", {}).get("request", {}).get("authType") != "dfs_ephemeral":
                return False
            try:
                # The official flow dry-runs once, waits for browser consent, then propagates.
                url = self.client.colab_domain + "/tun/m/credentials-propagation/" + self.endpoint
                params = {"authuser": "0", "authtype": "dfs_ephemeral", "version": "2", "dryrun": "true", "propagate": "true", "record": "false"}
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()
                token = json.loads(response.text.split("\n", 1)[-1])["token"]
                headers = {"x-goog-colab-token": token}
                response = self.session.post(url, params=params, headers=headers, files={"file_id": (None, "empty.ipynb")}, timeout=30)
                response.raise_for_status()
                data = json.loads(response.text.split("\n", 1)[-1])
                if not data.get("success"):
                    uri = data.get("unauthorized_redirect_uri")
                    parsed = urlsplit(uri or "")
                    if parsed.scheme != "https" or parsed.hostname != "accounts.google.com":
                        raise AppError("drive_auth_url", "Drive returned an unexpected authorization URL.")
                    self.mount_confirmation.clear()
                    self.mount_pending = True
                    deadline = time.monotonic() + 540
                    self.emit("auth", {"account_id": self.account_id, "session_id": self.session_id, "purpose": "mount_drive", "authorization_url": uri, "needs_code": False, "needs_confirmation": True})
                    while not self.mount_confirmation.wait(0.2):
                        if cancel.is_set() or time.monotonic() > deadline:
                            raise AppError("drive_auth_cancelled", "Drive authorization was not completed. The owned VM remains available.")
                params["dryrun"] = "false"
                response = self.session.post(url, params=params, headers=headers, files={"file_id": (None, "empty.ipynb")}, timeout=30)
                response.raise_for_status()
                reply = wsclient.session.msg("input_reply", {"value": {"type": "colab_reply", "colab_msg_id": message.get("metadata", {}).get("colab_msg_id")}})
                reply["parent_header"] = message.get("header", {})
                wsclient.stdin_channel.send(reply)
            except Exception as error:
                failed.append(error)
                try:
                    reply = wsclient.session.msg("input_reply", {"value": {"type": "colab_reply", "colab_msg_id": message.get("metadata", {}).get("colab_msg_id"), "error": "Drive authorization could not complete. Retry recovery in Easy Comfy Colab 2."}})
                    reply["parent_header"] = message.get("header", {})
                    wsclient.stdin_channel.send(reply)
                except Exception:
                    pass
                self.emit("auth", {"account_id": self.account_id, "session_id": self.session_id, "purpose": "mount_drive", "error_code": error.code if isinstance(error, AppError) else "drive_mount_failed", "error": "Drive authorization could not complete. Reconnect the owned session.", "needs_confirmation": False})
            finally:
                self.mount_pending = False
            return True

        self.runtime.colab_request_hook = drive_hook
        outputs = self.runtime.execute_code("from google.colab import drive\ndrive.mount('/content/drive', timeout_ms=600000)\nfrom pathlib import Path\nassert Path('/content/drive/MyDrive').is_dir()", allow_stdin=True,
                                            stdin_hook=lambda _: secret_prompt("Google Drive authorization code", self.language), timeout=660)
        if failed:
            raise AppError("drive_mount_failed", "Drive authorization could not be completed. The VM remains available.")
        if any(item.get("output_type") == "error" for item in outputs):
            raise AppError("drive_mount_failed", "Drive did not mount. Complete Google authorization and reconnect.")
        self.emit("auth", {"account_id": self.account_id, "session_id": self.session_id, "purpose": "mount_drive", "completed": True, "needs_confirmation": False})

    def stop(self, endpoint):
        self.owned(endpoint)
        self.client.unassign(endpoint)

    def close(self):
        if self.runtime:
            self.runtime.stop(shutdown_kernel=False)
