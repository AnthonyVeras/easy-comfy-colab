"""A imagem deve recusar corrupção, incompatibilidade e sobrescrita da VM."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runtime_image', Path(__file__).resolve().parents[1] / 'remote/runtime_image.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeImageTests(unittest.TestCase):
    def test_204_image_is_selected_without_rebuilding_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'STORE', Path(folder)):
            legacy = Path(folder) / (module.key_for({'test': 1}, module.LEGACY_RECIPE) + '.json')
            legacy.write_text('{}')
            self.assertEqual(module.image_pointer({'test': 1}, 'new'), (legacy, module.LEGACY_RECIPE))
            current = Path(folder) / (module.key_for({'test': 1}, 'new') + '.json')
            current.write_text('{}')
            self.assertEqual(module.image_pointer({'test': 1}, 'new'), (current, 'new'))

    def test_local_and_git_nodes_preserve_new_code_without_personal_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for git in (False, True):
                source, target = root / f'node-{git}', root / f'copy-{git}'
                source.mkdir()
                (source / 'models').mkdir()
                (source / 'models/code.py').write_text('model code')
                (source / '__init__.py').write_text('original')
                if git:
                    subprocess.run(['git', 'init', '-q', str(source)], check=True)
                    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
                    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial'], check=True)
                    subprocess.run(['git', '-C', str(source), 'remote', 'add', 'origin', 'https://github.com/example/node.git'], check=True)
                (source / '__init__.py').write_text('edited')
                (source / 'new.py').write_text('new local code')
                (source / '.env').write_text('secret test')
                (source / 'credentials.json').write_text('secret test')
                (source / 'output').mkdir()
                (source / 'output/result.png').write_bytes(b'personal output')
                module.copy_node(source, target)
                self.assertEqual((target / '__init__.py').read_text(), 'edited')
                self.assertEqual((target / 'new.py').read_text(), 'new local code')
                self.assertTrue((target / 'models/code.py').exists())
                self.assertFalse((target / '.env').exists())
                self.assertFalse((target / 'credentials.json').exists())
                self.assertFalse((target / 'output').exists())

    def test_publish_failure_keeps_previous_image_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage, store = root / 'stage', root / 'drive'
            store.mkdir()
            for component in module.COMPONENTS:
                (stage / component).mkdir(parents=True)
                (stage / component / 'new.txt').write_text('installation')
            base, recipe = {'test': 1}, 'new'
            pointer = store / (module.key_for(base, recipe) + '.json')
            pointer.write_text('previous pointer')
            original_digest = module.digest
            def corrupt(path):
                return 'incorrect' if Path(path).suffix == '.partial' else original_digest(path)
            with patch.multiple(module, STORE=store, ROOT=root), patch.object(module, 'validate'), \
                 patch.object(module, 'fingerprint', return_value=base), patch.object(module, 'recipe', return_value=recipe), \
                 patch.object(module, 'digest', side_effect=corrupt):
                with self.assertRaisesRegex(ValueError, 'verificação'):
                    module.publish(stage, {}, [])
                self.assertEqual(pointer.read_text(), 'previous pointer')
            with patch.multiple(module, STORE=store, ROOT=root), patch.object(module, 'validate'), \
                 patch.object(module, 'fingerprint', return_value=base), patch.object(module, 'recipe', return_value=recipe), \
                 patch.object(module, 'source_signature', return_value='changed'):
                with self.assertRaisesRegex(ValueError, 'instalação mudou'):
                    module.publish(stage, {}, [], expected_source='original')
                self.assertEqual(pointer.read_text(), 'previous pointer')

    def test_archive_boundary(self):
        for name, kind in [('ComfyUI-Easy-Install/../../outside', tarfile.REGTYPE),
                           ('/absolute', tarfile.REGTYPE), ('mcp-venv/link', tarfile.SYMTYPE),
                           ('mcp-venv/device', tarfile.CHRTYPE)]:
            member = tarfile.TarInfo(name)
            member.type = kind
            with self.assertRaises(ValueError):
                list(module.safe_members([member]))
        member = tarfile.TarInfo('mcp-venv/file')
        member.size = 100
        with self.assertRaises(ValueError):
            list(module.safe_members([member], limit=99))

    def test_restore_integrity_compatibility_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'vm'
            root.mkdir()
            drive = Path(folder) / 'drive'
            store = drive / '.runtime-images'
            store.mkdir(parents=True)
            with patch.multiple(module, ROOT=root, DRIVE=drive, STORE=store), \
                 patch.object(module, 'fingerprint', return_value={'test': 1}), \
                 patch.object(module, 'recipe', return_value='test'), \
                 patch.object(module, 'recreate_links'), patch.object(module, 'validate') as validate:
                self.assertFalse(module.restore())
                archive = store / 'test.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    for name in ['ComfyUI-Easy-Install/ComfyUI/main.py', 'mcp-venv/test']:
                        member = tarfile.TarInfo(name)
                        member.size = 3
                        tar.addfile(member, io.BytesIO(b'abc'))
                checksum = module.digest(archive)
                archive = archive.rename(store / (checksum + '.tar.gz'))
                pointer = store / (module.key_for({'test': 1}, 'test') + '.json')
                meta = dict(schema=module.SCHEMA, base={'test': 1}, recipe='test', sha256=checksum,
                            archive=archive.name, bytes=archive.stat().st_size, unpacked_bytes=6)
                module.atomic_json(pointer, dict(meta, base={'test': 2}))
                with self.assertRaisesRegex(ValueError, 'incompatível'):
                    module.restore()
                module.atomic_json(pointer, dict(meta, bytes=1))
                with self.assertRaisesRegex(ValueError, 'corrompida'):
                    module.restore()
                module.atomic_json(pointer, meta)
                validate.side_effect = ValueError('ambiente incompleto')
                with self.assertRaisesRegex(ValueError, 'incompleto'):
                    module.restore()
                self.assertFalse((root / module.COMPONENTS[0]).exists())
                self.assertFalse((root / 'installed.ok').exists())
                validate.side_effect = None
                # Symlinks são exercitados no Linux; no Windows não exigimos privilégio de criação.
                with patch.object(module, 'bind_drive'):
                    self.assertTrue(module.restore())
                self.assertEqual((root / 'installed.ok').read_text(), module.INSTALL_REVISION)
                self.assertTrue((root / 'runtime-image-active.json').is_file())
                with self.assertRaisesRegex(ValueError, 'Já existe'):
                    module.restore()
                self.assertEqual((root / module.COMPONENTS[0] / 'ComfyUI/main.py').read_bytes(), b'abc')


if __name__ == '__main__':
    unittest.main()
