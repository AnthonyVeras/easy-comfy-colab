import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_controller import command


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


if __name__ == "__main__":
    unittest.main()
