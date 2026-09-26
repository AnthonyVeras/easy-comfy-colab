"""Componentes e telas do Easy Comfy Colab."""

from i18n import LANGUAGES, tr

from tkinter import ttk
import os
import webbrowser
import customtkinter as ctk
from backend import COMFY_URL, GPU_CHOICES
from model_download import CATEGORIES
from services import VERSION, release_vram
from session import COLORS, font

GPU_NAMES = {
    "G4": "G4 · RTX PRO 6000",
    "A100": "A100",
    "L4": "L4",
    "T4": "T4",
    "CPU": "CPU · Downloads",
}


class Shell:
    def label(self, parent, text, heading=False, muted=False):
        w = ctk.CTkLabel(
            parent,
            text=tr(text),
            font=font(20 if heading else 13, "bold" if heading else "normal"),
            text_color=COLORS["muted" if muted else "text"],
            anchor="w",
            justify="left",
            wraplength=790,
        )
        w.pack(fill="x", pady=(0, 8))
        return w

    def button(self, parent, text, command, primary=False, width=140):
        w = ctk.CTkButton(
            parent,
            text=tr(text),
            command=command,
            height=38,
            width=width,
            corner_radius=6,
            font=font(13, "bold" if primary else "normal"),
            border_width=1,
            border_color=COLORS["border"],
            fg_color=COLORS["accent" if primary else "surface_high"],
            hover_color=COLORS["accent_hover" if primary else "border"],
            text_color=COLORS["accent_text" if primary else "text"],
        )
        w._canvas.configure(takefocus=1)
        w._canvas.bind("<Return>", lambda _: w.invoke())
        w._canvas.bind("<space>", lambda _: w.invoke())
        w.bind("<Button-1>", lambda _: w._canvas.focus_set(), add="+")
        w._canvas.bind(
            "<FocusIn>",
            lambda _: w.configure(border_color=COLORS["accent"], border_width=2),
        )
        w._canvas.bind(
            "<FocusOut>",
            lambda _: w.configure(border_color=COLORS["border"], border_width=1),
        )
        return w

    def choice(self, parent, **kwargs):
        kwargs.setdefault("fg_color", COLORS["surface_high"])
        kwargs.setdefault("button_color", COLORS["border"])
        kwargs.setdefault("button_hover_color", COLORS["surface_high"])
        kwargs.setdefault("text_color", COLORS["text"])
        w = ctk.CTkOptionMenu(parent, **kwargs)
        w._canvas.configure(takefocus=1)

        def select(delta):
            values = w.cget("values")
            index = values.index(w.get()) if w.get() in values else 0
            value = values[(index + delta) % len(values)]
            w.set(value)
            command = w.cget("command")
            if command:
                command(value)
            return "break"

        w._canvas.bind("<Down>", lambda _: select(1))
        w._canvas.bind("<Up>", lambda _: select(-1))
        w._canvas.bind(
            "<FocusIn>", lambda _: w.configure(button_color=COLORS["accent"])
        )
        w._canvas.bind(
            "<FocusOut>", lambda _: w.configure(button_color=COLORS["border"])
        )
        return w

    def entry(self, parent, label, placeholder="", show=None):
        self.label(parent, label, muted=True)
        w = ctk.CTkEntry(
            parent,
            height=38,
            placeholder_text=placeholder,
            font=font(13),
            fg_color=COLORS["page"],
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            **({"show": show} if show else {}),
        )
        w.pack(fill="x", pady=(0, 14))
        return w

    def section(self, parent, title, subtitle=""):
        f = ctk.CTkFrame(parent, fg_color=COLORS["surface"], corner_radius=8)
        f.pack(fill="x", pady=(0, 16))
        body = ctk.CTkFrame(f, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=18)
        self.label(body, title, heading=True)
        if subtitle:
            self.label(body, subtitle, muted=True)
        return body

    def row(self, parent):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=(4, 10))
        return f

    def tree(self, parent, columns, widths, height=8):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="both", expand=True, pady=(6, 10))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame, columns=columns, show="headings", height=height, selectmode="browse"
        )
        for name, width in zip(columns, widths):
            tree.heading(name, text=tr(name))
            tree.column(name, width=width, minwidth=70, stretch=(name == columns[0]))
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        tree.bind("<<TreeviewSelect>>", lambda _: self._selection_detail(tree))
        return tree

    def _selection_detail(self, tree):
        sel = tree.selection()
        if sel:
            self.notice_label.configure(
                text=" · ".join(str(v) for v in tree.item(sel[0], "values"))
            )

    def _build_ui(self):
        # CTkScrollableFrame registers global callbacks on the root, not on itself.
        commands_before = set(self._tclCommands or ())
        self._scroll_bindings = {sequence: self.bind_all(sequence) for sequence in (
            "<MouseWheel>", "<KeyPress-Shift_L>", "<KeyPress-Shift_R>",
            "<KeyRelease-Shift_L>", "<KeyRelease-Shift_R>")}
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            rowheight=34,
            font=("Segoe UI", 10),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=COLORS["surface_high"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padding=8,
        )
        style.map(
            "Treeview",
            background=[("selected", "#34506A")],
            foreground=[("selected", "#FFFFFF")],
        )
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        sidebar = ctk.CTkFrame(
            self, width=204, fg_color=COLORS["sidebar"], corner_radius=0
        )
        sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew")
        sidebar.grid_propagate(False)
        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=20, pady=(26, 24))
        self.label(brand, "Easy Comfy\nColab", heading=True)
        self.label(brand, tr("ESTÚDIO REMOTO / 2.0"), muted=True)
        self.nav = {}
        for name in [
            "Sessão",
            "Modelos",
            "Contas e Drive",
            "Monitor",
            "Workflows",
            "Histórico",
            "Configurações",
        ]:
            b = self.button(sidebar, name, lambda n=name: self.show_page(n), width=170)
            b.pack(fill="x", padx=14, pady=4)
            b.configure(anchor="w")
            self.nav[name] = b
        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=20, pady=20)
        self.label(footer, tr("Inferência no Colab\nArquivos no Google Drive"), muted=True)
        self.label(footer, tr("F5  Atualizar conta\nCtrl+1 / 2 / 3  Navegar"), muted=True)
        top = ctk.CTkFrame(self, fg_color=COLORS["page"], corner_radius=0)
        top.grid(row=0, column=1, sticky="ew", padx=26, pady=(18, 12))
        top.grid_columnconfigure(0, weight=1)
        self.page_title = ctk.CTkLabel(
            top, text="Sessão", font=font(26, "bold"), anchor="w"
        )
        self.page_title.grid(row=0, column=0, sticky="w")
        self.account_selector = self.choice(
            top,
            values=[tr("Conta principal")],
            command=self._account_changed,
            width=290,
            height=36,
            fg_color=COLORS["surface_high"],
            button_color=COLORS["border"],
            text_color=COLORS["text"],
            font=font(12),
        )
        self.account_selector.grid(row=0, column=1, padx=(12, 8))
        self.refresh_button = self.button(
            top, tr("Atualizar"), self._refresh_async, width=95
        )
        self.refresh_button.grid(row=0, column=2)
        self.host = ctk.CTkFrame(self, fg_color="transparent")
        self.host.grid(row=1, column=1, sticky="nsew", padx=26)
        self.host.grid_columnconfigure(0, weight=1)
        self.host.grid_rowconfigure(0, weight=1)
        self.pages = {
            name: ctk.CTkScrollableFrame(
                self.host, fg_color=COLORS["page"], corner_radius=0
            )
            for name in self.nav
        }
        for name, builder in [
            ("Sessão", self._build_session),
            ("Modelos", self._build_models),
            ("Contas e Drive", self._build_accounts),
            ("Monitor", self._build_monitor),
            ("Workflows", self._build_workflows),
            ("Histórico", self._build_history),
            ("Configurações", self._build_settings),
        ]:
            builder(self.pages[name])
        self.notice_label = ctk.CTkLabel(
            self,
            text=tr("Pronto para conectar."),
            font=font(12),
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=950,
        )
        self.notice_label.grid(row=2, column=1, sticky="ew", padx=26, pady=12)
        self.show_page("Sessão")
        bindings = "\n".join(self.bind_all(sequence) for sequence in self._scroll_bindings)
        # Own only global scroll callbacks, never CustomTkinter's transient timers.
        self._ui_root_commands = {
            command for command in set(self._tclCommands or ()) - commands_before
            if command in bindings
        }

    def _build_session(self, parent):
        section = self.section(parent, tr("Ambiente de trabalho"))
        self.status_label = self.label(section, tr("Verificando instalação…"))
        self.stage_label = self.label(section, "", muted=True)
        self.elapsed_label = self.label(section, "", muted=True)
        self.label(section, tr("Hardware para a próxima sessão"), muted=True)
        row = self.row(section)
        self.gpu_buttons = {}
        for i, gpu in enumerate(GPU_CHOICES):
            row.grid_columnconfigure(i, weight=1)
            b = self.button(
                row, GPU_NAMES[gpu], lambda g=gpu: self._choose_gpu(g), width=110
            )
            b.grid(row=0, column=i, sticky="ew", padx=(0, 8))
            self.gpu_buttons[gpu] = b
        self.label(
            section,
            tr("G4: até 96 GB de VRAM · GPU e consumo dependem do Colab.\nCPU: prepara downloads sem iniciar o ComfyUI."),
            muted=True,
        )
        self.label(section, tr("Onde salvar os resultados"), muted=True)
        row = self.row(section)
        self.output_choice = self.choice(
            row, values=["Google Drive", tr("Meu PC")], width=180,
            command=self._choose_output,
        )
        self.output_choice.pack(side="left", padx=(0, 8))
        self.button(row, tr("Abrir outputs no PC"), lambda: self._open_folder("output"), width=180).pack(side="left")
        self.output_hint = self.label(section, "", muted=True)
        self.output_sync_label = self.label(section, "", muted=True)
        row = self.row(section)
        self.start_button = self.button(row, tr("Iniciar sessão"), self._start, True, 170)
        self.start_button.pack(side="left", padx=(0, 8))
        self.open_button = self.button(
            row, tr("Abrir ComfyUI"), lambda: webbrowser.open(COMFY_URL)
        )
        self.open_button.pack(side="left", padx=(0, 8))
        self.stop_button = self.button(row, tr("Encerrar VM"), self._stop, width=130)
        self.stop_button.pack(side="right")
        row = self.row(section)
        self.restart_button = self.button(
            row, tr("Reiniciar ComfyUI"), self._restart_comfy, width=165
        )
        self.restart_button.pack(side="left", padx=(0, 8))
        self.reconnect_button = self.button(row, tr("Reconectar"), self._start, width=115)
        self.reconnect_button.pack(side="left", padx=(0, 8))
        self.free_button = self.button(
            row,
            tr("Liberar VRAM"),
            lambda: self._async(
                release_vram, lambda _: self.notice(tr("Modelos descarregados da memória."))
            ),
        )
        self.free_button.pack(side="left")
        self.auth_panel = ctk.CTkFrame(
            section, fg_color=COLORS["surface_high"], corner_radius=6
        )
        self.auth_title = ctk.CTkLabel(self.auth_panel, text="", font=font(16, "bold"))
        self.auth_title.pack(anchor="w", padx=16, pady=(12, 6))
        self.auth_detail = ctk.CTkLabel(
            self.auth_panel, text="", font=font(12), wraplength=720, justify="left"
        )
        self.auth_detail.pack(anchor="w", padx=16)
        authrow = ctk.CTkFrame(self.auth_panel, fg_color="transparent")
        authrow.pack(fill="x", padx=16, pady=12)
        self.auth_open_button = self.button(
            authrow, tr("Abrir autorização"), self._open_auth
        )
        self.auth_open_button.pack(side="left", padx=(0, 8))
        self.auth_code = ctk.CTkEntry(
            authrow, placeholder_text=tr("Código exibido pelo Google"), width=240, height=38
        )
        self.auth_code.pack(side="left", padx=(0, 8))
        self.auth_code.bind("<Return>", lambda _: self._submit_auth())
        self.auth_submit_button = self.button(
            authrow, tr("Concluir conexão"), self._submit_auth
        )
        self.auth_submit_button.pack(side="left")
        image_section = self.section(parent, tr("Imagem de instalação no Drive"),
            tr("Atualizada automaticamente antes de encerrar a VM. Salva os nodes e dependências atuais; "
            "os modelos já ficam nas pastas do Drive. A operação pode levar alguns minutos e mantém a GPU ligada."))
        self.image_button = self.button(image_section, tr("Atualizar imagem do Drive"), self._update_runtime_image, width=220)
        self.image_button.pack(anchor="w")
        metrics = self.section(parent, tr("Créditos e sessão"))
        row = self.row(metrics)
        self.metric_values = {}
        for key, label in [
            ("balance", tr("Saldo")),
            ("rate", tr("Consumo da conta")),
            ("runtime", tr("Autonomia estimada")),
        ]:
            f = ctk.CTkFrame(row, fg_color="transparent")
            f.pack(side="left", fill="x", expand=True)
            self.label(f, label, muted=True)
            self.metric_values[key] = self.label(f, "—", heading=True)
        self.credit_help = self.label(
            metrics,
            tr("Atualizado a cada 30 segundos. Autonomia = saldo / consumo da conta."),
            muted=True,
        )
        activity = self.section(parent, tr("Atividade"))
        self.log_box = ctk.CTkTextbox(
            activity,
            height=180,
            fg_color=COLORS["page"],
            font=font(12, family="Consolas"),
            wrap="word",
        )
        self.log_box.pack(fill="x")
        self.log_box.configure(state="disabled")

    def _build_models(self, parent):
        add = self.section(
            parent,
            tr("Baixar modelos"),
            tr("Hugging Face e Civitai → VM → Google Drive. A transferência continua na VM ao fechar o app."),
        )
        self.label(add, tr("Links — um por linha"), muted=True)
        self.urls = ctk.CTkTextbox(
            add,
            height=78,
            fg_color=COLORS["page"],
            font=font(12),
            border_width=1,
            border_color=COLORS["border"],
        )
        self.urls.pack(fill="x", pady=(0, 12))

        def next_field(event):
            event.widget.tk_focusNext().focus_set()
            return "break"

        self.urls.bind("<Tab>", next_field)
        self.label(
            add,
            tr("Para nomear um arquivo: URL | nome.safetensors. No Civitai, informe o nome com extensão."),
            muted=True,
        )
        row = self.row(add)
        self.category = self.choice(row, values=list(CATEGORIES), width=210, height=38)
        self.category.set("loras")
        self.category.pack(side="left", padx=(0, 10))
        self.download_button = self.button(
            row, tr("Adicionar à fila"), self._submit_downloads, True, 170
        )
        self.download_button.pack(side="left")
        self.parallel = self.choice(
            row,
            values=["1", "2", "3", "4", "5", "6"],
            width=64,
            height=38,
            command=self._change_parallel,
        )
        self.parallel.set(str(self.preferences.values["parallel"]))
        self.parallel.pack(side="right")
        ctk.CTkLabel(
            row, text=tr("Paralelos"), font=font(12), text_color=COLORS["muted"]
        ).pack(side="right", padx=10)
        self.destination_label = self.label(
            add, tr("Destino: Meu Drive / ComfyColab / models / loras"), muted=True
        )
        self.category.configure(
            command=lambda v: self.destination_label.configure(
                text=tr("Destino: Meu Drive / ComfyColab / models / ") + v
            )
        )
        downloads = self.section(
            parent,
            tr("Fila de downloads"),
            tr("Selecione um item para ver o nome completo, cancelar ou retomar."),
        )
        self.download_tree = self.tree(
            downloads,
            [tr("Arquivo"), tr("Estado"), tr("Progresso"), tr("Velocidade"), tr("Restante")],
            [315, 110, 160, 110, 90],
        )
        row = self.row(downloads)
        self.button(row, tr("Cancelar selecionado"), self._cancel_download, width=175).pack(
            side="left", padx=(0, 8)
        )
        self.button(
            row, tr("Retomar / tentar novamente"), self._retry_download, width=210
        ).pack(side="left")
        self.download_hint = self.label(
            downloads,
            tr("Inicie uma sessão de GPU ou CPU para acessar a fila."),
            muted=True,
        )
        library = self.section(parent, tr("Biblioteca no Drive"))
        row = self.row(library)
        self.search = ctk.CTkEntry(
            row, placeholder_text=tr("Pesquisar modelo ou categoria"), height=38, width=400
        )
        self.search.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.search.bind("<KeyRelease>", lambda _: self._render_library())
        self.button(row, tr("Atualizar biblioteca"), self._load_library, width=170).pack(
            side="right"
        )
        self.library_tree = self.tree(
            library, [tr("Modelo"), tr("Categoria"), tr("Tamanho")], [510, 180, 110], height=9
        )
        self.library_tree.configure(selectmode="extended")
        self.button(library, tr("Selecionar por workflow"), self._select_cache_workflow, width=210).pack(anchor="w")
        self.library_hint = self.label(
            library, tr("Atualize com a sessão conectada."), muted=True
        )
        cache = self.section(parent, tr("Carregamento rápido"),
                             tr("Selecione modelos na biblioteca com Ctrl ou pelo JSON do workflow. As cópias ficam na VM; os originais permanecem no Drive."))
        self.cache_enabled = ctk.CTkSwitch(cache, text=tr("Usar cache da VM"), command=self._cache_settings)
        self.cache_enabled.pack(anchor="w", pady=(0, 12))
        self.cache_warm = ctk.CTkSwitch(cache, text=tr("Preparar antecipadamente na RAM"), command=self._cache_settings)
        self.cache_warm.pack(anchor="w", pady=(0, 12))
        row = self.row(cache)
        self.cache_limit = self.choice(row, values=[f"{n} GiB" for n in (8, 16, 32, 64, 96, 128)],
                                       width=140, command=self._cache_settings)
        self.cache_limit.set("32 GiB")
        self.cache_limit.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(row, text=tr("Limite de pré-leitura · reserva automática de RAM"), font=font(12), text_color=COLORS["muted"]).pack(side="left")
        row = self.row(cache)
        self.cache_prepare = self.button(row, tr("Preparar selecionados"), self._prepare_cache, True, width=200)
        self.cache_prepare.pack(side="left", padx=(0, 10))
        self.cache_cancel = self.button(row, tr("Cancelar preparação"), lambda: self._cache_action("cancel"), width=190)
        self.cache_cancel.pack(side="left")
        self.cache_clear = self.button(row, tr("Limpar cache da VM"), lambda: self._cache_action("clear"), width=185)
        self.cache_clear.pack(side="left", padx=(10, 0))
        self.cache_tree = self.tree(cache, [tr("Modelo"), tr("Preparação"), tr("Detalhes")], [320, 190, 310], height=5)
        self.cache_hint = self.label(cache, tr("Conecte a VM para preparar modelos."), muted=True)
        self.label(cache, tr("A lista escolhida será preparada automaticamente nas próximas sessões. A RAM usa o cache do Linux e pode ser liberada pelo sistema. A cópia inicial ainda leva tempo; downloads continuam no Drive."), muted=True)
        self._render_cache()

    def _build_accounts(self, parent):
        accounts = self.section(
            parent,
            tr("Contas conectadas"),
            tr("Cada perfil guarda sua autorização e preferência de GPU."),
        )
        self.account_tree = self.tree(
            accounts, [tr("Conta"), tr("E-mail"), tr("Perfil")], [220, 400, 170], height=4
        )
        row = self.row(accounts)
        self.add_account_button = self.button(
            row, tr("Adicionar conta"), self._add_account, True
        )
        self.add_account_button.pack(side="left", padx=(0, 8))
        self.button(
            row, tr("Conectar conta selecionada"), self._connect_account, width=210
        ).pack(side="left")
        transfer = self.section(
            parent,
            tr("Copiar entre Google Drives"),
            tr("As cópias são feitas nos servidores do Google. Os arquivos da origem são preservados."),
        )
        self.label(
            transfer, tr("Origem: conta selecionada no topo do aplicativo."), muted=True
        )
        self.label(transfer, tr("Conta de destino"), muted=True)
        self.drive_destination = self.choice(
            transfer, values=[tr("Adicione outra conta")], width=450, height=38
        )
        self.drive_destination.pack(anchor="w", pady=(0, 14))
        self.drive_folder = self.entry(
            transfer, tr("Pasta de origem"), tr("ComfyColab ou ID de uma pasta do Drive")
        )
        self.drive_folder.insert(0, "ComfyColab")
        self.label(
            transfer,
            tr("A cópia cria ou atualiza ComfyColab no destino. O cache de instalação não é copiado. Arquivos diferentes com o mesmo nome são preservados com outro nome."),
            muted=True,
        )
        row = self.row(transfer)
        self.button(
            row, tr("Verificar cópia"), lambda: self._drive_action("plan"), True
        ).pack(side="left", padx=(0, 8))
        self.button(
            row, tr("Copiar / retomar"), lambda: self._drive_action("copy"), width=160
        ).pack(side="left", padx=(0, 8))
        self.button(
            row, tr("Autorizar Drive"), lambda: self._drive_action("authorize"), width=140
        ).pack(side="left")
        shared = self.section(
            parent,
            tr("Biblioteca compartilhada"),
            tr("Alternativa à cópia: duas contas passam a usar a mesma pasta de modelos."),
        )
        self.label(
            shared,
            tr("Cria um atalho models no destino e concede edição à outra conta. Alterações afetam ambas as contas; a biblioteca depende do Drive de origem. O destino precisa estar sem uma pasta models existente."),
            muted=True,
        )
        self.button(
            shared,
            tr("Compartilhar biblioteca"),
            lambda: self._drive_action("share"),
            width=210,
        ).pack(anchor="w")
        self.drive_message = self.label(
            parent,
            tr("Autorizações e progresso aparecem na aba Sessão. O acesso ao Drive será solicitado separadamente."),
            muted=True,
        )

    def _build_monitor(self, parent):
        self.monitor_labels = {}
        self.monitor_bars = {}
        for prefix, title in [("local", tr("Seu notebook")), ("remote", tr("VM do Colab"))]:
            section = self.section(
                parent, title, tr("Leituras atualizadas a cada 5 segundos.")
            )
            for key, label in [
                ("cpu", "CPU"),
                ("ram", tr("Memória RAM")),
                ("extra", tr("Detalhes")),
            ]:
                self.monitor_labels[prefix + "_" + key] = self.label(
                    section, label + ": —"
                )
                if key != "extra":
                    bar = ctk.CTkProgressBar(
                        section, height=8, progress_color=COLORS["accent"]
                    )
                    bar.set(0)
                    bar.pack(fill="x", pady=(0, 14))
                    self.monitor_bars[prefix + "_" + key] = bar
        self.gpu_section = self.section(parent, tr("GPU e armazenamento da VM"))
        self.gpu_label = self.label(
            self.gpu_section,
            tr("Inicie uma sessão para consultar GPU, VRAM e disco."),
            muted=True,
        )
        self.gpu_bar = ctk.CTkProgressBar(
            self.gpu_section, height=8, progress_color=COLORS["accent"]
        )
        self.gpu_bar.set(0)
        self.gpu_bar.pack(fill="x")
        self.monitor_status = self.label(parent, "", muted=True)

    def _build_workflows(self, parent):
        section = self.section(
            parent,
            tr("Verificar dependências"),
            tr("Escolha um workflow JSON para identificar modelos e nodes ausentes na VM conectada."),
        )
        self.button(
            section, tr("Abrir workflow JSON"), self._check_workflow, True, width=190
        ).pack(anchor="w")
        self.workflow_tree = self.tree(
            section,
            [tr("Resultado"), tr("Dependência"), tr("Próximo passo")],
            [190, 310, 350],
            height=12,
        )
        self.label(
            section,
            tr("O diagnóstico não executa a geração. Referências dinâmicas e alguns subgrafos podem exigir conferência no ComfyUI."),
            muted=True,
        )
        self.button(
            section,
            tr("Abrir pasta de workflows"),
            lambda: self._open_folder("user/default/workflows"),
            width=210,
        ).pack(anchor="w")

    def _build_history(self, parent):
        section = self.section(
            parent,
            tr("Histórico de operações"),
            tr("Registro local por conta. Taxa e saldo são os valores consultados na conta, quando disponíveis."),
        )
        self.history_tree = self.tree(
            section,
            [
                tr("Data"),
                tr("Conta"),
                tr("Operação"),
                "GPU",
                tr("Duração"),
                tr("CU estimados"),
                tr("Resultado"),
            ],
            [130, 200, 140, 65, 90, 100, 100],
            height=14,
        )
        self.button(
            section, tr("Exportar histórico"), self._export_history, width=165
        ).pack(anchor="w")
        self._render_history()

    def _build_settings(self, parent):
        language = self.section(
            parent, tr("Idioma / Language"),
            tr("A escolha é salva neste computador e aplicada imediatamente. Não reinicia o ComfyUI nem a VM."),
        )
        self.language_choice = self.choice(
            language, values=list(LANGUAGES.values()), width=220,
            command=self._change_language,
        )
        self.language_choice.set(LANGUAGES[self.preferences.values["language"]])
        self.language_choice.pack(anchor="w")
        self.section(
            parent,
            tr("Inicialização · imagem no Google Drive"),
            tr("Ativa por padrão. Uma instalação pronta é copiada do Drive e extraída no disco da VM. "
            "Se a base do Colab mudar, o app instala o ambiente novamente e prepara outra imagem em segundo plano. "
            "Use Atualizar imagem do Drive na aba Sessão após instalar nodes. Encerrar VM também atualiza a imagem antes de desligar. "
            "Os tempos de cada etapa aparecem no log da sessão."),
        )
        creds = self.section(
            parent,
            tr("Credenciais de download"),
            tr("Protegidas pelo usuário do Windows e separadas por conta. Deixe em branco para manter a credencial atual."),
        )
        self.hf_token = self.entry(creds, tr("Token do Hugging Face"), "hf_…", show="●")
        self.civit_token = self.entry(
            creds, tr("API key do Civitai"), tr("Credencial da sua conta Civitai"), show="●"
        )
        self.credential_status = self.label(creds, "", muted=True)
        row = self.row(creds)
        self.button(
            row, tr("Salvar credenciais"), self._save_credentials, True, width=165
        ).pack(side="left", padx=(0, 8))
        self.button(
            row, tr("Remover credenciais"), self._remove_credentials, width=175
        ).pack(side="left")
        economy = self.section(
            parent,
            tr("Uso e economia"),
            tr("O encerramento automático só funciona enquanto este aplicativo está aberto e conectado à VM."),
        )
        self.idle_entry = self.entry(
            economy,
            tr("Encerrar após quantos minutos sem geração ou download?"),
            tr("0 desativa"),
        )
        self.idle_entry.insert(0, str(self.preferences.values["idle_minutes"]))
        self.balance_entry = self.entry(
            economy, tr("Avisar quando o saldo ficar abaixo de quantos créditos?"), "20"
        )
        self.balance_entry.insert(0, str(self.preferences.values["balance_alert"]))
        self.auto_open = ctk.CTkCheckBox(
            economy,
            text=tr("Abrir o ComfyUI no navegador quando a sessão estiver pronta"),
            font=font(13),
        )
        self.auto_open.pack(anchor="w", pady=(0, 16))
        if self.preferences.values["auto_open"]:
            self.auto_open.select()
        self.button(
            economy, tr("Salvar preferências"), self._save_preferences, width=170
        ).pack(anchor="w")
        support = self.section(
            parent,
            tr("Aplicativo"),
            tr("Versão ") + VERSION + tr(" · ComfyUI Easy Install no Google Colab"),
        )
        row = self.row(support)
        self.button(
            row,
            tr("Abrir pasta do projeto"),
            lambda: os.startfile(self.gateway.root),
            width=185,
        ).pack(side="left", padx=(0, 8))
        self.button(
            row, tr("Exportar diagnóstico"), self._export_diagnostics, width=180
        ).pack(side="left")
        self.label(
            support,
            tr("Os modelos ficam no Drive. O cache e o ambiente temporário da VM podem ser recriados em uma nova sessão."),
            muted=True,
        )

    def show_page(self, name):
        if getattr(self, "_visible_page", None) == name:
            return
        previous = getattr(self, "_visible_page", None)
        self.current_page = name
        self._visible_page = name
        if previous:
            self.pages[previous].grid_remove()
        for key in self.pages:
            self.nav[key].configure(
                fg_color=COLORS["surface_high"] if key == name else "transparent",
                border_width=1 if key == name else 0,
            )
        self.pages[name].grid(row=0, column=0, sticky="nsew")
        self.page_title.configure(text=tr(name))
