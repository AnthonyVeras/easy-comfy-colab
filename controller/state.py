from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import math
import sqlite3
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path


class AppError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def data_dir():
    if os.environ.get("ECC_DATA_DIR"):
        return Path(os.environ["ECC_DATA_DIR"]).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "EasyComfyColab2"


def protect(value: bytes, decrypt=False):
    if os.name != "nt":
        raise AppError("windows_required", "Secure storage requires Windows.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    operation = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise AppError("credential_unavailable", "Windows could not unlock this credential. Reconnect this account.")
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free(result.data)


def secret_prompt(provider, language="pt-BR"):
    if os.name != "nt":
        raise AppError("windows_required", "The private credential dialog requires Windows.")

    class Info(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("parent", wintypes.HWND),
                    ("message", wintypes.LPCWSTR), ("caption", wintypes.LPCWSTR), ("banner", wintypes.HANDLE)]

    portuguese = language != "en"
    labels = {"Google authorization code": "o código de autorização do Google", "Google Drive authorization code": "o código de autorização do Google Drive"}
    label = labels.get(provider, "o token do " + provider)
    message = f"Informe {label}. Ele permanece no controlador nativo." if portuguese else f"Enter {provider}. It remains in the native controller."
    info = Info(ctypes.sizeof(Info), None, message, "Easy Comfy Colab 2", None)
    username = ctypes.create_unicode_buffer("Credencial" if portuguese else "Credential", 513)
    password = ctypes.create_unicode_buffer(1025)
    save = wintypes.BOOL(False)
    prompt = ctypes.windll.credui.CredUIPromptForCredentialsW
    prompt.argtypes = [ctypes.POINTER(Info), wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                       wintypes.LPWSTR, wintypes.ULONG, wintypes.LPWSTR, wintypes.ULONG,
                       ctypes.POINTER(wintypes.BOOL), wintypes.DWORD]
    prompt.restype = wintypes.DWORD
    result = prompt(ctypes.byref(info), "EasyComfyColab2/" + provider, None, 0,
                    username, len(username), password, len(password), ctypes.byref(save), 0x40000 | 0x2 | 0x80)
    if result == 1223:
        raise AppError("cancelled", "Credential entry cancelled.")
    if result:
        raise AppError("credential_dialog", "Windows could not open the credential dialog.")
    try:
        return password.value.strip()
    finally:
        ctypes.memset(ctypes.addressof(password), 0, ctypes.sizeof(password))


class InstanceLock:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.handle = None
        self.file = None
        if os.name == "nt":
            name = "Local\\EasyComfyColab2-" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
            create = ctypes.windll.kernel32.CreateMutexW
            create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            create.restype = wintypes.HANDLE
            self.handle = create(None, False, name)
            if not self.handle or ctypes.windll.kernel32.GetLastError() == 183:
                self.close()
                raise AppError("already_running", "Easy Comfy Colab 2 already has a controller open.")
        else:
            import fcntl
            self.file = (root / "controller.lock").open("a+")
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.close()
                raise AppError("already_running", "A controller is already open.")

    def close(self):
        if self.handle:
            close = ctypes.windll.kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close(self.handle)
            self.handle = None
        if self.file:
            self.file.close()
            self.file = None


class Store:
    def __init__(self, root=None, crypt=protect):
        self.root = Path(root or data_dir())
        self.root.mkdir(parents=True, exist_ok=True)
        self.crypt = crypt
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,label TEXT,email TEXT,subject TEXT UNIQUE,
                connected INTEGER NOT NULL DEFAULT 0,gpu TEXT NOT NULL DEFAULT 'T4',output_mode TEXT NOT NULL DEFAULT 'drive');
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS secrets(account_id TEXT,provider TEXT,value BLOB NOT NULL,
                PRIMARY KEY(account_id,provider),FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,time REAL,operation TEXT,account_id TEXT,result TEXT,details TEXT);
        """)
        if "session_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(history)")}:
            self.db.execute("ALTER TABLE history ADD COLUMN session_id TEXT")
        self.db.executescript("CREATE INDEX IF NOT EXISTS history_account ON history(account_id,id DESC); CREATE INDEX IF NOT EXISTS history_session ON history(session_id,id DESC);")

    def accounts(self):
        with self.lock:
            return [{**dict(row), "connected": bool(row["connected"])} for row in self.db.execute("SELECT id,label,email,connected,gpu,output_mode FROM accounts ORDER BY rowid")]

    def account(self, account_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise AppError("account_missing", "Select or connect a Google account first.")
        return dict(row)

    def add(self, label="Google account"):
        account_id = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute("INSERT INTO accounts(id,label,email,subject) VALUES(?,?,?,NULL)", (account_id, str(label).strip()[:48] or "Google account", ""))
            if not self.get("selected_account_id"):
                self.set("selected_account_id", account_id)
        return account_id

    def bind(self, account_id, identity, credential=None):
        account = self.account(account_id)
        if not identity.get("sub") or not identity.get("email") or identity.get("email_verified") is not True:
            raise AppError("identity_unverified", "Google did not confirm this account identity.")
        if account["subject"] and account["subject"] != identity["sub"]:
            raise AppError("account_mismatch", "Authorization belongs to another Google account. Add it separately.")
        protected = self.crypt(json.dumps(credential[1]).encode()) if credential else None
        with self.lock, self.db:
            other = self.db.execute("SELECT id FROM accounts WHERE subject=? AND id<>?", (identity["sub"], account_id)).fetchone()
            if other:
                raise AppError("account_duplicate", "This Google account is already connected.")
            self.db.execute("UPDATE accounts SET subject=?,email=?,connected=1 WHERE id=?", (identity["sub"], identity["email"], account_id))
            if credential:
                self.db.execute("INSERT OR REPLACE INTO secrets VALUES(?,?,?)", (account_id, credential[0], protected))

    def update(self, account_id, **changes):
        self.account(account_id)
        checked = {}
        for key, value in changes.items():
            if key == "label":
                value = str(value).strip()[:48]
                if not value:
                    raise AppError("invalid_label", "Enter an account name.")
            elif key == "gpu":
                if value not in {"CPU", "T4", "L4", "A100", "G4"}:
                    raise AppError("invalid_gpu", "Select supported hardware.")
            elif key == "output_mode":
                if value not in {"pc", "drive"}:
                    raise AppError("invalid_output_mode", "Choose PC or Drive.")
            else:
                raise AppError("invalid_account_field", "This account field cannot be changed.")
            checked[key] = value
        with self.lock, self.db:
            for key, value in checked.items():
                self.db.execute(f"UPDATE accounts SET {key}=? WHERE id=?", (value, account_id))

    def remove(self, account_id):
        self.account(account_id)
        with self.lock, self.db:
            self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            if self.get("selected_account_id") == account_id:
                choices = self.accounts()
                self.set("selected_account_id", choices[0]["id"] if choices else None)

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value)))

    def secret(self, account_id, provider, value=None, *, delete=False):
        self.account(account_id)
        with self.lock, self.db:
            if delete:
                self.db.execute("DELETE FROM secrets WHERE account_id=? AND provider=?", (account_id, provider))
                return None
            if value is not None:
                blob = self.crypt(json.dumps(value).encode())
                self.db.execute("INSERT OR REPLACE INTO secrets VALUES(?,?,?)", (account_id, provider, blob))
                return None
            row = self.db.execute("SELECT value FROM secrets WHERE account_id=? AND provider=?", (account_id, provider)).fetchone()
            return json.loads(self.crypt(row[0], decrypt=True)) if row else None

    def event(self, operation, account_id, result, **details):
        if not isinstance(operation, str) or not re.fullmatch(r"[a-zA-Z]+\.[a-zA-Z]+", operation) or result not in {"complete", "error", "allocation_absent"}:
            raise AppError("history_invalid", "History only accepts classified controller operations.")
        safe = {}
        for key in ("duration_seconds", "elapsed_seconds"):
            value = details.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                safe[key] = round(value, 3)
        if type(details.get("cold_install")) is bool:
            safe["cold_install"] = details["cold_install"]
        if isinstance(details.get("error_code"), str) and re.fullmatch(r"[a-z_]{1,64}", details["error_code"]):
            safe["error_code"] = details["error_code"]
        session_id = details.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session_id):
            session_id = None
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", account_id):
            account_id = None
        with self.lock, self.db:
            self.db.execute("INSERT INTO history(time,operation,account_id,session_id,result,details) VALUES(?,?,?,?,?,?)", (time.time(), operation, account_id, session_id, result, json.dumps(safe)))
            self.db.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 2000)")

    def history_page(self, offset=0, limit=50, account_id=None, session_id=None, operation=None, *, export=False):
        if type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= (2000 if export else 100):
            raise AppError("invalid_page", "Choose a history page with 1–100 items.")
        conditions, arguments = [], []
        for name, value in (("account_id", account_id), ("session_id", session_id), ("operation", operation)):
            if value is not None:
                if not isinstance(value, str) or len(value) > 80:
                    raise AppError("history_filter_invalid", "Choose a valid history filter.")
                conditions.append(name + "=?")
                arguments.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.lock:
            # One SQLite read transaction keeps count/page consistent across other store instances.
            self.db.execute("BEGIN")
            try:
                total = self.db.execute("SELECT count(*) FROM history" + where, arguments).fetchone()[0]
                rows = list(self.db.execute("SELECT * FROM history" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", [*arguments, limit, offset]))
            finally:
                self.db.commit()
        items = [{**dict(row), "details": json.loads(row["details"])} for row in rows]
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def history(self):
        return self.history_page()["items"]

    def close(self):
        self.db.close()
