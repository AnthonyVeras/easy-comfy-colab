"""PC destination, restart persistence and profile preference, without a VM."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from backend import Gateway, ProfileStore

spec = importlib.util.spec_from_file_location('output_storage', ROOT / 'remote/output_storage.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class OutputTests(unittest.TestCase):
    def test_pc_never_creates_drive_output_and_restart_preserves_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            storage.ROOT = Path(folder) / 'vm'
            storage.DRIVE = Path(folder) / 'drive' / 'output'
            first = storage.prepare('pc')
            self.assertTrue(first.is_dir())
            self.assertFalse(storage.DRIVE.exists())
            (first / 'image.png').write_bytes(b'example output')
            self.assertEqual(storage.prepare(), first)
            self.assertEqual(storage.prepare('drive'), storage.DRIVE)
            self.assertEqual(storage.prepare('pc'), first)
            self.assertTrue((first / 'image.png').exists())
            with self.assertRaises(ValueError):
                storage.prepare('../drive')
            profiles = ProfileStore(Path(folder) / 'profiles.json')
            profiles.current().output_mode = 'pc'
            profiles.save()
            profile = ProfileStore(profiles.path).current()
            self.assertEqual(profile.output_mode, 'pc')
            gateway = Gateway()
            gateway._linux_root = '/project'
            args = gateway.args(profile, 'true')
            self.assertIn('COMFY_OUTPUT_MODE=pc', args)
            self.assertEqual(profile.media_data, Path.home() / 'Comfy Colab Results')


if __name__ == '__main__':
    unittest.main()
