"""Perfis locais e comandos do Colab CLI executados no Ubuntu WSL."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


from runtime_config import app_data_dir, wsl_prefix

CLI = "@colab"
PROFILE_ROOT = "profiles"
COMFY_URL = "http://127.0.0.1:18188/"
GPU_CHOICES = ("G4", "A100", "L4", "T4", "CPU")
WINDOWS_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent
        for candidate in (folder, folder.parent):
            if (candidate / "start.sh").is_file():
                return candidate
        return folder
    return Path(__file__).resolve().parent.parent


@dataclass
class Profile:
    id: str
    label: str
    email: str
    token_path: str
    sessions_path: str
    gpu: str = "A100"
    connected: bool = False
    output_mode: str = "drive"

    @property
    def media_data(self) -> Path:
        root = Path.home() / "Comfy Colab Results"
        return root if self.id == "default" else root / "accounts" / self.id

    @property
    def local_data(self) -> Path:
        root = project_root()
        return root if self.id == "default" else root / "accounts" / self.id


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "profiles.json"
        self.profiles: list[Profile] = []
        self.selected = "default"
        self.load()

    def load(self) -> None:
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        self.profiles = [
            Profile(
                id="default",
                label="Conta principal",
                email="",
                token_path=f"{PROFILE_ROOT}/default/token.json",
                sessions_path=f"{PROFILE_ROOT}/default/sessions.json",
                connected=False,
            )
        ]
        saved_profiles = data.get("profiles", [])
        if not isinstance(saved_profiles, list):
            saved_profiles = []
        for raw in saved_profiles:
            if not isinstance(raw, dict):
                continue
            profile_id = raw.get("id", "")
            if profile_id == "default":
                default = self.profiles[0]
                default.label = str(raw.get("label") or default.label)[:48]
                default.email = str(raw.get("email") or "")[:254]
                default.gpu = (
                    raw.get("gpu") if raw.get("gpu") in GPU_CHOICES else "A100"
                )
                default.connected = bool(raw.get("connected", False))
                default.output_mode = "pc" if raw.get("output_mode") == "pc" else "drive"
            elif re.fullmatch(r"[0-9a-f]{16}", str(profile_id)):
                self.profiles.append(
                    Profile(
                        id=profile_id,
                        label=str(raw.get("label") or "Outra conta")[:48],
                        email=str(raw.get("email") or "")[:254],
                        token_path=f"{PROFILE_ROOT}/{profile_id}/token.json",
                        sessions_path=f"{PROFILE_ROOT}/{profile_id}/sessions.json",
                        gpu=raw.get("gpu") if raw.get("gpu") in GPU_CHOICES else "A100",
                        connected=bool(raw.get("connected", False)),
                        output_mode="pc" if raw.get("output_mode") == "pc" else "drive",
                    )
                )
        wanted = data.get("selected", "default")
        self.selected = (
            wanted if any(p.id == wanted for p in self.profiles) else "default"
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "selected": self.selected,
            "profiles": [asdict(p) for p in self.profiles],
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def current(self) -> Profile:
        return next(p for p in self.profiles if p.id == self.selected)

    def add(self, label: str) -> Profile:
        clean = label.strip()[:48] or "Outra conta"
        profile_id = uuid.uuid4().hex[:16]
        profile = Profile(
            id=profile_id,
            label=clean,
            email="",
            token_path=f"{PROFILE_ROOT}/{profile_id}/token.json",
            sessions_path=f"{PROFILE_ROOT}/{profile_id}/sessions.json",
        )
        self.profiles.append(profile)
        self.save()
        return profile


@dataclass
class Snapshot:
    balance: float | None = None
    rate: float | None = None
    active_assignments: int | None = None
    session_exists: bool = False
    status_known: bool = False
    remote_status: str = ""
    hardware: str = ""
    local_ready: bool = False
    error: str = ""

    @property
    def consuming(self) -> bool:
        return self.session_exists or bool(self.active_assignments)


def parse_usage(output: str) -> tuple[float | None, float | None, int | None]:
    def number(pattern: str, kind: type) -> float | int | None:
        match = re.search(pattern, output, re.IGNORECASE)
        return kind(match.group(1)) if match else None

    return (
        number(r"Current balance:\s*([0-9.]+)", float),
        number(r"Usage rate:\s*([0-9.]+)\s*/\s*hr", float),
        number(r"Active assignments:\s*(\d+)", int),
    )


def parse_status(output: str) -> tuple[bool, str, str]:
    if "not found" in output.lower():
        return False, "", ""
    if "Hardware:" not in output:
        return False, "", ""
    hardware = re.search(r"Hardware:\s*([A-Za-z0-9_-]+)", output)
    status = re.search(r"Status:\s*([A-Za-z0-9_-]+)", output)
    return (
        True,
        status.group(1) if status else "",
        hardware.group(1) if hardware else "",
    )


def parse_email(output: str) -> str:
    match = re.search(r"^Email:\s*(\S+)", output, re.MULTILINE)
    return match.group(1) if match and "@" in match.group(1) else ""


class Gateway:
    def __init__(self, root: Path | None = None):
        self.root = root or project_root()
        self._linux_root: str | None = None

    def check_installation(self) -> str:
        required = (
            "start.sh",
            "stop.sh",
            "remote/install.sh",
            "remote/runtime_image.py",
            "app/runtime_image.sh",
            "remote/restart.py",
            "remote/mcp_http.py",
            "app/install_hook.sh",
            "app/assignment_guard.py",
            "app/cancel_start.sh",
            "app/ssh_transport.sh",
            "app/environment.sh",
            "app/wsl_exec.sh",
            "app/drive_transfer.py",
            "remote/download_server.py",
            "remote/output_storage.py",
            "app/output_control.sh",
        )
        missing = [name for name in required if not (self.root / name).is_file()]
        if missing:
            return "Arquivos do projeto ausentes ao lado do aplicativo: " + ", ".join(
                missing
            )
        try:
            profile = ProfileStore().current()
            result = self.run(profile, "@check", timeout=15)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            return f"WSL indisponível: {exc}. Execute Setup.ps1."
        if result.returncode:
            return "Ambiente WSL não preparado. Execute Setup.ps1 na pasta do projeto."
        return ""

    def linux_root(self) -> str:
        if self._linux_root:
            return self._linux_root
        result = subprocess.run(
            [*wsl_prefix(), "wslpath", "-a", str(self.root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=WINDOWS_NO_CONSOLE,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError("Não foi possível localizar o projeto no Ubuntu WSL.")
        self._linux_root = result.stdout.strip()
        return self._linux_root

    def args(
        self, profile: Profile, *command: str, gpu: str | None = None
    ) -> list[str]:
        if profile.output_mode not in {"pc", "drive"}:
            raise ValueError("Destino de outputs inválido.")
        variables = [f"COMFY_PROFILE_ID={profile.id}", "COMFY_APP_MODE=1", f"COMFY_OUTPUT_MODE={profile.output_mode}", f"COMFY_MEDIA_ROOT={profile.media_data}"]
        if gpu:
            if gpu not in GPU_CHOICES:
                raise ValueError("GPU inválida")
            variables.append(f"COMFY_GPU={gpu}")
        return [*wsl_prefix(), "env", *variables, "bash", f"{self.linux_root()}/app/wsl_exec.sh", *command]

    def run(
        self, profile: Profile, *command: str, timeout: int = 25
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.args(profile, *command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=WINDOWS_NO_CONSOLE,
        )

    def spawn(
        self, profile: Profile, *command: str, gpu: str | None = None
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            self.args(profile, *command, gpu=gpu),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=WINDOWS_NO_CONSOLE,
        )

    def ensure_profile_home(self, profile: Profile) -> None:
        result = self.run(profile, "true")
        if result.returncode:
            raise RuntimeError("Não foi possível preparar o perfil no WSL.")

    def cli_command(self, profile: Profile, *subcommand: str) -> tuple[str, ...]:
        return (CLI, *subcommand)

    def refresh(self, profile: Profile) -> Snapshot:
        snapshot = Snapshot()
        try:
            usage = self.run(profile, *self.cli_command(profile, "usage"), timeout=20)
            if usage.returncode == 0:
                snapshot.balance, snapshot.rate, snapshot.active_assignments = (
                    parse_usage(usage.stdout)
                )
            else:
                snapshot.error = "Não foi possível consultar os créditos. Verifique a conta e a conexão."
            status = self.run(
                profile,
                *self.cli_command(profile, "status", "-s", "comfy-colab"),
                timeout=20,
            )
            if status.returncode == 0:
                snapshot.status_known = True
                snapshot.session_exists, snapshot.remote_status, snapshot.hardware = (
                    parse_status(status.stdout)
                )
            elif not snapshot.error:
                snapshot.error = "Não foi possível consultar a sessão do Colab."
        except (OSError, subprocess.TimeoutExpired) as exc:
            snapshot.error = f"Não foi possível consultar o Colab: {exc}"
        try:
            with urlopen(COMFY_URL + "system_stats", timeout=2) as response:
                snapshot.local_ready = response.status == 200
        except (OSError, URLError, TimeoutError):
            pass
        return snapshot

    def email(self, profile: Profile) -> str:
        result = self.run(profile, *self.cli_command(profile, "whoami"), timeout=18)
        if result.returncode != 0:
            return ""
        return parse_email(result.stdout)
