"""Regressões dos dois defeitos relatados na versão 2.0.0, sem VM."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from main import Dashboard
from i18n import get_language, set_language
from services import Preferences


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = patch.dict("os.environ", LOCALAPPDATA=self.temp.name)
        self.storage.start()
        self.app = Dashboard(offline=True)
        self.app.geometry("980x680")
        self.app.update()
        self.app.busy = "start"

    def tearDown(self):
        for callback in self.app.tk.call("after", "info"):
            self.app.tk.call("after", "cancel", callback)
        self.app.destroy()
        set_language("pt-BR")
        self.storage.stop()
        self.temp.cleanup()

    def test_refresh_preserves_selected_tab_and_scroll_during_auth(self):
        with patch("session.webbrowser.open"):
            self.app._consume_line(
                "https://accounts.google.com/o/oauth2/v2/auth?test=1"
            )
        self.app.show_page("Workflows")
        self.app.update()
        canvas = self.app.pages["Workflows"]._parent_canvas
        canvas.yview_moveto(0.35)
        self.app.update()
        before = canvas.yview()
        self.assertGreater(before[0], 0)
        for _ in range(4):
            self.app._render()
            self.app.update()
        self.assertEqual(self.app.current_page, "Workflows")
        self.assertEqual(canvas.yview(), before)
        self.app.show_page("Workflows")
        self.app.update()
        self.assertEqual(canvas.yview(), before)
        self.app.show_page("Modelos")
        self.app.update()
        self.app.show_page("Workflows")
        self.app.update()
        self.assertEqual(canvas.yview(), before)

    def test_submitting_drive_auth_clears_pending_request(self):
        self.app.auth_mode = "drive"
        self.app.auth_url = "https://accounts.google.com/o/oauth2/v2/auth?test=1"
        self.app.process = SimpleNamespace(stdin=io.BytesIO())
        self.app._submit_auth()
        self.assertEqual(self.app.process.stdin.getvalue(), b"\n")
        self.assertEqual(self.app.auth_mode, "")
        self.assertEqual(self.app.auth_url, "")
        self.app.show_page("Modelos")
        self.app._render()
        self.app.update()
        self.assertEqual(self.app.current_page, "Modelos")
        self.assertFalse(self.app.auth_panel.winfo_ismapped())

    def test_image_button_starts_only_manual_update_and_blocks_close_while_busy(self):
        self.app.busy = None
        self.app.setup_error = ''
        self.app.snapshot.session_exists = True
        self.app.snapshot.local_ready = True
        self.app.snapshot.hardware = 'L4'
        with patch.object(self.app, '_launch') as launch, patch.object(self.app.gateway, 'linux_root', return_value='/project'):
            self.app._render()
            self.assertEqual(self.app.image_button.cget('state'), 'normal')
            self.app.image_button.invoke()
            self.assertEqual(launch.call_args.args[0], 'image')
            self.assertEqual(launch.call_args.args[2], ('bash', '/project/app/runtime_image.sh'))
        self.app.busy = 'image'
        self.app._render()
        self.assertEqual(self.app.stop_button.cget('state'), 'disabled')
        with patch.object(self.app, 'destroy') as destroy:
            self.app._on_close()
            destroy.assert_not_called()
        self.app.busy = None
        self.app.snapshot.hardware = 'CPU'
        self.app._render()
        self.assertEqual(self.app.image_button.cget('state'), 'disabled')

    def test_language_switch_preserves_session_forms_navigation_and_protocol(self):
        app = self.app
        app.show_page("Configurações")
        app.urls.insert("1.0", "https://example.com/Modelo.safetensors")
        app.hf_token.insert(0, "unsaved-example")
        app.idle_entry.delete(0, "end")
        app.idle_entry.insert(0, "27")
        app.auth_mode = "code"
        app.auth_url = "https://example.com/auth"
        process = app.process = SimpleNamespace(stdin=io.BytesIO())
        snapshot = app.snapshot
        app.jobs = [dict(id="example", name="Modelo.safetensors", status="queued", bytes=0, total=10)]
        app._render_downloads()
        app.download_tree.selection_set("example")
        app.update()
        app.pages["Configurações"]._parent_canvas.yview_moveto(0.3)
        before = app.pages["Configurações"]._parent_canvas.yview()[0]
        command_count = len(app._tclCommands)
        with patch.object(app, "_launch") as launch, patch.object(app.gateway, "run") as run, \
                patch.object(app, "report_callback_exception") as callback_error:
            app.language_choice._dropdown_menu.invoke(1)
            app.update()
            callback_error.assert_not_called()
            self.assertEqual(get_language(), "en")
            self.assertEqual(Preferences().values["language"], "en")
            self.assertEqual(app.current_page, "Configurações")
            self.assertEqual(app.page_title.cget("text"), "Settings")
            self.assertEqual(app.nav["Modelos"].cget("text"), "Models")
            self.assertEqual(app.image_button.cget("text"), "Update Drive image")
            self.assertAlmostEqual(app.pages["Configurações"]._parent_canvas.yview()[0], before, delta=0.01)
            self.assertEqual(app.urls.get("1.0", "end-1c"), "https://example.com/Modelo.safetensors")
            self.assertEqual(app.hf_token.get(), "unsaved-example")
            self.assertEqual(app.idle_entry.get(), "27")
            self.assertEqual(app.busy, "start")
            self.assertIs(app.process, process)
            self.assertIs(app.snapshot, snapshot)
            self.assertEqual(app.download_tree.selection(), ("example",))
            self.assertEqual(app.download_tree.item("example", "values")[1], "Queued")
            app._consume_line("Atualizando imagem do Drive: preparando cópia...")
            self.assertEqual(app.stage, "Updating the Drive image…")
            app.busy = None
            app._choose_output("My PC")
            self.assertEqual(app.store.current().output_mode, "pc")
            app.language_choice._dropdown_menu.invoke(0)
            app.update()
            callback_error.assert_not_called()
            self.assertEqual(app.page_title.cget("text"), "Configurações")
            self.assertEqual(app.download_tree.item("example", "values")[1], "Na fila")
            self.assertEqual(app.output_choice.get(), "Meu PC")
            self.assertEqual(app.hf_token.get(), "unsaved-example")
            self.assertEqual(len(app._tclCommands), command_count)
            launch.assert_not_called()
            run.assert_not_called()

    def test_language_save_failure_keeps_current_interface(self):
        with patch.object(self.app.preferences, "save", side_effect=OSError("disk unavailable")):
            self.app._change_language("English")
        self.assertEqual(get_language(), "pt-BR")
        self.assertEqual(self.app.preferences.values["language"], "pt-BR")
        self.assertEqual(self.app.nav["Modelos"].cget("text"), "Modelos")


if __name__ == "__main__":
    unittest.main()
