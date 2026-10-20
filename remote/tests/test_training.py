import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from remote.common import Paths, Fault, atomic_json, digest
from remote.training import Training, settings, extract_dataset, verify_results, RECIPE, TRAINER_REVISION, MODEL_REVISION
from remote.training_worker import config_values
from remote.companion import Service


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = Paths(Path(self.temp.name)/'runtime', Path(self.temp.name)/'Drive/new')
        self.paths.state.mkdir(parents=True)
        self.training = Training(self.paths)

    def archive(self, caption='portrait of hrn_person', extra=None):
        file = Path(self.temp.name)/'dataset.zip'
        with zipfile.ZipFile(file, 'w') as bundle:
            for i in range(5):
                bundle.writestr(f'collection/imagens/{i}.jpg', b'image'+bytes([i]))
                bundle.writestr(f'collection/imagens/{i}.txt', caption)
            bundle.writestr('collection/revisao/contact.jpg', b'review')
            if extra: bundle.writestr(*extra)
        return file

    def saved(self):
        p = settings({})
        sha = 'a'*64
        project_id = hashlib.sha256(json.dumps([RECIPE, TRAINER_REVISION, MODEL_REVISION, sha, p], sort_keys=True).encode()).hexdigest()[:24]
        project = dict(id=project_id, settings=p, dataset_sha256=sha, recipe=RECIPE, trainer_revision=TRAINER_REVISION, model_revision=MODEL_REVISION, run=str(self.paths.drive/'output/training'/project_id/p['name']))
        atomic_json(self.training.projects/(project_id+'.json'), project)
        return project

    def test_rejects_executable_or_nonfinite_settings(self):
        for change in ({'name': '../escape'}, {'trigger': "x';import os"}, {'steps': True}, {'lr': float('nan')}, {'resolution': 769}, {'profile': 'shell'}, {'rank': 1000}):
            with self.subTest(change=change), self.assertRaises(Fault): settings(change)

    def test_extracts_only_training_pairs_preserving_bytes(self):
        file = self.archive()
        dest = Path(self.temp.name)/'images'
        rows = extract_dataset(file, dest, 'hrn_person')
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(list(dest.iterdir())), 10)
        self.assertEqual((dest/'00001.jpg').read_bytes(), b'image\0')

    def test_rejects_traversal_and_unmatched_captions(self):
        for file in (self.archive(extra=('../escape', 'x')),):
            with self.assertRaises(Fault): extract_dataset(file, Path(self.temp.name)/'images', 'hrn_person')
        file = self.archive(caption='someone else')
        with self.assertRaises(Fault): extract_dataset(file, Path(self.temp.name)/'images2', 'hrn_person')

    def test_requires_caption_for_every_photo(self):
        file = self.archive(extra=('collection/imagens/no-caption.jpg', b'image'))
        with self.assertRaises(Fault): extract_dataset(file, Path(self.temp.name)/'images', 'hrn_person')

    def test_rejects_ambiguous_image_folders(self):
        file = self.archive()
        with zipfile.ZipFile(file, 'a') as bundle:
            bundle.writestr('other/imagens/a.jpg', b'image')
            bundle.writestr('other/imagens/a.txt', 'hrn_person')
        with self.assertRaises(Fault): extract_dataset(file, Path(self.temp.name)/'images', 'hrn_person')

    def test_saved_project_cannot_redirect_outputs_or_execute_config(self):
        p = self.saved()
        self.assertEqual(self.training.project(p['id']), p)
        p['run'] = '/tmp/elsewhere'
        atomic_json(self.training.projects/(p['id']+'.json'), p)
        with self.assertRaises(Fault): self.training.project(p['id'])

    def test_unknown_or_active_state_blocks_shutdown_and_reconnect_preserves(self):
        self.training.require_safe()
        for value in ({'busy': True}, {'busy': False, 'preserved': False}, {}):
            atomic_json(self.training.file, value)
            with self.assertRaises(Fault): Training(self.paths).require_safe()
        atomic_json(self.training.file, {'busy': False, 'preserved': True})
        self.training.marker.touch()
        with self.assertRaises(Fault): self.training.require_safe()

    def test_preservation_hashes_drive_files_before_releasing_admission(self):
        p = self.saved()
        root = Path(p['run']); root.mkdir(parents=True)
        (root/'adapter.safetensors').write_bytes(b'weights')
        self.training.marker.touch()
        atomic_json(self.training.file, dict(job='test', project_id=p['id'], busy=False, preserved=False, phase='complete'))
        self.training.preserve()
        self.assertFalse(self.training.marker.exists())
        self.training.require_safe()
        receipt = json.loads((self.training.projects/(p['id']+'.receipt')).read_text())
        self.assertEqual(receipt['files'][0]['sha256'], digest(root/'adapter.safetensors'))

    def test_orphan_admission_marker_cannot_be_cleared_without_worker_journal(self):
        self.training.marker.touch()
        with self.assertRaises(Fault): self.training.preserve()
        self.assertTrue(self.training.marker.exists())

    def test_dead_worker_with_live_trainer_children_cannot_release_admission(self):
        p = self.saved()
        atomic_json(self.training.file, dict(job='test', project_id=p['id'], pid=12345, busy=False, preserved=False, phase='error'))
        self.training.marker.touch()
        with patch('remote.training.group_active', return_value=True), self.assertRaises(Fault): self.training.preserve()
        self.assertTrue(self.training.marker.exists())

    def test_no_start_without_current_vm_preparation(self):
        p = self.saved()
        with self.assertRaises(Fault): self.training.launch('start', {'project_id': p['id']})

    def test_start_cannot_overwrite_run_and_resume_needs_complete_checkpoint(self):
        p = self.saved()
        atomic_json(self.paths.root/'training/prepared'/(p['id']+'.json'), {'recipe': RECIPE})
        root = Path(p['run']); root.mkdir(parents=True)
        (root/'log.txt').write_text('existing')
        for action in ('start', 'resume'):
            with self.assertRaises(Fault): self.training.launch(action, {'project_id': p['id']})

    def test_recipe_preserves_effective_batch_and_disables_sampling(self):
        p = self.saved()
        values = config_values(p, self.paths.root/'training', Path('/source'), Path('/weights'), Path('/images'))
        self.assertEqual((values['MAX_SAMPLES'], values['GRAD_ACC']), (1, 2))
        self.assertEqual(values['SAMPLE_EVERY'], 0)
        self.assertEqual(values['MEMORY_PROFILE'], 'auto')
        self.assertEqual(values['TARGET_AREA'], 768**2)

    def test_metrics_checkpoint_and_export_are_real_files(self):
        p = self.saved(); root = Path(p['run'])
        checkpoint = root/'checkpoints/step_00000100'; checkpoint.mkdir(parents=True)
        for name in ('state.json', 'trainable.safetensors', 'optim.pt', 'rng.0.pt'): (checkpoint/name).write_bytes(b'valid')
        folder = checkpoint/'export'; folder.mkdir()
        (folder/'identity-comfy.safetensors').write_bytes(b'adapter')
        (root/'metrics.jsonl').write_text(json.dumps({'step': 100, 'step_time': 2, 'loss': .5})+'\n')
        atomic_json(self.training.file, dict(project_id=p['id'], busy=False, preserved=True, phase='stopped'))
        value = self.training.snapshot()
        self.assertEqual(value['checkpoints'], ['step_00000100'])
        self.assertEqual(value['eta_seconds'], 800)
        exported = self.training.export(p['id'])
        self.assertTrue(exported['verified'])
        self.assertEqual(Path(exported['path']).read_bytes(), b'adapter')
        self.assertEqual(self.training.export(p['id']), exported)

    def test_partial_checkpoints_are_never_offered_for_resume(self):
        p = self.saved(); root = Path(p['run'])
        (root/'checkpoints/step_00000100.partial').mkdir(parents=True)
        atomic_json(self.training.file, dict(project_id=p['id'], busy=False, preserved=True))
        self.assertEqual(self.training.snapshot()['checkpoints'], [])

    def test_prepare_wrong_hash_never_launches_worker(self):
        sha = 'a'*64
        uploads = self.paths.drive/'training/uploads'; uploads.mkdir()
        (uploads/(sha+'.zip')).write_bytes(b'wrong')
        with patch('remote.training.subprocess.Popen') as launch:
            with self.assertRaises(Fault): self.training.launch('prepare', {'dataset_sha256': sha})
        launch.assert_not_called()

    def test_service_stop_gates_training_before_any_preservation(self):
        config = dict(account_id='a', session_id='s', nonce='n', bearer_token='b'*48, output_mode='drive', gpu='CPU')
        service = Service(self.paths, config)
        atomic_json(service.training.file, dict(busy=True, preserved=False))
        for route in ('/stop/prepare', '/restart/prepare', '/runtime/save', '/outputs/delivery', '/cache/clear'):
            with self.subTest(route=route), self.assertRaises(Fault) as raised:
                service.route('POST', route, {}, {})
            self.assertEqual(raised.exception.code, 'training_busy')


if __name__ == '__main__': unittest.main()
