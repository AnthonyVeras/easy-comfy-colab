"""Fresh installs must not inherit the developer's identity or authentication."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from backend import Gateway, ProfileStore
from runtime_config import app_data_dir, wsl_prefix


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name, "USERPROFILE": self.temp.name, "HOME": self.temp.name}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_fresh_profile_is_disconnected_and_contains_no_identity(self):
        profile = ProfileStore().current()
        self.assertFalse(profile.connected)
        self.assertEqual(profile.email, "")
        self.assertFalse(Path(profile.token_path).is_absolute())
        self.assertEqual(app_data_dir(), Path(self.temp.name) / "EasyComfyColab")

    def test_default_wsl_uses_distribution_default_user(self):
        self.assertEqual(wsl_prefix(), ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec"])

    def test_powershell_bom_configuration_and_environment_override(self):
        app_data_dir().mkdir()
        (app_data_dir() / "runtime.json").write_text(
            json.dumps({"distro": "Ubuntu", "user": "example"}), encoding="utf-8-sig"
        )
        self.assertEqual(wsl_prefix()[2:5], ["Ubuntu", "--user", "example"])
        with patch.dict(os.environ, EASY_COMFY_WSL_DISTRO="Debian", EASY_COMFY_WSL_USER="tester"):
            self.assertEqual(wsl_prefix()[2:5], ["Debian", "--user", "tester"])

    def test_invalid_configuration_does_not_become_an_argument(self):
        app_data_dir().mkdir()
        (app_data_dir() / "runtime.json").write_text('{"distro": ["bad"]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            wsl_prefix()

    def test_wrapper_preserves_spaces_as_one_argument_and_profile_isolation(self):
        gateway = Gateway(Path(self.temp.name) / "Project with spaces")
        gateway._linux_root = "/mnt/c/Project with spaces"
        store = ProfileStore()
        profile = store.add("Another account")
        args = gateway.args(profile, "@colab", "runtime", "list")
        self.assertIn(f"COMFY_PROFILE_ID={profile.id}", args)
        self.assertIn("/mnt/c/Project with spaces/app/wsl_exec.sh", args)
        self.assertEqual(args[-3:], ["@colab", "runtime", "list"])
        self.assertFalse(profile.connected)
        self.assertNotEqual(profile.token_path, store.profiles[0].token_path)


if __name__ == "__main__":
    unittest.main()
