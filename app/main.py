"""Easy Comfy Colab: interface Windows com serviços assíncronos."""

from __future__ import annotations
import json
import math
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog
from model_download import prepare_download, start_download, _json_request
from services import (
    VERSION,
    Preferences,
    History,
    SessionLedger,
    IdleGuard,
    local_metrics,
    check_workflow,
    workflow_cache_selection,
    size,
    duration,
)
from session import SessionWindow, COLORS, ConfirmDialog, format_units
from ui import Shell

STATUS = {
    "queued": "Na fila",
    "running": "Baixando",
    "complete": "Concluído",
    "error": "Falhou",
    "cancelled": "Cancelado",
    "cancelling": "Cancelando",
    "interrupted": "Interrompido",
}


class Dashboard(Shell, SessionWindow):
    def __init__(self, offline=False):
        self.offline = offline
        self.preferences = Preferences()
        self.history = History()
        self.ledger = SessionLedger()
        self.guard = IdleGuard()
        self.models = []
        self.jobs = []
        self.remote_metrics = None
        self.local_stats = {}
        self.current_page = "Sessão"
        self.telemetry_pending = False
        self.operation_started = 0
        self.operation_busy = False
        self.idle_countdown = None
        self.idle_dialog = None
        self.low_alerted = False
        self.output_sync_pending = False
        self.had_pc_outputs = False
        self.cache_state = None
        self.cache_profile = None
        self.cache_pending = False
        super().__init__()
        self.title("Easy Comfy Colab — " + VERSION)
        self.after(500, self._tick)
        self.after(1000, self._telemetry)
        self.after(4000, self._sync_outputs)
        self.bind("<F5>", lambda _: self._refresh_async())
        self.bind_all("<Control-KeyPress>", self._keyboard_navigation, add="+")
        self.after(300, self.focus_set)

    def _keyboard_navigation(self, event):
        if event.widget.winfo_toplevel() != self:
            return
        pages = {49: "Sessão", 50: "Modelos", 51: "Contas e Drive"}
        page = pages.get(event.keycode)
        if page:
            self.show_page(page)
            return "break"

    def _initialize(self):
        if self.offline:
            self.events.put(
                ("initialized", "Modo de prévia: nenhuma conexão será aberta.")
            )
        else:
            super()._initialize()

    def _async(self, work, done=None):
        def run():
            try:
                result = work()
                if done:
                    self.events.put(("ui", lambda r=result: done(r)))
            except Exception as exc:
                self.events.put(("ui", lambda e=str(exc): self.notice(e, True)))

        threading.Thread(target=run, daemon=True).start()

    def notice(self, text, error=False):
        self.stage = text
        self._append_log(text)
        self.notice_label.configure(
            text=text, text_color=COLORS["danger"] if error else COLORS["text"]
        )
        self._render()

    def _render_accounts(self):
        signature = (
            self.store.selected,
            tuple((p.id, p.label, p.email, p.connected) for p in self.store.profiles),
        )
        if getattr(self, "_accounts_signature", None) == signature:
            return
        self._accounts_signature = signature
        self.account_choices = {
            f"{index + 1}. {p.label} · {p.email or 'não conectada'}": p.id
            for index, p in enumerate(self.store.profiles)
        }
        self.account_selector.configure(values=list(self.account_choices))
        self.account_selector.set(
            next(k for k, v in self.account_choices.items() if v == self.store.selected)
        )
        dest = [k for k, v in self.account_choices.items() if v != self.store.selected]
        self.drive_destination.configure(values=dest or ["Adicione outra conta"])
        self.drive_destination.set(dest[0] if dest else "Adicione outra conta")
        self.account_tree.delete(*self.account_tree.get_children())
        for p in self.store.profiles:
            self.account_tree.insert(
                "", "end", iid=p.id, values=(p.label, p.email or "Não conectada", p.id)
            )
        self.credential_status.configure(
            text="Hugging Face: "
            + (
                "salvo"
                if self.credentials.get(self.store.selected, "Hugging Face")
                else "não cadastrado"
            )
            + "  ·  Civitai: "
            + (
                "salvo"
                if self.credentials.get(self.store.selected, "Civitai")
                else "não cadastrado"
            )
        )

    def _account_changed(self, name):
        previous = self.store.selected
        self._select_profile(self.account_choices[name])
        self._accounts_signature = None
        if self.store.selected != previous:
            self.hf_token.delete(0, "end")
            self.civit_token.delete(0, "end")
            self.remote_metrics = None
            self.had_pc_outputs = False
            self.output_sync_label.configure(text="")
            self.jobs = []
            self.models = []
            self.cache_state = None
            self.cache_profile = None
            self._render_cache()
            self.guard.since = None
            self._dismiss_idle()
            self._render_downloads()
            self._render_library()
        self._render()

    def _render(self):
        if not hasattr(self, "notice_label"):
            return
        self._render_accounts()
        p = self.store.current()
        s = self.snapshot
        self.metric_values["balance"].configure(text=format_units(s.balance, " CU"))
        self.metric_values["rate"].configure(text=format_units(s.rate, " CU/h"))
        self.metric_values["runtime"].configure(
            text=duration(s.balance / s.rate * 3600)
            if s.balance is not None and s.rate
            else "—"
        )
        self.stage_label.configure(text=self.stage)
        cpu = (self.remote_metrics or {}).get(
            "mode"
        ) == "downloads" or s.hardware == "CPU"
        ready = s.session_exists and s.local_ready
        status = "VM desligada"
        if self.setup_error:
            status = self.setup_error
        elif self.busy:
            status = "Operação em andamento"
        elif not p.connected:
            status = "Conecte a conta para começar"
        elif ready:
            status = "Downloads em CPU disponíveis" if cpu else "ComfyUI conectado"
        elif s.session_exists:
            status = "VM ativa · conexão indisponível"
        elif not s.status_known:
            status = "Consultando a sessão…"
        self.status_label.configure(
            text=status, text_color=COLORS["success"] if ready else COLORS["text"]
        )
        for gpu, b in self.gpu_buttons.items():
            selected = gpu == p.gpu
            b.configure(
                state="normal"
                if not self.busy
                and not s.session_exists
                and (s.status_known or not p.connected)
                else "disabled",
                border_color=COLORS["accent"] if selected else COLORS["border"],
                border_width=2 if selected else 1,
            )
        available = not self.busy and not self.setup_error
        self.output_choice.set("Meu PC" if p.output_mode == "pc" else "Google Drive")
        self.output_choice.configure(state="normal" if not self.busy and (s.status_known or not p.connected) else "disabled")
        active_mode = (self.remote_metrics or {}).get("output_mode")
        if ready and active_mode is None:
            output_hint = "Destino ativo ainda não confirmado; sessões anteriores usam Drive. Reiniciar ComfyUI aplica a escolha sem desligar a VM."
        elif ready and active_mode != p.output_mode:
            output_hint = "Destino atual: " + ("PC" if active_mode == "pc" else "Drive") + ". A escolha será aplicada ao clicar Reiniciar ComfyUI; a VM continua ligada."
        elif p.output_mode == "pc":
            output_hint = "PC: download automático com o app aberto e a fila vazia. Até copiar, o arquivo existe só no disco temporário da VM."
        else:
            output_hint = "Drive: os resultados são gravados diretamente em ComfyColab/output."
        self.output_hint.configure(text=output_hint + " A opção controla outputs; entradas e workflows continuam no Drive.")
        start_text = (
            "Conectar conta"
            if not p.connected
            else (
                "Reconectar sessão"
                if s.session_exists
                else "Iniciar downloads CPU"
                if p.gpu == "CPU"
                else "Iniciar sessão"
            )
        )
        self.start_button.configure(
            text="Iniciando…" if self.busy == "start" else start_text,
            state="normal"
            if available and (not p.connected or s.status_known) and not ready
            else "disabled",
            command=self._start,
        )
        self.stop_button.configure(
            text="Cancelar início" if self.busy == "start" else "Encerrar VM",
            state="normal"
            if self.busy == "start"
            or (available and (s.session_exists or not s.status_known))
            else "disabled",
        )
        self.open_button.configure(state="normal" if ready and not cpu else "disabled")
        self.restart_button.configure(
            state="normal"
            if ready and available and not cpu and not self.download_active
            else "disabled"
        )
        self.free_button.configure(
            state="normal" if ready and available and not cpu else "disabled"
        )
        self.image_button.configure(
            text="Atualizando imagem…" if self.busy == "image" else "Atualizar imagem do Drive",
            state="normal" if ready and available and not cpu else "disabled",
        )
        self.reconnect_button.configure(
            state="normal"
            if available and s.session_exists and not s.local_ready
            else "disabled"
        )
        self.download_button.configure(
            state="normal"
            if ready and available and not self.operation_busy
            else "disabled"
        )
        self.refresh_button.configure(
            state="normal" if available and not self.refreshing else "disabled"
        )
        self.add_account_button.configure(state="normal" if available else "disabled")
        if self.auth_mode and self.auth_url:
            self.auth_panel.pack(fill="x", pady=(14, 0))
            self.auth_title.configure(text="Autorizar acesso ao Google")
            self.auth_detail.configure(
                text="Confira a conta e as permissões solicitadas no navegador. Depois conclua aqui."
            )
            if self.auth_mode == "code":
                self.auth_code.pack(side="left", padx=(0, 8))
                self.auth_submit_button.configure(text="Enviar código")
            else:
                self.auth_code.pack_forget()
                self.auth_submit_button.configure(text="Já autorizei")
        else:
            self.auth_panel.pack_forget()

    def _finish_operation(self, code, error):
        operation = self.busy
        profile = self.operation_profile
        seconds = time.monotonic() - self.operation_started
        if profile:
            if operation == "stop" and code == 0:
                self._record_session(profile)
            self.history.event(
                profile,
                operation,
                code,
                seconds,
                self.snapshot.balance,
                self.snapshot.rate,
            )
        self._render_history()
        super()._finish_operation(code, error)
        if self.exit_after_stop and operation == "stop" and code == 0:
            return
        if operation == "drive":
            self.notice(
                "Operação no Drive concluída."
                if code == 0
                else "A operação no Drive parou. Consulte a atividade e tente retomar.",
                code != 0,
            )
        if operation == "stop":
            self.remote_metrics = None
            self.guard.since = None
            self.idle_countdown = None
        if operation == "start" and code == 0:
            self.after(3000, self._load_library)

    def _choose_output(self, value):
        if self.busy:
            return
        profile = self.store.current()
        profile.output_mode = "pc" if value == "Meu PC" else "drive"
        self.store.save()
        self._render()

    def _sync_outputs(self):
        wanted = self.store.current().output_mode == "pc" or self.had_pc_outputs
        if wanted and not self.offline and not self.output_sync_pending and not self.busy and self.snapshot.session_exists and self.snapshot.local_ready:
            profile = self.store.current()
            # Also drain PC files after switching back to Drive in the same VM.
            self.output_sync_pending = True
            def work():
                try:
                    result = self.gateway.run(profile, "bash", f"{self.gateway.linux_root()}/app/output_control.sh", timeout=600)
                    text = (result.stdout if result.returncode == 0 else result.stderr).strip()
                    if result.returncode:
                        text = "Outputs não confirmados no PC. A VM precisa permanecer ligada. " + text[-200:]
                except Exception:
                    text = "Sem confirmação da cópia de outputs. Reconecte antes de encerrar."
                self.events.put(("ui", lambda: self._outputs_synced(profile.id, text)))
            threading.Thread(target=work, daemon=True).start()
        self.after(15000, self._sync_outputs)

    def _outputs_synced(self, profile_id, text):
        self.output_sync_pending = False
        if profile_id == self.store.selected:
            self.output_sync_label.configure(text=text)

    def _tick(self):
        self.elapsed_label.configure(
            text="Tempo desta operação: "
            + duration(time.monotonic() - self.operation_started)
            if self.busy
            else ""
        )
        if self.idle_countdown is not None:
            remaining = max(0, int(self.idle_countdown - time.time()))
            self.notice_label.configure(
                text=f"VM ociosa. Encerramento em {remaining}s. Use “Manter sessão” para cancelar."
            )
            if remaining == 0:
                can_stop = self.guard.update(
                    self.remote_metrics,
                    self.preferences.values["idle_minutes"],
                    blocked=bool(
                        self.busy
                        or self.operation_busy
                        or not self.snapshot.session_exists
                    ),
                )
                self._dismiss_idle()
                if can_stop:
                    self._stop(automatic=True)
        self.after(1000, self._tick)

    def _on_snapshot(self, profile_id, snapshot):
        if profile_id != self.store.selected:
            return
        observed = self.ledger.observe(self.store.current(), snapshot)
        if snapshot.status_known and not snapshot.session_exists:
            self._record_session(self.store.current())
        if observed:
            self.credit_help.configure(
                text="Tempo acompanhado: "
                + duration(time.time() - observed["started"])
                + " · Consumo estimado observado da conta: "
                + format_units(observed["cost"], " CU")
                + " · Não inclui períodos sem leitura."
            )

    def _telemetry(self):
        if not self.telemetry_pending:
            self.telemetry_pending = True
            profile = self.store.selected
            ready = self.snapshot.local_ready

            def work():
                local = local_metrics()
                remote = None
                jobs = None
                error = ""
                if ready:
                    try:
                        remote = _json_request("comfy-colab/metrics")
                        jobs = _json_request("comfy-colab/downloads")["jobs"]
                    except Exception as exc:
                        error = str(exc)
                self.events.put(
                    (
                        "ui",
                        lambda: self._apply_telemetry(
                            profile, local, remote, jobs, error
                        ),
                    )
                )

            threading.Thread(target=work, daemon=True).start()
        self.after(5000, self._telemetry)

    def _apply_telemetry(self, profile, local, remote, jobs, error):
        self.telemetry_pending = False
        self.local_stats = local
        if profile != self.store.selected:
            return
        self.remote_metrics = remote
        self.cache_state = (remote or {}).get("model_cache")
        self._render_cache()
        if (remote or {}).get("output_mode") == "pc":
            self.had_pc_outputs = True
        if jobs is not None:
            self.jobs = jobs
            self.download_active = any(
                j["status"] in ("queued", "running", "cancelling") for j in jobs
            )
            self._render_downloads()
        for prefix, data in [("local", local), ("remote", remote)]:
            if not data:
                for key in ("cpu", "ram", "extra"):
                    self.monitor_labels[prefix + "_" + key].configure(
                        text="Sem leitura da VM · conecte ou atualize a sessão."
                    )
                for key in ("cpu", "ram"):
                    self.monitor_bars[prefix + "_" + key].set(0)
                continue
            self.monitor_labels[prefix + "_cpu"].configure(
                text=f"CPU · {data['cpu_percent']:.0f}%"
            )
            self.monitor_bars[prefix + "_cpu"].set(data["cpu_percent"] / 100)
            self.monitor_labels[prefix + "_ram"].configure(
                text=f"RAM · {size(data['ram_used'])} usados de {size(data['ram_total'])}"
            )
            self.monitor_bars[prefix + "_ram"].set(
                data["ram_used"] / max(data["ram_total"], 1)
            )
            self.monitor_labels[prefix + "_extra"].configure(
                text=f"Disponível: {size(data['ram_available'])}"
                + (
                    f" · Aplicativo: {size(data['app_ram'])}"
                    if prefix == "local"
                    else ""
                )
            )
        if remote:
            gpu = remote.get("gpus", [])
            self.gpu_label.configure(
                text="\n".join(
                    f"{g['name']} · GPU {g['percent']:.0f}% · VRAM {size(g['used'])} / {size(g['total'])} · {g['temperature']:.0f} °C"
                    for g in gpu
                )
                + f"\nDisco temporário livre: {size(remote['disk_free'])} de {size(remote['disk_total'])}"
            )
            self.gpu_bar.set(gpu[0]["used"] / gpu[0]["total"] if gpu else 0)
        else:
            self.gpu_bar.set(0)
            self.gpu_label.configure(text="Sem dados recentes da GPU.")
        self.monitor_status.configure(
            text=error or "Última leitura: " + time.strftime("%H:%M:%S")
        )
        should_stop = self.guard.update(
            remote,
            self.preferences.values["idle_minutes"],
            blocked=bool(
                self.busy or self.operation_busy or not self.snapshot.session_exists
            ),
        )
        if not should_stop:
            self._dismiss_idle()
        elif self.idle_countdown is None:
            self.idle_countdown = time.time() + 30
            self.idle_dialog = ConfirmDialog(
                self,
                "Sessão ociosa",
                "A VM será encerrada em 30 segundos se continuar sem geração ou download.",
                [
                    ("Manter sessão", self._keep_session, "primary"),
                    ("Encerrar agora", lambda: self._stop(automatic=True), "secondary"),
                ],
                on_cancel=self._keep_session,
            )
        if (
            self.snapshot.balance is not None
            and self.snapshot.balance < self.preferences.values["balance_alert"]
        ):
            if not self.low_alerted:
                self.notice(
                    "Saldo baixo: "
                    + format_units(self.snapshot.balance, " CU")
                    + ". Confira o consumo antes de continuar."
                )
                self.low_alerted = True
        else:
            self.low_alerted = False
        self._render()

    def _stop(self, force=False, automatic=False):
        self._dismiss_idle()
        if force or self.busy:
            return super()._stop(force=force)
        if self.operation_busy:
            self.notice("Aguarde o envio do lote de downloads antes de encerrar.")
            return
        profile = self.store.selected

        def work():
            try:
                return _json_request("comfy-colab/metrics")
            except Exception:
                return None

        def checked(metrics):
            if profile != self.store.selected or self.busy:
                return
            known = (
                isinstance(metrics, dict)
                and metrics.get("queue_busy") is not None
                and "downloads_busy" in metrics
            )
            busy = known and (metrics["queue_busy"] or metrics["downloads_busy"])
            if automatic:
                if known and not busy:
                    super(Dashboard, self)._stop(force=True)
                else:
                    self._keep_session()
            elif busy or not known:
                ConfirmDialog(
                    self,
                    "Encerrar VM",
                    "Há geração/download em andamento. Encerrar interrompe essas tarefas."
                    if busy
                    else "Não foi possível verificar a fila. Encerrar pode interromper tarefas na VM.",
                    [
                        ("Voltar", lambda: None, "secondary"),
                        (
                            "Encerrar mesmo assim",
                            lambda: self._stop(force=True),
                            "primary",
                        ),
                    ],
                )
            else:
                super(Dashboard, self)._stop(force=True)

        self._async(work, checked)

    def _keep_session(self):
        self.guard.since = None
        self._dismiss_idle()
        self.notice("Sessão mantida. O período de inatividade recomeçou.")

    def _dismiss_idle(self):
        self.idle_countdown = None
        if self.idle_dialog and self.idle_dialog.winfo_exists():
            self.idle_dialog.grab_release()
            self.idle_dialog.destroy()
        self.idle_dialog = None

    def _record_session(self, profile):
        session = self.ledger.finish(profile)
        if session:
            self.history.event(
                profile,
                "session",
                0,
                session["duration"],
                self.snapshot.balance,
                self.snapshot.rate,
                estimated_cu=session["cost"],
                measured_seconds=session["measured"],
                unmeasured_seconds=session["gaps"],
            )
            self._render_history()

    def _submit_downloads(self):
        if not self.snapshot.local_ready or self.busy or self.operation_busy:
            self.notice("Inicie uma sessão antes de adicionar downloads.")
            return
        try:
            entries = []
            for line in self.urls.get("1.0", "end").splitlines():
                if not line.strip():
                    continue
                parts = line.split("|", 1)
                entries.append(
                    prepare_download(
                        parts[0],
                        parts[1] if len(parts) > 1 else "",
                        self.category.get(),
                    )
                )
            if not entries:
                raise ValueError("Cole ao menos um link de arquivo.")
            if len(entries) > 100:
                raise ValueError("Adicione até 100 links por lote.")
        except ValueError as exc:
            self.notice(str(exc), True)
            return
        profile = self.store.selected
        tokens = {
            provider: self.credentials.get(profile, provider)
            for _, _, _, provider in entries
        }
        parallel = int(self.parallel.get())
        self.operation_busy = True
        self._render()

        def work():
            errors = []
            count = 0
            try:
                _json_request("comfy-colab/downloads/settings", {"parallel": parallel})
                for url, name, category, provider in entries:
                    try:
                        start_download(url, name, category, tokens[provider])
                        count += 1
                    except Exception as exc:
                        errors.append(name + ": " + str(exc))
            except Exception as exc:
                errors.append(str(exc))
            self.events.put(("ui", lambda: self._downloads_submitted(count, errors)))

        threading.Thread(target=work, daemon=True).start()

    def _downloads_submitted(self, count, errors):
        self.operation_busy = False
        if not errors:
            self.urls.delete("1.0", "end")
        self.notice(
            f"{count} download(s) adicionados à fila."
            + (" " + "\n".join(errors) if errors else ""),
            bool(errors),
        )

    def _change_parallel(self, value):
        self.preferences.values["parallel"] = int(value)
        self.preferences.save()
        if self.snapshot.local_ready:
            self._async(
                lambda: _json_request(
                    "comfy-colab/downloads/settings", {"parallel": int(value)}
                )
            )

    def _render_downloads(self):
        selected = self.download_tree.selection()
        self.download_tree.delete(*self.download_tree.get_children())
        for job in sorted(self.jobs, key=lambda j: j.get("created", 0), reverse=True):
            progress = f"{size(job['bytes'])} / {size(job.get('total'))}"
            self.download_tree.insert(
                "",
                "end",
                iid=job["id"],
                values=(
                    job["name"],
                    STATUS.get(job["status"], job["status"]),
                    progress,
                    size(job.get("speed", 0)) + "/s",
                    duration(job.get("eta")),
                ),
            )
        if selected and self.download_tree.exists(selected[0]):
            self.download_tree.selection_set(selected[0])
        self.download_hint.configure(
            text=f"{len(self.jobs)} itens no histórico deste Drive. "
            + (
                "Há transferências em andamento."
                if self.download_active
                else "Nenhum download em andamento."
            )
        )

    def _selected_job(self):
        selected = self.download_tree.selection()
        return next((j for j in self.jobs if selected and j["id"] == selected[0]), None)

    def _cancel_download(self):
        if not self.snapshot.local_ready:
            self.notice("Reconecte a sessão para cancelar downloads.")
            return
        job = self._selected_job()
        if job:
            self._async(
                lambda: _json_request(
                    "comfy-colab/model-download/" + job["id"] + "/cancel", {}
                ),
                lambda _: self.notice(
                    "Cancelamento solicitado. O parcial permanece no Drive."
                ),
            )
        else:
            self.notice("Selecione um download na lista.")

    def _retry_download(self):
        if not self.snapshot.local_ready:
            self.notice("Inicie uma sessão para retomar downloads.")
            return
        job = self._selected_job()
        if not job:
            self.notice("Selecione um download na lista.")
            return
        if job["status"] in ("running", "queued", "cancelling"):
            self.notice("Esse download ainda está em andamento.")
            return
        provider = "Hugging Face" if "huggingface.co/" in job["url"] else "Civitai"
        token = self.credentials.get(self.store.selected, provider)
        self._async(
            lambda: start_download(job["url"], job["name"], job["directory"], token),
            lambda _: self.notice("Download recolocado na fila."),
        )

    def _load_library(self):
        if not self.snapshot.local_ready:
            self.notice("Inicie uma sessão para consultar a biblioteca.")
            return
        profile = self.store.selected

        def done(data):
            if profile != self.store.selected:
                return
            self.models = data["models"]
            self._render_library()
            self.library_hint.configure(
                text=f"{len(self.models)} modelos · {size(sum(m['bytes'] for m in self.models))} · destino: Meu Drive / ComfyColab / models"
            )

        self._async(lambda: _json_request("comfy-colab/models"), done)

    def _render_library(self):
        query = self.search.get().strip().casefold()
        selected = self.library_tree.selection()
        self.library_tree.delete(*self.library_tree.get_children())
        for m in self.models:
            if query not in (m["name"] + " " + m["category"]).casefold():
                continue
            self.library_tree.insert(
                "", "end", iid=m["category"] + "/" + m["name"],
                values=(m["name"], m["category"], size(m["bytes"]))
            )
        self.library_tree.selection_set([v for v in selected if self.library_tree.exists(v)])

    def _cache_options(self):
        return dict(enabled=bool(self.cache_enabled.get()), warm_ram=bool(self.cache_warm.get()),
                    ram_gib=int(self.cache_limit.get().split()[0]))

    def _cache_action(self, action, payload=None):
        if self.offline or not self.cache_state or self.busy or self.cache_pending:
            self.notice("Conecte a versão atualizada do ComfyUI para preparar os modelos.")
            return
        profile = self.store.selected
        self.cache_pending = True
        self._render_cache()

        def work():
            try:
                return _json_request("comfy-colab/cache/" + action, payload or {}), ""
            except Exception as exc:
                return None, str(exc)

        def done(result):
            self.cache_pending = False
            if profile != self.store.selected:
                return
            state, error = result
            if state:
                self.cache_state = state
            self.cache_profile = None
            self._render_cache()
            self.notice(error or {"prepare": "Preparação iniciada na VM. Acompanhe abaixo; pode fechar o app.",
                                  "settings": "Preferências do cache salvas neste Drive.",
                                  "clear": "Cache temporário removido. Modelos do Drive preservados.",
                                  "cancel": "Cancelamento solicitado. Cópias completas serão preservadas."}[action], bool(error))

        self._async(work, done)

    def _cache_settings(self, _=None):
        self._cache_action("settings", self._cache_options())

    def _prepare_cache(self):
        selected = list(self.library_tree.selection())
        if not selected:
            self.notice("Selecione os modelos na biblioteca com Ctrl ou use Selecionar por workflow.")
            return
        if not self.cache_enabled.get():
            self.notice("Ative Usar cache da VM antes de preparar os modelos.")
            return
        self._cache_action("prepare", dict(self._cache_options(), models=selected))

    def _select_cache_workflow(self):
        if not self.snapshot.local_ready:
            self.notice("Conecte a VM para consultar os modelos do workflow.")
            return
        path = filedialog.askopenfilename(title="Selecionar modelos do workflow", filetypes=[("Workflow ComfyUI", "*.json")])
        if not path:
            return
        profile = self.store.selected

        def work():
            models = _json_request("comfy-colab/models")["models"]
            return models, workflow_cache_selection(path, models)

        def done(result):
            if profile != self.store.selected:
                return
            self.models, (selected, unresolved) = result
            self.search.delete(0, "end")
            self._render_library()
            self.library_tree.selection_set(sorted(selected))
            if selected:
                self.library_tree.see(sorted(selected)[0])
            self.notice(f"{len(selected)} modelos selecionados. Confira a lista e clique em Preparar selecionados."
                        + (" Não selecionados: " + "; ".join(unresolved) if unresolved else ""))

        self._async(work, done)

    def _render_cache(self):
        if not hasattr(self, "cache_hint"):
            return
        state = self.cache_state
        available = state is not None and not self.offline and not self.busy and not self.cache_pending
        busy = bool(state and state.get("busy"))
        self.cache_enabled.configure(state="normal" if available else "disabled")
        for widget in (self.cache_warm, self.cache_limit, self.cache_prepare, self.cache_clear):
            widget.configure(state="normal" if available and not busy else "disabled")
        self.cache_cancel.configure(state="normal" if available and busy else "disabled")
        if not state:
            self.cache_tree.delete(*self.cache_tree.get_children())
            self._cache_items_signature = None
            self.cache_hint.configure(text="Conecte o ComfyUI atualizado para usar o cache. Na sessão antiga, use Reiniciar ComfyUI quando a fila estiver vazia.")
            return
        if self.cache_profile != self.store.selected:
            settings = state["settings"]
            for widget, key in ((self.cache_enabled, "enabled"), (self.cache_warm, "warm_ram")):
                widget.select() if settings[key] else widget.deselect()
            self.cache_limit.set(str(settings["ram_gib"]) + " GiB")
            self.cache_profile = self.store.selected
        labels = dict(idle="Aguardando seleção", queued="Na fila", copying="Copiando para a VM",
                      warming="Preparando RAM", waiting="Aguardando a geração terminar",
                      cached="Cache pronto", warm="Pré-leitura concluída", complete="Preparação concluída",
                      error="Falha", cancelled="Cancelado")
        items = state.get("items", [])
        signature = json.dumps(items, sort_keys=True)
        if getattr(self, "_cache_items_signature", None) != signature:
            view = self.cache_tree.yview()
            self.cache_tree.delete(*self.cache_tree.get_children())
            for item in items:
                detail = item.get("error") or item.get("note") or f"{size(item['bytes'])} / {size(item['total'])}"
                if item["status"] == "warming":
                    detail = f"{size(item.get('warm_bytes', 0))} / {size(item['total'])} lidos"
                self.cache_tree.insert("", "end", values=(item["path"], labels.get(item["status"], item["status"]), detail))
            self.cache_tree.yview_moveto(view[0] if view else 0)
            self._cache_items_signature = signature
        self.cache_hint.configure(text=(state.get("error") or labels.get(state["status"], state["status"]))
                                  + f" · {len(items)} arquivos · {state.get('seconds', 0):.1f}s"
                                  + ("\nÚltimo modelo lido pelo cache: " + state["last_loaded"] if state.get("last_loaded") else ""))

    def _save_credentials(self):
        try:
            for provider, field in [
                ("Hugging Face", self.hf_token),
                ("Civitai", self.civit_token),
            ]:
                if field.get().strip():
                    self.credentials.set(
                        self.store.selected, provider, field.get().strip()
                    )
                    field.delete(0, "end")
            self._accounts_signature = None
            self.notice("Credenciais salvas e protegidas neste Windows.")
        except Exception as exc:
            self.notice(str(exc), True)

    def _remove_credentials(self):
        def remove():
            for provider in ("Hugging Face", "Civitai"):
                self.credentials.set(self.store.selected, provider, "")
            self._accounts_signature = None
            self.notice("Credenciais removidas da conta selecionada.")

        ConfirmDialog(
            self,
            "Remover credenciais",
            "Remove os tokens de download salvos para a conta selecionada.",
            [("Cancelar", lambda: None, "secondary"), ("Remover", remove, "primary")],
        )

    def _save_preferences(self):
        try:
            minutes = int(self.idle_entry.get())
            balance = float(self.balance_entry.get().replace(",", "."))
            if (
                not 0 <= minutes <= 1440
                or not math.isfinite(balance)
                or not 0 <= balance <= 100000
            ):
                raise ValueError()
        except ValueError:
            self.notice("Use de 0 a 1440 minutos e um saldo entre 0 e 100000.", True)
            return
        self.preferences.values.update(
            idle_minutes=minutes,
            balance_alert=balance,
            auto_open=bool(self.auto_open.get()),
        )
        self.preferences.save()
        self.guard.since = None
        self._dismiss_idle()
        self.notice(
            "Preferências salvas. "
            + (
                "Encerramento por inatividade ativado."
                if minutes
                else "Encerramento automático desativado."
            )
        )

    def _drive_action(self, action):
        if self.busy:
            self.notice("Aguarde a operação atual.")
            return
        if self.offline:
            self.notice("Prévia local: conexão com Drive desativada.")
            return
        source = self.store.current()
        if not source.connected or not source.email:
            self.notice(
                "Conecte a conta de origem ao Colab antes de autorizar o Drive.", True
            )
            return
        destid = self.account_choices.get(self.drive_destination.get())
        dest = next((p for p in self.store.profiles if p.id == destid), None)
        if action != "authorize" and not dest:
            self.notice(
                "Adicione uma segunda conta antes de transferir arquivos.", True
            )
            return
        if action != "authorize" and (not dest.connected or not dest.email):
            self.notice(
                "Conecte a conta de destino ao Colab antes de transferir arquivos.",
                True,
            )
            return
        folder = self.drive_folder.get().strip() or "ComfyColab"
        if folder != "ComfyColab" and not __import__("re").fullmatch(
            r"[A-Za-z0-9_-]+", folder
        ):
            self.notice("Informe ComfyColab ou apenas o ID da pasta.", True)
            return

        def launch():
            command = (
                "@python",
                f"{self.gateway.linux_root()}/app/drive_transfer.py",
                action,
                "--source",
                source.id,
                "--source-email",
                source.email,
                "--folder",
                folder,
            )
            if dest:
                command += ("--destination", dest.id, "--destination-email", dest.email)
            self.show_page("Sessão")
            self._launch("drive", source, command)

        if action in ("copy", "share"):
            detail = (
                f"Origem: {source.email or source.label}\nDestino: {dest.email or dest.label}\n"
                + (
                    "Copiar arquivos e conceder leitura da origem ao destino."
                    if action == "copy"
                    else "Compartilhar models com permissão de edição nas duas contas."
                )
            )
            ConfirmDialog(
                self,
                "Confirmar " + ("cópia" if action == "copy" else "compartilhamento"),
                detail,
                [
                    ("Cancelar", lambda: None, "secondary"),
                    (
                        "Copiar / retomar" if action == "copy" else "Compartilhar",
                        launch,
                        "primary",
                    ),
                ],
            )
        else:
            launch()

    def _check_workflow(self):
        if not self.snapshot.local_ready:
            self.notice("Conecte o ComfyUI antes de verificar dependências.")
            return
        path = filedialog.askopenfilename(
            title="Verificar workflow", filetypes=[("Workflow ComfyUI", "*.json")]
        )
        if not path:
            return

        def work():
            return check_workflow(
                path,
                _json_request("object_info"),
                _json_request("comfy-colab/models")["models"],
            )

        def done(rows):
            self.workflow_tree.delete(*self.workflow_tree.get_children())
            for row in rows:
                self.workflow_tree.insert("", "end", values=row)
            self.notice("Verificação concluída: " + Path(path).name)

        self._async(work, done)

    def _render_history(self):
        self.history_tree.delete(*self.history_tree.get_children())
        names = {
            "start": "Iniciar",
            "stop": "Encerrar",
            "connect": "Conectar",
            "restart": "Reiniciar",
            "drive": "Google Drive",
            "image": "Atualizar imagem",
            "session": "Sessão observada",
        }
        for h in reversed(self.history.rows):
            self.history_tree.insert(
                "",
                "end",
                values=(
                    time.strftime("%d/%m %H:%M", time.localtime(h["time"])),
                    h["account"],
                    names.get(h["operation"], h["operation"]),
                    h["gpu"],
                    duration(h["seconds"]),
                    format_units(h.get("estimated_cu")),
                    h["result"],
                ),
            )

    def _export_history(self):
        target = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile="historico-comfy-colab.json",
            filetypes=[("JSON", "*.json")],
        )
        if target:
            Path(target).write_text(
                json.dumps(self.history.rows, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.notice("Histórico exportado.")

    def _export_diagnostics(self):
        target = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile="diagnostico-comfy-colab.json",
            filetypes=[("JSON", "*.json")],
        )
        if target:
            Path(target).write_text(
                json.dumps(
                    {
                        "version": VERSION,
                        "setup_error": self.setup_error,
                        "gpu_requested": self.store.current().gpu,
                        "session": self.snapshot.__dict__,
                        "local": self.local_stats,
                        "remote": self.remote_metrics,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.notice("Diagnóstico exportado sem tokens ou chaves.")


def main():
    app = Dashboard(offline="--preview" in sys.argv)
    app.mainloop()


if __name__ == "__main__":
    main()
