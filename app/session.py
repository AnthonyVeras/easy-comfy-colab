"""Painel Windows para operar o ComfyUI hospedado no Google Colab."""

from __future__ import annotations

import codecs
import os
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import customtkinter as ctk

from backend import (
    COMFY_URL,
    Gateway,
    Profile,
    ProfileStore,
    Snapshot,
    parse_email,
)
from model_download import CredentialStore


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

COLORS = {
    "page": "#17191C",
    "sidebar": "#1E2024",
    "surface": "#22252A",
    "surface_high": "#2D3239",
    "border": "#454D59",
    "text": "#F4F6FF",
    "muted": "#AEBBD3",
    "subtle": "#ACB5C2",
    "accent": "#86BCF9",
    "accent_hover": "#ABD2FF",
    "accent_text": "#101328",
    "success": "#53D8B5",
    "warning": "#FFD17C",
    "danger": "#FF858B",
}

URL_PATTERN = re.compile(r"https://accounts\.google\.com/o/oauth2/v2/auth\?\S+")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def font(size: int, weight: str = "normal", family: str = "Segoe UI") -> ctk.CTkFont:
    return ctk.CTkFont(family=family, size=size, weight=weight)


def format_units(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    return (
        f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".") + suffix
    )


class ConfirmDialog(ctk.CTkToplevel):
    def __init__(
        self,
        parent: ctk.CTk,
        title: str,
        detail: str,
        actions: list[tuple[str, callable, str]],
        on_cancel=None,
    ):
        super().__init__(parent)
        self.title(title)
        self.geometry("640x300")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["surface"])
        self.transient(parent)
        self.grab_set()
        previous_focus = parent.focus_get()

        def close(callback=None):
            self.grab_release()
            self.destroy()
            if previous_focus and previous_focus.winfo_exists():
                previous_focus.focus_set()
            if callback:
                callback()

        self.protocol("WM_DELETE_WINDOW", lambda: close(on_cancel))
        ctk.CTkLabel(
            self, text=title, font=font(21, "bold"), text_color=COLORS["text"]
        ).pack(anchor="w", padx=24, pady=(24, 6))
        ctk.CTkLabel(
            self,
            text=detail,
            font=font(13),
            text_color=COLORS["muted"],
            wraplength=575,
            justify="left",
        ).pack(anchor="w", padx=24)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="bottom", fill="x", padx=24, pady=24)
        for label, callback, role in actions:
            color = COLORS["accent"] if role == "primary" else COLORS["surface_high"]
            hover = COLORS["accent_hover"] if role == "primary" else COLORS["border"]

            def activate(fn=callback):
                close(fn)

            button = ctk.CTkButton(
                row,
                text=label,
                command=activate,
                height=38,
                fg_color=color,
                hover_color=hover,
                text_color=COLORS["accent_text"]
                if role == "primary"
                else COLORS["text"],
                font=font(12, "bold"),
                corner_radius=9,
            )
            button.pack(side="right", padx=(8, 0))
            button._canvas.configure(takefocus=1)
            button._canvas.bind("<Return>", lambda _, b=button: b.invoke())
            button._canvas.bind("<space>", lambda _, b=button: b.invoke())
            button._canvas.bind(
                "<FocusIn>",
                lambda _, b=button: b.configure(
                    border_width=2, border_color=COLORS["accent"]
                ),
            )
            button._canvas.bind(
                "<FocusOut>", lambda _, b=button: b.configure(border_width=0)
            )
        self.bind("<Escape>", lambda _event: close(on_cancel))
        self.focus_force()


class SessionWindow(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Easy Comfy Colab")
        self.geometry("1240x840")
        self.minsize(980, 680)
        self.configure(fg_color=COLORS["page"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_icon()

        self.store = ProfileStore()
        self.gateway = Gateway()
        self.credentials = CredentialStore()
        self.download_active = False
        self.snapshot = Snapshot()
        self.events: queue.Queue[tuple] = queue.Queue()
        self.busy: str | None = None
        self.refreshing = False
        self.process = None
        self.operation_profile: Profile | None = None
        self.operation_text = ""
        self.output_pending = ""
        self.auth_url = ""
        self.auth_mode = ""
        self.cli_oauth_pending = False
        self.cancel_requested = False
        self.exit_after_stop = False
        self.setup_error = ""
        self.stage = "Verificando instalação…"
        self._build_ui()
        self._render()
        self.after(100, self._poll_events)
        self.after(30000, self._periodic_refresh)
        threading.Thread(target=self._initialize, daemon=True).start()

    def _set_icon(self) -> None:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon = base / "assets" / "comfy-colab.ico"
        if icon.exists():
            try:
                self.iconbitmap(str(icon))
            except Exception:
                pass

    def _initialize(self) -> None:
        try:
            problem = self.gateway.check_installation()
        except Exception as exc:
            problem = str(exc)
        self.events.put(("initialized", problem))

    def _periodic_refresh(self) -> None:
        if not self.busy and not self.refreshing and not self.setup_error:
            self._refresh_async()
        self.after(30000, self._periodic_refresh)

    def _refresh_async(self) -> None:
        if self.busy or self.refreshing or self.setup_error:
            return
        profile = self.store.current()
        if not profile.connected:
            self.snapshot = Snapshot()
            self._render()
            return
        self.refreshing = True
        self.stage = "Consultando sua conta…"
        self._render()

        def work():
            try:
                snapshot = self.gateway.refresh(profile)
                email = (
                    self.gateway.email(profile)
                    if not profile.email and not snapshot.error
                    else ""
                )
            except Exception as exc:
                snapshot = Snapshot(error=f"Não foi possível atualizar: {exc}")
                email = ""
            self.events.put(("refreshed", profile.id, snapshot, email))

        threading.Thread(target=work, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            for _ in range(150):
                item = self.events.get_nowait()
                kind = item[0]
                if kind == "initialized":
                    self.setup_error = item[1]
                    self.stage = self.setup_error or "Instalação pronta"
                    self._append_log(self.stage)
                    self._render()
                    if not self.setup_error:
                        self._refresh_async()
                elif kind == "refreshed":
                    _, profile_id, snapshot, email = item
                    self.refreshing = False
                    if self.store.selected == profile_id:
                        self.snapshot = snapshot
                        self._on_snapshot(profile_id, snapshot)
                        self.stage = snapshot.error or "Informações atualizadas"
                        if email:
                            profile = self.store.current()
                            profile.email = email
                            self.store.save()
                        self._render()
                    else:
                        self._refresh_async()
                elif kind == "stream":
                    self._consume_output(item[1])
                elif kind == "process_ready":
                    self.process = item[1]
                elif kind == "finished":
                    self._finish_operation(item[1], item[2])
                elif kind == "worker_error":
                    self._finish_operation(1, str(item[1]))
                elif kind == "ui":
                    item[1]()
        except queue.Empty:
            pass
        except Exception:
            self._append_log(
                "Falha ao atualizar a interface.\n" + traceback.format_exc()
            )
        self.after(100, self._poll_events)

    def _consume_output(self, chunk: str) -> None:
        clean = ANSI_PATTERN.sub("", chunk).replace("\r", "\n")
        if self.busy in {"connect", "drive"}:
            self.operation_text = (self.operation_text + clean)[-12000:]
        self.output_pending += clean
        while "\n" in self.output_pending:
            line, self.output_pending = self.output_pending.split("\n", 1)
            self._consume_line(line.strip())
        self.output_pending = self.output_pending[-12000:]

    def _consume_line(self, line: str) -> None:
        if not line:
            return
        if "To authorize colab-cli" in line:
            self.cli_oauth_pending = True
            self._append_log("A autorização do Colab CLI é necessária no navegador.")
            return
        match = URL_PATTERN.search(line)
        if match:
            new_request = self.auth_url != match.group(0) or not self.auth_mode
            self.auth_url = match.group(0)
            self.auth_mode = (
                "code"
                if self.busy in {"connect", "drive"} or self.cli_oauth_pending
                else "drive"
            )
            self.stage = "Autorização do Google necessária"
            self._append_log("Autorize a conta no navegador para continuar.")
            self._render()
            if new_request:
                self.show_page("Sessão")
                self._open_auth()
            return
        if "authorization code:" in line.lower():
            self._append_log("Cole no aplicativo o código exibido pelo Google.")
            return
        self._append_log(line)
        stages = (
            ("Criando sessão", "Alocando a GPU no Colab…"),
            ("Montando Google Drive", "Montando o Google Drive…"),
            ("Instalando ComfyUI", "Preparando o ComfyUI…"),
            ("Procurando imagem", "Verificando a imagem no Google Drive…"),
            ("Restaurando imagem", "Copiando a imagem do Drive para a VM…"),
            ("Extraindo...", "Extraindo e verificando a instalação…"),
            ("Preparando instalação convencional", "Instalando dependências: imagem compatível indisponível…"),
            ("Sincronizando custom nodes", "Sincronizando os nodes…"),
            ("Enviando entradas", "Enviando as entradas…"),
            ("Iniciando servidor", "Iniciando o servidor…"),
            ("Preparando Comfy MCP", "Preparando a integração Comfy MCP…"),
            ("Atualizando imagem do Drive", "Atualizando a imagem do Drive…"),
            ("Preparando imagem em uma cópia", "Copiando a instalação para preparar a imagem…"),
            ("Validando dependências", "Verificando a instalação copiada…"),
            ("Compactando imagem", "Compactando a imagem da instalação…"),
            ("Enviando imagem ao Drive", "Salvando a nova imagem no Drive…"),
            ("Verificando imagem no Drive", "Verificando a integridade da nova imagem…"),
            ("Imagem pronta no Drive", "Imagem do Drive atualizada e verificada"),
            ("Abrindo túnel", "Abrindo o túnel SSH…"),
            ("Teste concluído", "Inferência na GPU verificada"),
            ("Copiando resultados", "Copiando os resultados…"),
            ("Encerrando sessão", "Desligando a VM…"),
        )
        for needle, stage in stages:
            if needle in line:
                self.stage = stage
                self._render()
                break

    def _append_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 250:
            self.log_box.delete("1.0", "80.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _launch(
        self,
        operation: str,
        profile: Profile,
        command: tuple[str, ...],
        gpu: str | None = None,
    ) -> None:
        if self.busy:
            return
        self.operation_started = time.monotonic()
        self.busy = operation
        self.operation_profile = profile
        self.operation_text = ""
        self.output_pending = ""
        self.auth_url = ""
        self.auth_mode = ""
        self.cli_oauth_pending = False
        self.cancel_requested = False
        self.stage = {
            "start": "Preparando sessão…",
            "stop": "Encerrando sessão…",
            "connect": "Conectando conta…",
            "restart": "Reiniciando o ComfyUI…",
            "drive": "Operação no Google Drive…",
            "image": "Atualizando a imagem do Drive…",
        }[operation]
        self._append_log(f"── {self.stage} ──")
        self._render()

        def work():
            try:
                if operation == "connect":
                    self.gateway.ensure_profile_home(profile)
                process = self.gateway.spawn(profile, *command, gpu=gpu)
                self.events.put(("process_ready", process))
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                assert process.stdout is not None
                while chunk := process.stdout.read(4096):
                    text = decoder.decode(chunk)
                    if text:
                        self.events.put(("stream", text))
                tail = decoder.decode(b"", final=True)
                if tail:
                    self.events.put(("stream", tail))
                self.events.put(("finished", process.wait(), ""))
            except Exception as exc:
                self.events.put(("worker_error", exc))

        threading.Thread(target=work, daemon=True).start()

    def _finish_operation(self, return_code: int, error: str) -> None:
        operation = self.busy
        if self.output_pending.strip():
            self._consume_line(self.output_pending.strip())
        self.output_pending = ""
        self.process = None
        self.busy = None
        self.auth_mode = ""
        self.auth_url = ""
        self.cli_oauth_pending = False

        if operation == "start" and self.cancel_requested:
            self.cancel_requested = False
            self._append_log("Inicialização interrompida. Liberando a sessão do Colab…")
            self._launch(
                "stop",
                self.operation_profile,
                ("bash", f"{self.gateway.linux_root()}/stop.sh"),
            )
            return

        if return_code != 0:
            self.exit_after_stop = False
            self.stage = ("A VM foi mantida ligada. Veja o erro na atividade e tente novamente."
                          if operation in {"stop", "image"} else "Falha na operação. Veja a atividade e tente novamente.")
            self._append_log(error or f"Operação terminou com código {return_code}.")
            self._render()
            self._refresh_async()
            return

        if operation == "connect":
            email = parse_email(self.operation_text)
            if not email:
                self.stage = "A conta respondeu, mas o e-mail não foi identificado. Tente reconectar."
            else:
                profile = self.operation_profile
                profile.email = email
                profile.connected = True
                self.store.save()
                self.stage = f"Conta conectada: {email}"
                self._append_log(self.stage)
        elif operation == "start":
            self.stage = (
                "Downloads em CPU prontos"
                if self.operation_profile.gpu == "CPU"
                else "ComfyUI pronto no navegador"
            )
            self._append_log(self.stage)
            if (
                self.preferences.values["auto_open"]
                and self.operation_profile.gpu != "CPU"
            ):
                webbrowser.open(COMFY_URL)
        elif operation == "stop":
            self.stage = "VM desligada. O consumo desta sessão foi interrompido."
            self._append_log(self.stage)
            self.snapshot = Snapshot()
            if self.exit_after_stop:
                self.destroy()
                return
        elif operation == "restart":
            self.stage = "ComfyUI reiniciado. Modelos removidos da memória da GPU."
            self._append_log(self.stage)
        elif operation == "image":
            self.stage = "Imagem do Drive atualizada. Será usada na próxima VM compatível."
            self._append_log(self.stage)
        self._render()
        self._refresh_async()

    def _start(self) -> None:
        profile = self.store.current()
        if self.setup_error:
            self.stage = self.setup_error
        elif not profile.connected:
            self._connect_account()
            return
        elif not self.snapshot.status_known:
            self.stage = "Atualize a conta para confirmar se já existe uma VM ativa."
        elif self.snapshot.local_ready and not self.snapshot.session_exists:
            self.stage = "A porta local já está em uso por outra sessão. Encerre-a antes de iniciar."
        else:
            self._launch(
                "start",
                profile,
                ("bash", f"{self.gateway.linux_root()}/start.sh"),
                profile.gpu,
            )
            return
        self._render()

    def _stop(self, force: bool = False) -> None:
        if self.download_active and not force:
            ConfirmDialog(
                self,
                "Download em andamento",
                "Desligar a VM interrompe o download. A parte já baixada fica no Drive para retomar depois.",
                [
                    ("Aguardar download", lambda: None, "secondary"),
                    ("Desligar VM", lambda: self._stop(force=True), "primary"),
                ],
            )
            return
        if self.setup_error:
            self.stage = self.setup_error
            self._render()
            return
        if self.busy == "start":
            if self.cancel_requested:
                return
            self.cancel_requested = True
            self.stage = "Cancelando o início e liberando a VM…"
            profile = self.operation_profile

            def cancel():
                try:
                    result = self.gateway.run(
                        profile,
                        "bash",
                        f"{self.gateway.linux_root()}/app/cancel_start.sh",
                        timeout=15,
                    )
                    self.events.put(("stream", result.stdout or result.stderr))
                    if result.returncode:
                        self.events.put(("ui", self._cancel_failed))
                except Exception as exc:
                    self.events.put(("stream", f"Falha ao cancelar: {exc}\n"))
                    self.events.put(("ui", self._cancel_failed))

            threading.Thread(target=cancel, daemon=True).start()
            self._render()
            return
        if self.busy:
            return
        profile = self.store.current()
        self._launch("stop", profile, ("bash", f"{self.gateway.linux_root()}/stop.sh"))

    def _cancel_failed(self):
        if self.busy == "start":
            self.cancel_requested = False
            self.stage = "O cancelamento não foi confirmado. Tente cancelar novamente."
            self._render()

    def _update_runtime_image(self) -> None:
        if (self.busy or self.setup_error or not self.snapshot.session_exists
                or not self.snapshot.local_ready or self.snapshot.hardware == "CPU"):
            return
        self._launch("image", self.store.current(),
                     ("bash", f"{self.gateway.linux_root()}/app/runtime_image.sh"))

    def _restart_comfy(self) -> None:
        if (
            self.busy
            or self.download_active
            or self.setup_error
            or not (self.snapshot.session_exists and self.snapshot.local_ready)
        ):
            return
        profile = self.store.current()
        self._launch(
            "restart",
            profile,
            ("bash", f"{self.gateway.linux_root()}/app/output_control.sh", "restart"),
        )

    def _choose_gpu(self, gpu: str) -> None:
        if self.busy or self.snapshot.session_exists:
            return
        profile = self.store.current()
        profile.gpu = gpu
        self.store.save()
        self.stage = f"{gpu} selecionada para a próxima sessão"
        self._render()

    def _select_profile(self, profile_id: str) -> None:
        if profile_id == self.store.selected:
            return
        if self.busy:
            self.stage = "Aguarde a operação atual antes de trocar de conta."
            self._render()
            return
        if self.store.current().connected and not self.snapshot.status_known:
            self.stage = "Aguarde a consulta da VM antes de trocar de conta."
            self._render()
            return
        if self.snapshot.session_exists or self.snapshot.local_ready:
            self.stage = "Encerre a VM atual antes de trocar de conta."
            self._render()
            return
        self.store.selected = profile_id
        self.store.save()
        self.snapshot = Snapshot()
        self.stage = "Conta selecionada"
        self._render()
        self._refresh_async()

    def _add_account(self) -> None:
        if self.store.current().connected and not self.snapshot.status_known:
            self.stage = "Aguarde a consulta da VM antes de adicionar uma conta."
            self._render()
            return
        if self.busy or self.snapshot.session_exists or self.snapshot.local_ready:
            self.stage = "Encerre a VM antes de adicionar outra conta."
            self._render()
            return
        dialog = ctk.CTkInputDialog(
            text="Dê um nome para identificar esta conta.", title="Adicionar conta"
        )
        label = dialog.get_input()
        if label is None:
            return
        profile = self.store.add(label)
        self.store.selected = profile.id
        self.store.save()
        self.snapshot = Snapshot()
        self._render()
        self._connect_account()

    def _connect_account(self) -> None:
        if self.busy:
            return
        profile = self.store.current()
        self._launch("connect", profile, self.gateway.cli_command(profile, "whoami"))

    def _open_auth(self) -> None:
        if self.auth_url:
            webbrowser.open(self.auth_url)

    def _submit_auth(self) -> None:
        if not self.process or not self.process.stdin:
            self.stage = "Aguarde o pedido de autorização do Colab."
            self._render()
            return
        if self.auth_mode == "code":
            code = self.auth_code.get().strip()
            if not code:
                self.stage = "Cole o código exibido pelo Google."
                self._render()
                return
            payload = (code + "\n").encode("utf-8")
            self.auth_code.delete(0, "end")
            self._append_log("Código de autorização enviado ao Colab CLI.")
        else:
            payload = b"\n"
            self._append_log("Autorização do Drive concluída no navegador.")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
            self.auth_mode = ""
            self.auth_url = ""
            self.cli_oauth_pending = False
            self.auth_panel.pack_forget()
            self.stage = "Concluindo a autorização…"
        except (BrokenPipeError, OSError):
            self.stage = "O pedido de autorização expirou. Tente novamente."
        self._render()

    def _open_folder(self, subfolder: str) -> None:
        profile = self.store.current()
        folder = (profile.media_data if subfolder in {"input", "output"} else profile.local_data) / subfolder
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _on_close(self) -> None:
        if self.busy == "stop":
            self.exit_after_stop = True
            self.stage = "O aplicativo fechará após desligar a VM."
            self._render()
            return
        if self.busy == "start":
            ConfirmDialog(
                self,
                "Inicialização em andamento",
                "Cancelar agora também encerra a sessão do Colab para não continuar consumindo créditos.",
                [
                    ("Voltar", lambda: None, "secondary"),
                    ("Cancelar e desligar", self._cancel_and_exit, "primary"),
                ],
            )
            return
        if self.busy == "connect":
            ConfirmDialog(
                self,
                "Conexão em andamento",
                "Você pode cancelar a autorização desta conta.",
                [
                    ("Voltar", lambda: None, "secondary"),
                    ("Cancelar e sair", self._cancel_connect_and_exit, "primary"),
                ],
            )
            return
        if self.busy in {"restart", "drive", "image"}:
            self.stage = "Aguarde a operação terminar antes de fechar o aplicativo."
            self._render()
            return
        if self.setup_error:
            ConfirmDialog(
                self,
                "Sessão não verificada",
                "O aplicativo não conseguiu consultar o Colab. Verifique no Colab se há uma VM ativa após sair.",
                [
                    ("Voltar", lambda: None, "secondary"),
                    ("Sair", self.destroy, "primary"),
                ],
            )
            return
        if self.snapshot.session_exists or (
            self.store.current().connected and not self.snapshot.status_known
        ):
            ConfirmDialog(
                self,
                "A VM ainda está ligada",
                "Encerrar VM e sair atualiza a imagem do Drive e salva os outputs antes de desligar. Sair com VM ativa mantém o consumo de créditos e interrompe as cópias automáticas para o PC.",
                [
                    ("Voltar", lambda: None, "secondary"),
                    ("Sair com VM ativa", self.destroy, "secondary"),
                    ("Encerrar VM e sair", self._stop_and_exit, "primary"),
                ],
            )
            return
        self.destroy()

    def _cancel_and_exit(self) -> None:
        self.exit_after_stop = True
        self._stop()

    def _cancel_connect_and_exit(self) -> None:
        if self.process:
            self.process.terminate()
        self.destroy()

    def _stop_and_exit(self) -> None:
        if self.download_active:
            ConfirmDialog(
                self,
                "Download em andamento",
                "Aguarde a conclusão ou desligue a VM para interromper o download.",
                [
                    ("Aguardar", lambda: None, "secondary"),
                    ("Desligar e sair", self._force_stop_and_exit, "primary"),
                ],
            )
            return
        self._force_stop_and_exit()

    def _force_stop_and_exit(self) -> None:
        self.exit_after_stop = True
        self._stop(force=True)
