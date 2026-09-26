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


if __name__ == "__main__":
    unittest.main()
