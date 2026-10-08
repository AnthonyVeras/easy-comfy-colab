from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from controller.google import GoogleAuth
from controller.import_legacy import import_legacy
from controller.state import AppError, Store, data_dir


def crypt(data, decrypt=False):
    return data[7:] if decrypt else b"sealed:" + data


class AuthImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "destination", crypt=crypt)
        self.auth = GoogleAuth(self.store, lambda *_: None)
        self.original = self.root / "legacy-home"
        self.windows = self.root / "legacy-windows"
        self.windows.mkdir()
        self.info = {"type": "authorized_user", "refresh_token": "synthetic-refresh", "client_id": "synthetic-original-client", "client_secret": "synthetic-secret"}
        self.who = {"sub": "subject-a", "email": "a@example.invalid", "email_verified": True}
        self.session = SimpleNamespace(credentials=SimpleNamespace(to_json=lambda: json.dumps(self.info)))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def fixture(self):
        self.write(self.windows / "profiles.json", {"selected": "default", "profiles": [{"id": "default", "label": "Principal", "email": "a@example.invalid", "gpu": "L4", "output_mode": "pc"}]})
        self.write(self.original / ".config/colab-cli/token.json", self.info)
        self.write(self.original / ".local/share/comfy-colab/drive/default/token.json", self.info)
        self.write(self.original / ".config/comfy-colab/drive-oauth-client.json", {"installed": {"client_id": "synthetic-client", "client_secret": "synthetic-drive-client-secret", "redirect_uris": ["https://accounts.google.com/example"]}})
        self.write(self.windows / "model-credentials.json", {"default:Hugging Face": base64.b64encode(b"synthetic-old-encrypted").decode(), "default:Civitai": base64.b64encode(b"synthetic-old-encrypted").decode(), "default:Telegram": "never-import"})

    def test_import_preserves_originals_client_binding_and_excludes_sessions(self):
        self.fixture()
        self.write(self.original / ".local/share/comfy-colab/sessions.json", {"endpoint": "legacy-endpoint"})
        original_files = {p: p.read_bytes() for p in self.root.rglob("*.json")}
        with patch("controller.import_legacy.authorized", return_value=self.session), patch("controller.import_legacy.identity", return_value=self.who), patch("controller.google.identity", return_value=self.who), patch("controller.import_legacy.protect", return_value=b"synthetic-provider-token"):
            result = import_legacy(self.store, self.auth, source_root=self.original, windows_root=self.windows)
        self.assertEqual(result["imported"], 1)
        self.assertTrue(result["sources_preserved"])
        account = result["accounts"][0]["id"]
        self.assertEqual(self.store.secret(account, "google_colab")["client_id"], "synthetic-original-client")
        self.assertEqual(self.store.secret(account, "google_drive")["client_secret"], "synthetic-secret")
        self.assertEqual(self.store.secret(account, "Hugging Face"), "synthetic-provider-token")
        self.assertEqual(self.store.secret(account, "google_drive_client")["installed"]["client_id"], "synthetic-client")
        self.assertIsNone(self.store.secret(account, "Telegram"))
        self.assertIsNone(self.store.get("active_session"))
        self.assertNotIn("synthetic-secret", json.dumps(result))
        for path, content in original_files.items():
            self.assertEqual(path.read_bytes(), content)

    def test_wrong_account_import_does_not_create_profile(self):
        self.fixture()
        with patch("controller.import_legacy.authorized", return_value=self.session), patch("controller.import_legacy.identity", return_value={**self.who, "email": "another@example.invalid"}):
            result = import_legacy(self.store, self.auth, source_root=self.original, windows_root=self.windows)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(self.store.accounts(), [])
        self.assertEqual(result["errors"][0]["code"], "legacy_login_unavailable")

    def test_adc_fallback_and_duplicate_import_keep_one_identity(self):
        self.fixture()
        (self.original / ".config/colab-cli/token.json").unlink()
        self.write(self.original / ".config/gcloud/application_default_credentials.json", self.info)
        with patch("controller.import_legacy.authorized", return_value=self.session), patch("controller.import_legacy.identity", return_value=self.who), patch("controller.google.identity", return_value=self.who), patch("controller.import_legacy.protect", return_value=b"synthetic-provider-token"):
            first = import_legacy(self.store, self.auth, source_root=self.original, windows_root=self.windows)
            second = import_legacy(self.store, self.auth, source_root=self.original, windows_root=self.windows)
        self.assertEqual(len(self.store.accounts()), 1)
        self.assertEqual(first["accounts"][0]["id"], second["accounts"][0]["id"])

    def test_authorized_session_cannot_bind_wrong_subject(self):
        account = self.store.add()
        self.store.bind(account, self.who)
        with patch("controller.google.identity", return_value={**self.who, "sub": "another"}), self.assertRaises(AppError):
            self.auth.save(account, self.session)
        self.assertIsNone(self.store.secret(account, "google_colab"))

    def test_drive_authorization_requires_same_subject(self):
        account = self.store.add()
        self.store.bind(account, self.who)
        with patch("controller.google.identity", return_value={**self.who, "sub": "another"}), self.assertRaises(AppError):
            self.auth.save(account, self.session, purpose="drive")
        self.assertIsNone(self.store.secret(account, "google_drive"))

    def test_cached_account_context_not_global_default(self):
        account = self.store.add()
        self.store.bind(account, self.who)
        self.store.secret(account, "google_colab", self.info)
        with patch("controller.google.authorized", return_value=self.session), patch("controller.google.identity", return_value=self.who):
            self.assertIs(self.auth.context(account), self.session)
            self.assertIs(self.auth.context(account), self.session)
        self.assertEqual(set(self.auth.sessions), {(account, "colab")})

    def test_benchmark_data_dir_explicit_override(self):
        with patch.dict("os.environ", {"ECC_DATA_DIR": str(self.root / "isolated"), "LOCALAPPDATA": str(self.root / "real")}), patch("controller.state.Path.home", return_value=self.root):
            self.assertEqual(data_dir(), (self.root / "isolated").resolve())
            isolated = Store()
            try:
                isolated.add("Isolated")
            finally:
                isolated.close()
        self.assertFalse((self.root / "real").exists())

    def test_invalid_legacy_metadata_does_not_import_or_mutate(self):
        self.write(self.windows / "profiles.json", [])
        with self.assertRaises(AppError) as error:
            import_legacy(self.store, self.auth, source_root=self.original, windows_root=self.windows)
        self.assertEqual(error.exception.code, "legacy_profiles_invalid")
        self.assertEqual(self.store.accounts(), [])

    def test_drive_verification_requires_filename_or_tracked_copy(self):
        from controller.drive import version, verified
        source = {"id": "source", "name": "expected.gguf", "mimeType": "application/octet-stream", "size": "3", "md5Checksum": "synthetic-md5", "version": 1}
        target = {**source, "id": "target", "name": "another.gguf"}
        self.assertFalse(verified(source, target))
        target["appProperties"] = {"easySource": "source", "easyVersion": version(source)}
        self.assertTrue(verified(source, target))

    def test_drive_conflict_copy_keeps_model_suffix_and_resumes_verified_copy(self):
        from controller.drive import FOLDER, transfer, version
        from unittest.mock import Mock, patch
        source_account = self.store.add("Source")
        dest_account = self.store.add("Destination")
        self.store.bind(source_account, {"sub": "source-sub", "email": "source@example.invalid", "email_verified": True})
        self.store.bind(dest_account, {"sub": "dest-sub", "email": "dest@example.invalid", "email_verified": True})
        source, dest = Mock(), Mock()
        source.root.return_value = {"id": "source-root", "mimeType": FOLDER, "name": "EasyComfyColab2"}
        dest.root.return_value = {"id": "destination-root"}
        original = {"id": "original", "name": "model.safetensors", "mimeType": "application/octet-stream", "size": "3", "md5Checksum": "original-md5", "version": 1}
        conflict = {**original, "id": "other", "md5Checksum": "different-md5"}
        copied = {**original, "id": "copy", "name": "model (copy " + version(original)[:8] + ").safetensors", "appProperties": {"easySource": "original", "easyVersion": version(original)}}
        source.children.return_value = [original]
        dest.children.return_value = [conflict]
        dest.request.side_effect = [{"storageQuota": {"limit": "100", "usage": "0"}}, copied]
        auth = Mock(store=self.store)
        params = {"source_account_id": source_account, "destination_account_id": dest_account}
        with patch("controller.drive.Drive", side_effect=[source, dest]):
            result = transfer(auth, params, "copy", lambda *_: None)
        self.assertTrue(result["verified"])
        self.assertTrue(result["renamed_conflicts"][0]["destination_name"].endswith(".safetensors"))
        self.assertEqual(dest.request.call_args.kwargs["json"]["name"], copied["name"])
        dest.request.reset_mock()
        dest.request.side_effect = [{"storageQuota": {"limit": "100", "usage": "0"}}]
        dest.children.return_value = [conflict, copied]
        with patch("controller.drive.Drive", side_effect=[source, dest]):
            resumed = transfer(auth, params, "copy", lambda *_: None)
        self.assertEqual(resumed["bytes_copied"], 0)
        self.assertEqual(dest.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
