"""Easy Comfy Colab: interface Windows com serviços assíncronos."""

from __future__ import annotations
from i18n import EN, LANGUAGES, get_language, set_language, tr
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
        set_language(self.preferences.values["language"])
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
                ("initialized", tr("Modo de prévia: nenhuma conexão será aberta."))
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
            f"{index + 1}. {tr(p.label) if p.id == 'default' and p.label == 'Conta principal' else p.label} · {p.email or tr('não conectada')}": p.id
            for index, p in enumerate(self.store.profiles)
        }
        self.account_selector.configure(values=list(self.account_choices))
        self.account_selector.set(
            next(k for k, v in self.account_choices.items() if v == self.store.selected)
        )
        dest = [k for k, v in self.account_choices.items() if v != self.store.selected]
        self.drive_destination.configure(values=dest or [tr("Adicione outra conta")])
        self.drive_destination.set(dest[0] if dest else tr("Adicione outra conta"))
        self.account_tree.delete(*self.account_tree.get_children())
        for p in self.store.profiles:
            self.account_tree.insert(
                "", "end", iid=p.id,
                values=(tr(p.label) if p.id == "default" and p.label == "Conta principal" else p.label,
                        p.email or tr("Não conectada"), p.id)
            )
        self.credential_status.configure(
            text="Hugging Face: "
            + (
                tr("salvo")
                if self.credentials.get(self.store.selected, "Hugging Face")
                else tr("não cadastrado")
            )
            + "  ·  Civitai: "
            + (
                tr("salvo")
                if self.credentials.get(self.store.selected, "Civitai")
                else tr("não cadastrado")
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
        status = tr("VM desligada")
        if self.setup_error:
            status = self.setup_error
        elif self.busy:
            status = tr("Operação em andamento")
        elif not p.connected:
            status = tr("Conecte a conta para começar")
        elif ready:
            status = tr("Downloads em CPU disponíveis") if cpu else tr("ComfyUI conectado")
        elif s.session_exists:
            status = tr("VM ativa · conexão indisponível")
        elif not s.status_known:
            status = tr("Consultando a sessão…")
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
        self.output_choice.set(tr("Meu PC") if p.output_mode == "pc" else "Google Drive")
        self.output_choice.configure(state="normal" if not self.busy and (s.status_known or not p.connected) else "disabled")
        active_mode = (self.remote_metrics or {}).get("output_mode")
        if ready and active_mode is None:
            output_hint = tr("Destino ativo ainda não confirmado; sessões anteriores usam Drive. Reiniciar ComfyUI aplica a escolha sem desligar a VM.")
        elif ready and active_mode != p.output_mode:
            output_hint = tr("Destino atual: ") + ("PC" if active_mode == "pc" else "Drive") + tr(". A escolha será aplicada ao clicar Reiniciar ComfyUI; a VM continua ligada.")
        elif p.output_mode == "pc":
            output_hint = tr("PC: download automático com o app aberto e a fila vazia. Até copiar, o arquivo existe só no disco temporário da VM.")
        else:
            output_hint = tr("Drive: os resultados são gravados diretamente em ComfyColab/output.")
        self.output_hint.configure(text=output_hint + tr(" A opção controla outputs; entradas e workflows continuam no Drive."))
        start_text = (
            tr("Conectar conta")
            if not p.connected
            else (
                tr("Reconectar sessão")
                if s.session_exists
                else tr("Iniciar downloads CPU")
                if p.gpu == "CPU"
                else tr("Iniciar sessão")
            )
        )
        self.start_button.configure(
            text=tr("Iniciando…") if self.busy == "start" else start_text,
            state="normal"
            if available and (not p.connected or s.status_known) and not ready
            else "disabled",
            command=self._start,
        )
        self.stop_button.configure(
            text=tr("Cancelar início") if self.busy == "start" else tr("Encerrar VM"),
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
            text=tr("Atualizando imagem…") if self.busy == "image" else tr("Atualizar imagem do Drive"),
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
            self.auth_title.configure(text=tr("Autorizar acesso ao Google"))
            self.auth_detail.configure(
                text=tr("Confira a conta e as permissões solicitadas no navegador. Depois conclua aqui.")
            )
            if self.auth_mode == "code":
                self.auth_code.pack(side="left", padx=(0, 8))
                self.auth_submit_button.configure(text=tr("Enviar código"))
            else:
                self.auth_code.pack_forget()
                self.auth_submit_button.configure(text=tr("Já autorizei"))
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
                tr("Operação no Drive concluída.")
                if code == 0
                else tr("A operação no Drive parou. Consulte a atividade e tente retomar."),
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
        profile.output_mode = "pc" if value == tr("Meu PC") else "drive"
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
                        text = tr("Outputs não confirmados no PC. A VM precisa permanecer ligada. ") + text[-200:]
                except Exception:
                    text = tr("Sem confirmação da cópia de outputs. Reconecte antes de encerrar.")
                self.events.put(("ui", lambda: self._outputs_synced(profile.id, text)))
            threading.Thread(target=work, daemon=True).start()
        self.after(15000, self._sync_outputs)

    def _outputs_synced(self, profile_id, text):
        self.output_sync_pending = False
        if profile_id == self.store.selected:
            self.output_sync_label.configure(text=text)

    def _tick(self):
        self.elapsed_label.configure(
            text=tr("Tempo desta operação: ")
            + duration(time.monotonic() - self.operation_started)
            if self.busy
            else ""
        )
        if self.idle_countdown is not None:
            remaining = max(0, int(self.idle_countdown - time.time()))
            self.notice_label.configure(
                text=tr("VM ociosa. Encerramento em {p0}s. Use “Manter sessão” para cancelar.", p0=remaining)
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
                text=tr("Tempo acompanhado: ")
                + duration(time.time() - observed["started"])
                + tr(" · Consumo estimado observado da conta: ")
                + format_units(observed["cost"], " CU")
                + tr(" · Não inclui períodos sem leitura.")
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
                        text=tr("Sem leitura da VM · conecte ou atualize a sessão.")
                    )
                for key in ("cpu", "ram"):
                    self.monitor_bars[prefix + "_" + key].set(0)
                continue
            self.monitor_labels[prefix + "_cpu"].configure(
                text=f"CPU · {data['cpu_percent']:.0f}%"
            )
            self.monitor_bars[prefix + "_cpu"].set(data["cpu_percent"] / 100)
            self.monitor_labels[prefix + "_ram"].configure(
                text=tr("RAM · {p0} usados de {p1}", p0=size(data['ram_used']), p1=size(data['ram_total']))
            )
            self.monitor_bars[prefix + "_ram"].set(
                data["ram_used"] / max(data["ram_total"], 1)
            )
            self.monitor_labels[prefix + "_extra"].configure(
                text=tr("Disponível: {p0}", p0=size(data['ram_available']))
                + (
                    tr(" · Aplicativo: {p0}", p0=size(data['app_ram']))
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
                + tr("\nDisco temporário livre: {p0} de {p1}", p0=size(remote['disk_free']), p1=size(remote['disk_total']))
            )
            self.gpu_bar.set(gpu[0]["used"] / gpu[0]["total"] if gpu else 0)
        else:
            self.gpu_bar.set(0)
            self.gpu_label.configure(text=tr("Sem dados recentes da GPU."))
        self.monitor_status.configure(
            text=error or tr("Última leitura: ") + time.strftime("%H:%M:%S")
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
                tr("Sessão ociosa"),
                tr("A VM será encerrada em 30 segundos se continuar sem geração ou download."),
                [
                    (tr("Manter sessão"), self._keep_session, "primary"),
                    (tr("Encerrar agora"), lambda: self._stop(automatic=True), "secondary"),
                ],
                on_cancel=self._keep_session,
            )
        if (
            self.snapshot.balance is not None
            and self.snapshot.balance < self.preferences.values["balance_alert"]
        ):
            if not self.low_alerted:
                self.notice(
                    tr("Saldo baixo: ")
                    + format_units(self.snapshot.balance, " CU")
                    + tr(". Confira o consumo antes de continuar.")
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
            self.notice(tr("Aguarde o envio do lote de downloads antes de encerrar."))
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
                    tr("Encerrar VM"),
                    tr("Há geração/download em andamento. Encerrar interrompe essas tarefas.")
                    if busy
                    else tr("Não foi possível verificar a fila. Encerrar pode interromper tarefas na VM."),
                    [
                        (tr("Voltar"), lambda: None, "secondary"),
                        (
                            tr("Encerrar mesmo assim"),
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
        self.notice(tr("Sessão mantida. O período de inatividade recomeçou."))

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
            self.notice(tr("Inicie uma sessão antes de adicionar downloads."))
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
                raise ValueError(tr("Cole ao menos um link de arquivo."))
            if len(entries) > 100:
                raise ValueError(tr("Adicione até 100 links por lote."))
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
            tr("{p0} download(s) adicionados à fila.", p0=count)
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
                    tr(STATUS.get(job["status"], job["status"])),
                    progress,
                    size(job.get("speed", 0)) + "/s",
                    duration(job.get("eta")),
                ),
            )
        if selected and self.download_tree.exists(selected[0]):
            self.download_tree.selection_set(selected[0])
        self.download_hint.configure(
            text=tr("{p0} itens no histórico deste Drive. ", p0=len(self.jobs))
            + (
                tr("Há transferências em andamento.")
                if self.download_active
                else tr("Nenhum download em andamento.")
            )
        )

    def _selected_job(self):
        selected = self.download_tree.selection()
        return next((j for j in self.jobs if selected and j["id"] == selected[0]), None)

    def _cancel_download(self):
        if not self.snapshot.local_ready:
            self.notice(tr("Reconecte a sessão para cancelar downloads."))
            return
        job = self._selected_job()
        if job:
            self._async(
                lambda: _json_request(
                    "comfy-colab/model-download/" + job["id"] + "/cancel", {}
                ),
                lambda _: self.notice(
                    tr("Cancelamento solicitado. O parcial permanece no Drive.")
                ),
            )
        else:
            self.notice(tr("Selecione um download na lista."))

    def _retry_download(self):
        if not self.snapshot.local_ready:
            self.notice(tr("Inicie uma sessão para retomar downloads."))
            return
        job = self._selected_job()
        if not job:
            self.notice(tr("Selecione um download na lista."))
            return
        if job["status"] in ("running", "queued", "cancelling"):
            self.notice(tr("Esse download ainda está em andamento."))
            return
        provider = "Hugging Face" if "huggingface.co/" in job["url"] else "Civitai"
        token = self.credentials.get(self.store.selected, provider)
        self._async(
            lambda: start_download(job["url"], job["name"], job["directory"], token),
            lambda _: self.notice(tr("Download recolocado na fila.")),
        )

    def _load_library(self):
        if not self.snapshot.local_ready:
            self.notice(tr("Inicie uma sessão para consultar a biblioteca."))
            return
        profile = self.store.selected

        def done(data):
            if profile != self.store.selected:
                return
            self.models = data["models"]
            self._render_library()
            self.library_hint.configure(
                text=tr("{p0} modelos · {p1} · destino: Meu Drive / ComfyColab / models", p0=len(self.models), p1=size(sum(m['bytes'] for m in self.models)))
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
            self.notice(tr("Conecte a versão atualizada do ComfyUI para preparar os modelos."))
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
            self.notice(error or {"prepare": tr("Preparação iniciada na VM. Acompanhe abaixo; pode fechar o app."),
                                  "settings": tr("Preferências do cache salvas neste Drive."),
                                  "clear": tr("Cache temporário removido. Modelos do Drive preservados."),
                                  "cancel": tr("Cancelamento solicitado. Cópias completas serão preservadas.")}[action], bool(error))

        self._async(work, done)

    def _cache_settings(self, _=None):
        self._cache_action("settings", self._cache_options())

    def _prepare_cache(self):
        selected = list(self.library_tree.selection())
        if not selected:
            self.notice(tr("Selecione os modelos na biblioteca com Ctrl ou use Selecionar por workflow."))
            return
        if not self.cache_enabled.get():
            self.notice(tr("Ative Usar cache da VM antes de preparar os modelos."))
            return
        self._cache_action("prepare", dict(self._cache_options(), models=selected))

    def _select_cache_workflow(self):
        if not self.snapshot.local_ready:
            self.notice(tr("Conecte a VM para consultar os modelos do workflow."))
            return
        path = filedialog.askopenfilename(title=tr("Selecionar modelos do workflow"), filetypes=[(tr("Workflow ComfyUI"), "*.json")])
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
            self.notice(tr("{p0} modelos selecionados. Confira a lista e clique em Preparar selecionados.", p0=len(selected))
                        + (tr(" Não selecionados: ") + "; ".join(unresolved) if unresolved else ""))

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
            self.cache_hint.configure(text=tr("Conecte o ComfyUI atualizado para usar o cache. Na sessão antiga, use Reiniciar ComfyUI quando a fila estiver vazia."))
            return
        if self.cache_profile != self.store.selected:
            settings = state["settings"]
            for widget, key in ((self.cache_enabled, "enabled"), (self.cache_warm, "warm_ram")):
                widget.select() if settings[key] else widget.deselect()
            self.cache_limit.set(str(settings["ram_gib"]) + " GiB")
            self.cache_profile = self.store.selected
        labels = dict(idle=tr("Aguardando seleção"), queued=tr("Na fila"), copying=tr("Copiando para a VM"),
                      warming=tr("Preparando RAM"), waiting=tr("Aguardando a geração terminar"),
                      cached=tr("Cache pronto"), warm=tr("Pré-leitura concluída"), complete=tr("Preparação concluída"),
                      error=tr("Falha"), cancelled=tr("Cancelado"))
        items = state.get("items", [])
        signature = json.dumps(items, sort_keys=True)
        if getattr(self, "_cache_items_signature", None) != signature:
            view = self.cache_tree.yview()
            self.cache_tree.delete(*self.cache_tree.get_children())
            for item in items:
                detail = item.get("error") or item.get("note") or f"{size(item['bytes'])} / {size(item['total'])}"
                if item["status"] == "warming":
                    detail = tr("{p0} / {p1} lidos", p0=size(item.get('warm_bytes', 0)), p1=size(item['total']))
                self.cache_tree.insert("", "end", values=(item["path"], labels.get(item["status"], item["status"]), detail))
            self.cache_tree.yview_moveto(view[0] if view else 0)
            self._cache_items_signature = signature
        self.cache_hint.configure(text=(state.get("error") or labels.get(state["status"], state["status"]))
                                  + tr(" · {p0} arquivos · {p1:.1f}s", p0=len(items), p1=state.get('seconds', 0))
                                  + (tr("\nÚltimo modelo lido pelo cache: ") + state["last_loaded"] if state.get("last_loaded") else ""))

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
            self.notice(tr("Credenciais salvas e protegidas neste Windows."))
        except Exception as exc:
            self.notice(str(exc), True)

    def _remove_credentials(self):
        def remove():
            for provider in ("Hugging Face", "Civitai"):
                self.credentials.set(self.store.selected, provider, "")
            self._accounts_signature = None
            self.notice(tr("Credenciais removidas da conta selecionada."))

        ConfirmDialog(
            self,
            tr("Remover credenciais"),
            tr("Remove os tokens de download salvos para a conta selecionada."),
            [(tr("Cancelar"), lambda: None, "secondary"), (tr("Remover"), remove, "primary")],
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
            self.notice(tr("Use de 0 a 1440 minutos e um saldo entre 0 e 100000."), True)
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
            tr("Preferências salvas. ")
            + (
                tr("Encerramento por inatividade ativado.")
                if minutes
                else tr("Encerramento automático desativado.")
            )
        )

    def _change_language(self, label):
        language = next((key for key, value in LANGUAGES.items() if value == label), None)
        previous = get_language()
        if language is None or language == previous:
            return
        self.preferences.values["language"] = language
        try:
            self.preferences.save()
        except OSError as exc:
            self.preferences.values["language"] = previous
            self.language_choice.set(LANGUAGES[previous])
            self.notice(tr("Não foi possível salvar o idioma: {error}", error=str(exc)), True)
            return

        # Rebuild only presentation; connections, workers and timers keep running.
        page = self.current_page
        views = {name: frame._parent_canvas.yview()[0] for name, frame in self.pages.items()}
        entries = {name: getattr(self, name).get() for name in (
            "search", "drive_folder", "hf_token", "civit_token", "idle_entry", "balance_entry", "auth_code")}
        choices = {name: getattr(self, name).get() for name in ("category", "parallel", "cache_limit")}
        checks = {name: getattr(self, name).get() for name in ("auto_open", "cache_enabled", "cache_warm")}
        texts = {name: getattr(self, name).get("1.0", "end-1c") for name in ("urls", "log_box")}
        selected = {name: getattr(self, name).selection() for name in ("library_tree", "download_tree")}
        destination = self.account_choices.get(self.drive_destination.get())
        rows = [self.workflow_tree.item(item, "values") for item in self.workflow_tree.get_children()]
        reverse = {value: key for key, value in EN.items()} if previous == "en" else {}
        rows = [(reverse.get(row[0], row[0]), row[1], reverse.get(row[2], row[2])) for row in rows]
        for sequence, binding in self._scroll_bindings.items():
            self.tk.call("bind", "all", sequence, binding)
        for command in self._ui_root_commands:
            self.deletecommand(command)
        widgets = [widget for widget in self.winfo_children() if widget.winfo_toplevel() == self]
        # CTkTextbox timers survive destroy(); cancel them before command names can be reused.
        pending = widgets.copy()
        owners = {}
        while pending:
            widget = pending.pop()
            owners.update((command, widget) for command in widget._tclCommands or ())
            pending.extend(widget.winfo_children())
        for timer in self.tk.call("after", "info"):
            script, _ = self.tk.call("after", "info", timer)
            if script in owners:
                owners[script].after_cancel(timer)
        for widget in widgets:
            widget.destroy()
        set_language(language)
        self.setup_error = tr(reverse.get(self.setup_error, self.setup_error))
        self._visible_page = None
        self._accounts_signature = None
        self.cache_profile = None
        self._cache_items_signature = None
        self._build_ui()
        for name, value in entries.items():
            widget = getattr(self, name)
            widget.delete(0, "end")
            widget.insert(0, value)
        for name, value in choices.items():
            getattr(self, name).set(value)
        for name, value in checks.items():
            widget = getattr(self, name)
            widget.select() if value else widget.deselect()
        for name, value in texts.items():
            widget = getattr(self, name)
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
        self.log_box.configure(state="disabled")
        for row in rows:
            detail = row[1]
            if row[0] == "Verificação concluída" and detail.split()[0].isdigit():
                detail = tr("{count} nodes inspecionados", count=detail.split()[0])
            self.workflow_tree.insert("", "end", values=(tr(row[0]), detail, tr(row[2])))
        self._render_downloads()
        self._render_library()
        for name, items in selected.items():
            tree = getattr(self, name)
            tree.selection_set([item for item in items if tree.exists(item)])
        self.notice(tr("Idioma atualizado."))
        self.destination_label.configure(text=tr("Destino: Meu Drive / ComfyColab / models / ") + self.category.get())
        for name, profile in self.account_choices.items():
            if profile == destination:
                self.drive_destination.set(name)
        self.show_page(page)
        self.update_idletasks()
        for name, position in views.items():
            self.pages[name]._parent_canvas.yview_moveto(position)

    def _drive_action(self, action):
        if self.busy:
            self.notice(tr("Aguarde a operação atual."))
            return
        if self.offline:
            self.notice(tr("Prévia local: conexão com Drive desativada."))
            return
        source = self.store.current()
        if not source.connected or not source.email:
            self.notice(
                tr("Conecte a conta de origem ao Colab antes de autorizar o Drive."), True
            )
            return
        destid = self.account_choices.get(self.drive_destination.get())
        dest = next((p for p in self.store.profiles if p.id == destid), None)
        if action != "authorize" and not dest:
            self.notice(
                tr("Adicione uma segunda conta antes de transferir arquivos."), True
            )
            return
        if action != "authorize" and (not dest.connected or not dest.email):
            self.notice(
                tr("Conecte a conta de destino ao Colab antes de transferir arquivos."),
                True,
            )
            return
        folder = self.drive_folder.get().strip() or "ComfyColab"
        if folder != "ComfyColab" and not __import__("re").fullmatch(
            r"[A-Za-z0-9_-]+", folder
        ):
            self.notice(tr("Informe ComfyColab ou apenas o ID da pasta."), True)
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
                tr("Origem: {p0}\nDestino: {p1}\n", p0=source.email or source.label, p1=dest.email or dest.label)
                + (
                    tr("Copiar arquivos e conceder leitura da origem ao destino.")
                    if action == "copy"
                    else tr("Compartilhar models com permissão de edição nas duas contas.")
                )
            )
            ConfirmDialog(
                self,
                tr("Confirmar ") + (tr("cópia") if action == "copy" else tr("compartilhamento")),
                detail,
                [
                    (tr("Cancelar"), lambda: None, "secondary"),
                    (
                        tr("Copiar / retomar") if action == "copy" else tr("Compartilhar"),
                        launch,
                        "primary",
                    ),
                ],
            )
        else:
            launch()

    def _check_workflow(self):
        if not self.snapshot.local_ready:
            self.notice(tr("Conecte o ComfyUI antes de verificar dependências."))
            return
        path = filedialog.askopenfilename(
            title=tr("Verificar workflow"), filetypes=[(tr("Workflow ComfyUI"), "*.json")]
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
            self.notice(tr("Verificação concluída: ") + Path(path).name)

        self._async(work, done)

    def _render_history(self):
        self.history_tree.delete(*self.history_tree.get_children())
        names = {
            "start": tr("Iniciar"),
            "stop": tr("Encerrar"),
            "connect": tr("Conectar"),
            "restart": tr("Reiniciar"),
            "drive": "Google Drive",
            "image": tr("Atualizar imagem"),
            "session": tr("Sessão observada"),
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
                    tr(h["result"]),
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
            self.notice(tr("Histórico exportado."))

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
            self.notice(tr("Diagnóstico exportado sem tokens ou chaves."))


def main():
    app = Dashboard(offline="--preview" in sys.argv)
    app.mainloop()


if __name__ == "__main__":
    main()
