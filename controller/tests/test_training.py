from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from controller.training import handle
from controller.state import AppError


class TrainingControllerTests(unittest.TestCase):
    def setUp(self):
        self.remote = Mock()
        self.controller = SimpleNamespace(require_remote=Mock(return_value=self.remote), session={'account_id':'a', 'session_id':'s'}, transport=Mock(), emit=Mock())
        self.params = {'account_id':'a', 'session_id':'s'}

    def test_foreign_session_cannot_train_or_upload(self):
        for action in ('training.status', 'training.start', 'training.import'):
            with self.assertRaises(AppError): handle(self.controller, action, {**self.params, 'session_id':'foreign'})
        self.remote.call.assert_not_called()

    def test_status_and_commands_use_owned_companion_only(self):
        handle(self.controller, 'training.status', self.params)
        self.remote.call.assert_called_once_with('/training')
        handle(self.controller, 'training.resume', {**self.params, 'project_id':'p'})
        self.remote.call.assert_called_with('/training/resume', {'project_id':'p'}, timeout=600)

    def test_import_cannot_run_during_training(self):
        self.remote.call.return_value = {'busy':True, 'preserved':False}
        with self.assertRaises(AppError): handle(self.controller, 'training.import', {**self.params, 'path':'dataset.zip'})
        self.controller.transport.sftp.put.assert_not_called()

    def test_upload_checksum_failure_never_publishes_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp)/'data.zip'; file.write_bytes(b'correct')
            self.remote.call.return_value = {'busy':False, 'preserved':True}
            self.remote.health.return_value = {'paths':{'models':'/content/drive/MyDrive/EasyComfyColab2/models'}}
            import io
            self.controller.transport.sftp.open.return_value = io.BytesIO(b'corrupt')
            with self.assertRaises(AppError): handle(self.controller, 'training.import', {**self.params, 'path':str(file)})
            self.controller.transport.sftp.rename.assert_not_called()
            self.controller.transport.sftp.remove.assert_called_once()
