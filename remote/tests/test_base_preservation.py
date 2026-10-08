"""Only verified manifest Git fallbacks may bypass registry-version preservation."""
from contextlib import nullcontext
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from remote.common import Fault, Paths, atomic_json, read_json
from remote.companion import Service


class BasePreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = Paths(root / "runtime", root / "drive")
        self.paths.state.mkdir(parents=True)
        self.paths.models.mkdir(parents=True)
        self.config = dict(account_id="account", session_id="session", nonce="nonce", bearer_token="b"*48, gpu="CPU", output_mode="drive")
        self.service = Service(self.paths, self.config)
        self.service.cpu = False
        self.service.require_idle = Mock()
        self.service.outputs.barrier = lambda: nullcontext()
        self.service.nodes.list = Mock()

    def tearDown(self):
        self.service.downloads.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_registry_versions_preserve_without_git_exception(self):
        self.service.nodes.list.return_value = {"registered-node": {"ver": "1.2.3"}, "easy_runtime_guard": {"ver": "unknown"}, "easy_model_download": {"ver": "unknown"}}
        with patch("remote.bootstrap.verify_easy_node", create=True) as verify, patch("remote.companion.artifacts.save", return_value={"verified": True}) as save:
            self.assertTrue(self.service.save()["verified"])
        verify.assert_not_called()
        save.assert_called_once()

    def test_verified_git_fallback_is_saved_with_actual_installed_state(self):
        item = {"ver": "nightly", "enabled": True}
        self.service.nodes.list.return_value = {"fixed-base-alias": item}
        with patch("remote.bootstrap.verify_easy_node", return_value=True, create=True) as verify, patch("remote.companion.artifacts.save", return_value={"verified": True}) as save:
            self.assertTrue(self.service.save()["verified"])
        verify.assert_called_once_with(self.paths.root, "fixed-base-alias", item)
        self.assertEqual(read_json(self.paths.state / "nodes.json"), {"fixed-base-alias": item})
        save.assert_called_once()

    def test_real_git_pin_accepts_clean_base_and_rejects_tracked_change(self):
        folder = self.paths.comfy / "custom_nodes/BaseNode"
        folder.mkdir(parents=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init")
        source = folder / "__init__.py"
        source.write_text("# reviewed base source\n")
        git("add", "__init__.py")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
        repository = "https://example.invalid/reviewed-base.git"
        git("remote", "add", "origin", repository)
        package = {"id": "base-node", "aliases": ["base-alias"], "folder": "BaseNode", "source": "git", "revision": git("rev-parse", "HEAD"), "repository": repository}
        self.service.nodes.list.return_value = {"base-alias": {"ver": "nightly"}}
        with patch.dict("remote.companion.MANIFEST", {"packages": [package]}), patch("remote.companion.artifacts.save", return_value={"verified": True}) as save:
            self.assertTrue(self.service.save()["verified"])
            save.reset_mock()
            source.write_text("# changed tracked source\n")
            with self.assertRaises(Fault) as raised:
                self.service.save()
            self.assertEqual(raised.exception.code, "node_unpinned")
            save.assert_not_called()

    def test_unverified_nightly_never_changes_selected_artifact_or_node_receipt(self):
        previous = {"original": {"ver": "1.0.0"}}
        atomic_json(self.paths.state / "nodes.json", previous)
        atomic_json(self.paths.artifacts / "selected.json", {"original": True})
        for node_id in ("arbitrary-node", "fixed-base-alias"):
            self.service.nodes.list.return_value = {node_id: {"ver": "nightly"}}
            with patch("remote.bootstrap.verify_easy_node", return_value=False, create=True), patch("remote.companion.artifacts.save") as save, self.assertRaises(Fault) as raised:
                self.service.save()
            self.assertEqual(raised.exception.code, "node_unpinned")
            save.assert_not_called()
        self.assertEqual(read_json(self.paths.state / "nodes.json"), previous)
        self.assertEqual(read_json(self.paths.artifacts / "selected.json"), {"original": True})

    def test_manifest_base_ids_aliases_and_folders_block_generic_mutation(self):
        package = {"id": "base-node", "aliases": ["old-base-id"], "folder": "ComfyUI-Base-Node", "source": "git"}
        self.service.nodes.operate = Mock()
        with patch.dict("remote.companion.MANIFEST", {"packages": [package]}):
            for node_id in ("base-node", "old-base-id", "ComfyUI-Base-Node", "BASE-NODE", "core", "frontend", "manager", "mcp", "easy_runtime_guard", "easy_model_download"):
                for action in ("install", "update", "remove"):
                    with self.subTest(node_id=node_id, action=action), self.assertRaises(Fault) as raised:
                        self.service.route("POST", "/nodes/" + action, {}, {"id": node_id, "version": "2.0.0", "expected_version": "1.0.0"})
                    self.assertEqual(raised.exception.code, "protected_package")
        self.service.nodes.operate.assert_not_called()
        self.service.require_idle.assert_not_called()

    def test_registered_nonbase_mutation_keeps_existing_manager_flow(self):
        self.service.nodes.operate = Mock(return_value={"verified": True})
        self.service.outputs.resume = Mock()
        data = {"id": "community-node", "version": "2.0.0", "expected_version": "1.0.0"}
        with patch.dict("remote.companion.MANIFEST", {"packages": [{"id": "base-node", "aliases": [], "folder": "BaseNode"}]}), patch("remote.companion.validate_runtime"):
            result = self.service.route("POST", "/nodes/update", {}, data)
        self.assertTrue(result["verified"])
        self.service.nodes.operate.assert_called_once_with("update", data)
        self.service.require_idle.assert_called_once()
        self.service.outputs.resume.assert_called_once()

    def test_snapshot_installed_list_and_catalog_share_protection_matching(self):
        package = {"id": "base-node", "aliases": ["old-base-id"], "folder": "BaseNode", "source": "git"}
        installed = {"OLD-BASE-ID": {"ver": "nightly"}, "easy_model_download": {"ver": "unknown"}, "community-node": {"ver": "1.0.0"}}
        atomic_json(self.paths.state / "nodes.json", installed)
        self.service.nodes.list.return_value = installed
        self.service.nodes.registry.catalog = Mock(return_value={"items": [{"id": "base-node", "installed": True}, {"id": "BaseNode", "installed": True}, {"id": "community-node", "installed": True}], "total": 3, "offset": 0, "limit": 50})
        self.service.pending_outputs = Mock(return_value=0)
        with patch.dict("remote.companion.MANIFEST", {"packages": [package]}), patch("remote.companion.comfy_json", side_effect=Fault("comfy_unavailable", "Not running locally.")):
            snapshot = {item["id"]: item for item in self.service.snapshot()["packages"]}
            listed = {item["id"]: item for item in self.service.route("GET", "/nodes", {}, {})["items"]}
            catalog = {item["id"]: item for item in self.service.route("GET", "/nodes/catalog", {}, {})["items"]}
        for report in (snapshot, listed):
            self.assertTrue(report["OLD-BASE-ID"]["protected"])
            self.assertTrue(report["easy_model_download"]["protected"])
            self.assertFalse(report["community-node"]["protected"])
        self.assertTrue(snapshot["core"]["protected"])
        self.assertTrue(catalog["base-node"]["protected"])
        self.assertTrue(catalog["BaseNode"]["protected"])
        self.assertFalse(catalog["community-node"]["protected"])


if __name__ == "__main__":
    unittest.main()
