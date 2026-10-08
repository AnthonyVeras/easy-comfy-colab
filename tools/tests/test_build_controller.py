import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_controller import command, resources, ROOT


class BuildTests(unittest.TestCase):
    def test_bundle_has_no_credentials_or_legacy_sources(self):
        args = command("python.exe", Path("build/sidecar-resources"))
        self.assertEqual(args[0:3], ["python.exe", "-m", "PyInstaller"])
        data = args[args.index("--add-data") + 1]
        self.assertEqual(data, "build\\sidecar-resources;.")
        self.assertNotIn(".private", " ".join(args))
        self.assertNotIn("Comfy Colab Results", " ".join(args))
        self.assertIn("controller.import_legacy", args)
        self.assertIn("colab_cli.runtime", args)

    def test_sidecar_contains_exact_browser_bridge_and_no_tests_or_private_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = resources(Path(directory))
            self.assertEqual((root/"remote/web/remote_download.js").read_bytes(), (ROOT/"remote/web/remote_download.js").read_bytes())
            self.assertTrue((root/"remote/model_bridge.py").is_file())
            self.assertFalse((root/"remote/tests").exists())
            self.assertFalse((root/".private").exists())


if __name__ == "__main__":
    unittest.main()
