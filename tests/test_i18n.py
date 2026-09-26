"""Translation catalog and persisted settings; no VM or network."""

import ast
import json
from pathlib import Path
from string import Formatter
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from i18n import EN, get_language, set_language, tr
from services import Preferences
from session import format_units


class LanguageTests(unittest.TestCase):
    def tearDown(self):
        set_language("pt-BR")

    def test_catalog_placeholders_and_explicit_messages(self):
        def fields(text):
            return sorted((name, spec, conversion) for _, name, spec, conversion in Formatter().parse(text) if name is not None)
        for source, english in EN.items():
            self.assertEqual(fields(source), fields(english), source)
        for path in (Path(__file__).resolve().parents[1] / "app").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "tr" and node.args and isinstance(node.args[0], ast.Constant)):
                    self.assertIn(node.args[0].value, EN, f"{path.name}:{node.lineno}")
        set_language("en")
        self.assertEqual(tr("{p0} download(s) adicionados à fila.", p0=3), "3 download(s) added to the queue.")
        self.assertEqual(tr("user {content}.safetensors"), "user {content}.safetensors")
        self.assertEqual(format_units(1234.5), "1,234.50")
        set_language("pt-BR")
        self.assertEqual(format_units(1234.5), "1.234,50")

    def test_preference_migration_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", LOCALAPPDATA=folder):
            prefs = Preferences()
            self.assertEqual(prefs.values["language"], "pt-BR")
            for invalid in (None, "de", [], 42):
                prefs.path.parent.mkdir(parents=True, exist_ok=True)
                prefs.path.write_text(json.dumps({"parallel": 5, "language": invalid}), encoding="utf-8")
                loaded = Preferences()
                self.assertEqual(loaded.values["language"], "pt-BR")
                self.assertEqual(loaded.values["parallel"], 5)
            loaded.values["language"] = "en"
            loaded.save()
            self.assertEqual(Preferences().values["language"], "en")
            set_language("unknown")
            self.assertEqual(get_language(), "pt-BR")


if __name__ == "__main__":
    unittest.main()
