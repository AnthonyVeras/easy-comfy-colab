import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from remote import artifacts, bootstrap
from remote.cache import Cache, model_paths
from remote.common import Fault, Paths, atomic_bytes, atomic_json, authorize, digest, inside, model_path, read_json, validate_identity
from remote.companion import Server, Service, page, body_limit
from remote.downloads import Downloads, open_source, source_url
from remote.nodes import Nodes
from remote.registry import Registry
from remote.outputs import Outputs
from remote.gate import protected, controlled


def config(mode="pc", gpu="CPU"):
    return dict(account_id="account", session_id="session", nonce="nonce", bearer_token="b"*48, output_mode=mode, gpu=gpu)


class Temporary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = Paths(root / "runtime", root / "Drive/NewProduct")
        self.paths.state.mkdir(parents=True)
        self.paths.models.mkdir(parents=True)

    def assertFault(self, code, fn, *args, **kwargs):
        with self.assertRaises(Fault) as raised: fn(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def symlink(self, link, target):
        try: link.symlink_to(target)
        except OSError: self.skipTest("Windows symlink creation is unavailable")


class Boundaries(Temporary):
    def test_rejects_traversal_and_link_escape(self):
        for value in ("../other", "/etc", "C:/x", "a\\b", "a//b", "./x", "a/../b", "x\n"):
            self.assertFault("invalid_path", inside, self.paths.models, value)
        outside = Path(self.temp.name)/"outside"; outside.mkdir()
        self.symlink(self.paths.models/"escape", outside)
        self.assertFault("invalid_path", inside, self.paths.models, "escape/x")

    def test_valid_model_categories_and_extensions(self):
        self.assertEqual(model_path(self.paths.models, "loras", "file.gguf"), self.paths.models/"loras/file.gguf")
        for category, filename in (("bad", "file.gguf"),("loras", "file.py"),("loras", "folder/file.bin"),("loras", None)):
            with self.assertRaises(Fault): model_path(self.paths.models, category, filename)

    def test_identity_auth_binds_every_field(self):
        c = config(); validate_identity(c)
        headers = {"Authorization":"Bearer "+c["bearer_token"],"X-Easy-Account":"account","X-Easy-Session":"session","X-Easy-Nonce":"nonce"}
        authorize(c, headers)
        for key in headers:
            changed = dict(headers); changed[key] = "other"
            with self.assertRaises(Fault): authorize(c, changed)
        c["bearer_token"] = "short"; self.assertFault("invalid_identity", validate_identity, c)

    def test_atomic_publication_keeps_previous_on_failure(self):
        target = self.paths.root/"file"; atomic_bytes(target, b"good")
        with patch("remote.common.os.replace", side_effect=OSError("full")):
            with self.assertRaises(OSError): atomic_bytes(target,b"new")
        self.assertEqual(target.read_bytes(),b"good")
        self.assertEqual(list(target.parent.glob(".publish-*")),[])

    def test_pagination_bounds(self):
        self.assertEqual(page({"offset":["7"],"limit":["10"]}),(7,10))
        for value in ({"limit":["101"]},{"offset":["-1"]},{"limit":["bad"]}): self.assertFault("invalid_page",page,value)

    def test_admission_covers_manager_and_prompt_without_controlled_prompt_bypass(self):
        self.assertTrue(protected("POST","/prompt"));self.assertTrue(protected("POST","/v2/manager/queue/task"))
        self.assertFalse(protected("GET","/v2/customnode/installed"))
        self.assertFalse(controlled("/v2/manager/queue/task",{},config()))
        headers={"X-Easy-Control":config()["bearer_token"]}
        self.assertTrue(controlled("/v2/manager/queue/task",headers,config()))
        self.assertFalse(controlled("/prompt",headers,config()))


class URLTests(unittest.TestCase):
    def test_huggingface_canonical_source(self):
        value = source_url("https://huggingface.co/org/repo/blob/main/file.safetensors?download=true")
        self.assertEqual(value,("https://huggingface.co/org/repo/resolve/main/file.safetensors","file.safetensors","huggingface.co"))
        self.assertEqual(source_url("https://civitai.com/api/download/models/123?format=SafeTensor", "file.safetensors")[1],"file.safetensors")

    def test_rejects_encoded_credentials_and_traversal(self):
        values = ["https://user:pass@huggingface.co/org/repo/resolve/main/a.bin", "https://user%40huggingface.co/org/repo/resolve/main/a.bin", "https://huggingface.co/org/repo/resolve/main/%2e%2e/a.bin", "https://huggingface.co/org/repo/resolve/main/%2fetc.bin", "https://huggingface.co/org/repo/resolve/main/a.bin?%74oken=secret", "https://huggingface.co/org/repo/resolve/main/a.bin#x", "http://huggingface.co/org/repo/resolve/main/a.bin", "https://huggingface.co:444/org/repo/resolve/main/a.bin", "https://evil.example/org/repo/resolve/main/a.bin", "https://civitai.com/api/download/models/1"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(Fault): source_url(value)

    def test_redirect_strips_bearer_and_refuses_unknown_host(self):
        seen=[]
        class Opener:
            def open(self, request, timeout):
                seen.append(request)
                if len(seen)==1: raise HTTPError(request.full_url,302,"redirect",{"Location":"https://cdn.hf.co/file"},io.BytesIO())
                return "response"
        with patch("remote.downloads.build_opener",return_value=Opener()):
            self.assertEqual(open_source("https://huggingface.co/o/r/resolve/main/a.bin","token",{}),"response")
        self.assertEqual(seen[0].get_header("Authorization"),"Bearer token")
        self.assertIsNone(seen[1].get_header("Authorization"))
        class Unsafe:
            def open(self, request, timeout): raise HTTPError(request.full_url,302,"redirect",{"Location":"https://evil.example/file"},io.BytesIO())
        with patch("remote.downloads.build_opener",return_value=Unsafe()),self.assertRaises(Fault): open_source("https://huggingface.co/o/r/resolve/main/a.bin","token",{})


class Response(io.BytesIO):
    def __init__(self, content, status=200, headers=None):
        super().__init__(content); self.status=status
        self.headers = headers or {"Content-Length":str(len(content)),"ETag":"fixed"}


class DownloadTests(Temporary):
    def queue(self, response=None, opener=None):
        d=Downloads(self.paths,opener=opener or (lambda *args: Response(response or b"bytes")))
        self.addCleanup(d.executor.shutdown,True)
        return d

    def submit(self, d, **entry):
        item = {"url":"https://huggingface.co/o/r/resolve/main/a.safetensors",**entry}
        result=d.add({"urls":[item],"category":"loras","tokens":{"huggingface":"private"}})
        d.executor.shutdown(wait=True)
        return next(j for j in d.jobs if j["id"]==result["ids"][0])

    def test_verified_download_atomic_and_no_secret_on_disk(self):
        d=self.queue(b"model")
        j=self.submit(d,sha256=hashlib.sha256(b"model").hexdigest())
        self.assertEqual(j["status"],"complete")
        self.assertEqual((self.paths.models/"loras/a.safetensors").read_bytes(),b"model")
        self.assertFalse((self.paths.models/"loras/a.safetensors.part").exists())
        self.assertNotIn("private",(self.paths.state/"downloads.json").read_text())
        self.assertEqual(d.tokens,{})

    def test_civitai_filename_text_contract(self):
        d=self.queue(b"model")
        result=d.add({"urls":["https://civitai.com/api/download/models/123 | chosen.safetensors"],"category":"loras"})
        d.executor.shutdown(wait=True)
        self.assertEqual(d.jobs[0]["status"],"complete");self.assertEqual(d.jobs[0]["filename"],"chosen.safetensors")

    def test_digest_and_size_failure_never_publish(self):
        for i,(response, expected) in enumerate(((Response(b"bad"),"0"*64),(Response(b"bad",headers={"Content-Length":"40"}),None))):
            d=self.queue(opener=lambda *args,r=response:r)
            j=self.submit(d,sha256=expected,filename=f"{i}.safetensors")
            self.assertEqual(j["status"],"error")
            self.assertEqual(j["error"]["code"],"integrity_failed")
            self.assertFalse((self.paths.models/"loras/a.safetensors").exists())

    def test_existing_model_not_overwritten(self):
        target=self.paths.models/"loras/a.safetensors"; target.parent.mkdir(); target.write_bytes(b"original")
        d=self.queue(); j=self.submit(d)
        self.assertEqual(j["error"]["code"],"model_conflict")
        self.assertEqual(target.read_bytes(),b"original")

    def test_partial_identity_conflict(self):
        target=self.paths.models/"loras/a.safetensors.part"; target.parent.mkdir();target.write_bytes(b"old")
        atomic_json(target.with_name(target.name+".json"),{"identity":"other","etag":"fixed"})
        d=self.queue();j=self.submit(d)
        self.assertEqual(j["error"]["code"],"model_conflict");self.assertEqual(target.read_bytes(),b"old")

    def test_resume_range_etag_and_complete_length(self):
        url="https://huggingface.co/o/r/resolve/main/a.safetensors"
        identity=hashlib.sha256(json.dumps([url,"loras","a.safetensors",None]).encode()).hexdigest()
        partial=self.paths.models/"loras/a.safetensors.part";partial.parent.mkdir();partial.write_bytes(b"first")
        atomic_json(partial.with_name(partial.name+".json"),{"identity":identity,"etag":"fixed"})
        seen=[]
        def opener(url,token,headers):
            seen.append(headers);return Response(b"last",206,{"Content-Length":"4","ETag":"fixed","Content-Range":"bytes 5-8/9"})
        d=self.queue(opener=opener);j=self.submit(d)
        self.assertEqual(j["status"],"complete");self.assertEqual(seen[0]["Range"],"bytes=5-")
        self.assertEqual((self.paths.models/"loras/a.safetensors").read_bytes(),b"firstlast")

    def test_paused_jobs_survive_process_restart(self):
        atomic_json(self.paths.state/"downloads.json",[{"status":"running"}])
        d=self.queue();self.assertEqual(d.jobs[0]["status"],"paused");self.assertFalse(d.busy)

    def test_partial_symlink_is_rejected(self):
        target=self.paths.models/"loras/a.safetensors.part";target.parent.mkdir()
        original=Path(self.temp.name)/"outside";original.write_bytes(b"original")
        self.symlink(target,original)
        d=self.queue();j=self.submit(d)
        self.assertEqual(j["error"]["code"],"invalid_path");self.assertEqual(original.read_bytes(),b"original")

    def test_resume_tokens_are_validated(self):
        d=self.queue();self.assertFault("invalid_credentials",d.action,"x","resume",{"huggingface":"line\n"})


class OutputTests(Temporary):
    def test_unreadable_or_missing_root_never_produces_empty_manifest(self):
        outputs=Outputs(self.paths,config())
        original=Path.lstat
        def denied(path,*args,**kwargs):
            if path==outputs.root: raise PermissionError("synthetic")
            return original(path,*args,**kwargs)
        with patch.object(Path,"lstat",denied):
            self.assertFault("unsafe_output",outputs.prepare)
            self.assertFault("unsafe_output",outputs.pending,quick=True)
        self.assertFalse((self.paths.state/"output-manifest.json").exists())
        outputs.root.rmdir(); self.assertFault("unsafe_output",outputs.prepare)

    def test_walk_or_entry_stat_errors_preserve_existing_output_and_receipts(self):
        outputs=Outputs(self.paths,config()); file=outputs.root/"a.png"; file.write_bytes(b"original")
        def unreadable(path,**kwargs):
            kwargs["onerror"](OSError("synthetic disk error"))
            return iter(())
        with patch("remote.outputs.os.walk",side_effect=unreadable): self.assertFault("unsafe_output",outputs.prepare)
        original=Path.lstat
        def denied(path,*args,**kwargs):
            if path==file: raise PermissionError("synthetic")
            return original(path,*args,**kwargs)
        with patch.object(Path,"lstat",denied): self.assertFault("unsafe_output",outputs.prepare)
        self.assertEqual(file.read_bytes(),b"original")
        self.assertFalse((self.paths.state/"output-manifest.json").exists())
        self.assertFalse(outputs.ack_path.exists())

    def test_capture_ack_and_changed_output_gate(self):
        outputs=Outputs(self.paths,config());(outputs.root/"result.png").write_bytes(b"first")
        m=outputs.prepare();self.assertEqual(m["pending_outputs"],1)
        self.assertTrue((self.paths.state/"stopping").exists())
        self.assertFault("output_mismatch",outputs.ack,{"manifest_id":m["manifest_id"],"files":[]})
        copied=[{k:r[k] for k in ("relative_path","size","sha256")} for r in m["files"]]
        self.assertTrue(outputs.ack({"manifest_id":m["manifest_id"],"files":copied})["verified"])
        self.assertEqual(outputs.pending(),0);self.assertEqual(outputs.pending(quick=True),0)
        (outputs.root/"result.png").write_bytes(b"other")
        self.assertEqual(outputs.pending(),1)
        self.assertFault("output_mismatch",outputs.ack,{"manifest_id":m["manifest_id"],"files":copied})
        outputs.resume();self.assertFalse((self.paths.state/"stopping").exists())

    def test_queue_unknown_or_busy_blocks_capture(self):
        outputs=Outputs(self.paths,config(gpu="T4"),idle=lambda:False)
        self.assertFault("generation_busy",outputs.prepare)
        self.assertFalse((self.paths.state/"stopping").exists())
        with patch.object(outputs,"idle",side_effect=Fault("queue_unknown","Unknown")):
            self.assertFault("queue_unknown",outputs.prepare)

    def test_drive_captures_persistent_output_hashes(self):
        outputs=Outputs(self.paths,config("drive"));(outputs.root/"video.mp4").write_bytes(b"video")
        m=outputs.prepare();self.assertEqual(m["pending_outputs"],0);self.assertEqual(len(m["files"]),1)

    def test_incomplete_and_linked_outputs_block_shutdown(self):
        outputs=Outputs(self.paths,config());partial=outputs.root/"a.part";partial.write_bytes(b"partial")
        self.assertFault("output_incomplete",outputs.prepare)
        partial.unlink();original=Path(self.temp.name)/"outside";original.write_bytes(b"original")
        self.symlink(outputs.root/"outside",original);self.assertFault("unsafe_output",outputs.prepare)


class ArtifactTests(Temporary):
    def fixture(self):
        for component in artifacts.COMPONENTS:
            directory=self.paths.root/component;directory.mkdir();(directory/"data").write_bytes(component.encode())
        (self.paths.comfy/"models").mkdir();(self.paths.comfy/"models/secret.bin").write_bytes(b"model-excluded")
        (self.paths.comfy/".env").write_bytes(b"secret-excluded")

    def remove_install(self):
        for name in artifacts.COMPONENTS: __import__("shutil").rmtree(self.paths.root/name)

    @staticmethod
    def fake_link(path, target): path.write_bytes(b"fake interpreter")

    @unittest.skipUnless(shutil.which("git"), "Git is needed for detached repository regression")
    def test_detached_git_empty_refs_is_restored_before_runtime_validation(self):
        self.fixture()
        def git(root, *args):
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
        git(self.paths.comfy, "init")
        git(self.paths.comfy, "add", "data")
        git(self.paths.comfy, "-c", "user.name=Runtime test", "-c", "user.email=runtime-test@example.invalid", "commit", "-m", "fixture")
        expected = git(self.paths.comfy, "rev-parse", "HEAD")
        git(self.paths.comfy, "checkout", "--detach", expected)
        git(self.paths.comfy, "update-ref", "-d", "refs/heads/master")
        git(self.paths.comfy, "update-ref", "-d", "refs/heads/main")
        (self.paths.comfy/".git/config").write_text("[core]\nrepositoryformatversion=0\nfilemode=false\nbare=false\nlogallrefupdates=true\n", encoding="utf-8")
        self.assertFalse(any((self.paths.comfy/".git/refs").rglob("*.*")))
        saved = artifacts.save(self.paths)
        with tarfile.open(self.paths.artifacts/saved["manifest"]["archive"]) as archive:
            self.assertFalse(any(name.startswith("ComfyUI/.git/refs/") for name in archive.getnames()))
        for file in self.paths.comfy.rglob("*"):
            if file.is_file(): file.chmod(0o600)
        self.remove_install()
        with patch.object(Path, "symlink_to", self.fake_link):
            self.assertTrue(artifacts.restore(self.paths, lambda stage: self.assertEqual(git(stage/"ComfyUI", "rev-parse", "HEAD"), expected)))
        self.assertEqual(git(self.paths.comfy, "rev-parse", "HEAD"), expected)

    def test_dependency_models_source_survives_archive_and_restore(self):
        self.fixture()
        required = ["venv/lib/site-packages/transformers/models/clip/tokenization_clip.py",
                    "mcp-venv/lib/site-packages/example/input/module.py",
                    "ComfyUI/custom_nodes/registered_node/models/module.py"]
        for name in required:
            file=self.paths.root/name; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(b"dependency source")
        saved=artifacts.save(self.paths)
        with tarfile.open(self.paths.artifacts/saved["manifest"]["archive"]) as archive:
            entries=set(archive.getnames())
            self.assertTrue(all(name in entries for name in required))
            self.assertNotIn("ComfyUI/models/secret.bin",entries)
        self.remove_install()
        def validate(stage):
            for name in required: self.assertEqual((stage/name).read_bytes(),b"dependency source")
        with patch.object(Path,"symlink_to",self.fake_link): self.assertTrue(artifacts.restore(self.paths,validate))

    def test_unreadable_runtime_scan_cannot_replace_selected_or_last_good(self):
        self.fixture(); first=artifacts.save(self.paths)["manifest"]
        (self.paths.comfy/"data").write_bytes(b"second complete runtime")
        selected=artifacts.save(self.paths)["manifest"]
        def unreadable(path,**kwargs):
            kwargs["onerror"](OSError("synthetic disk failure"))
            return iter(())
        with patch("remote.artifacts.os.walk",side_effect=unreadable): self.assertFault("unsafe_artifact",artifacts.save,self.paths)
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),selected)
        self.assertEqual(read_json(self.paths.artifacts/"last-good.json"),first)

    def test_unreadable_component_directory_or_file_is_not_omitted(self):
        self.fixture(); selected=artifacts.save(self.paths)["manifest"]
        original=Path.lstat
        for denied in (self.paths.root/"venv",self.paths.comfy/"data"):
            def inaccessible(path,*args,**kwargs):
                if path==denied: raise PermissionError("synthetic")
                return original(path,*args,**kwargs)
            with patch.object(Path,"lstat",inaccessible): self.assertFault("unsafe_artifact",artifacts.save,self.paths)
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),selected)

    def two_versions(self):
        self.fixture()
        atomic_json(self.paths.state/"nodes.json",{"node":{"ver":"1.0.0"}})
        old = artifacts.save(self.paths)["manifest"]
        (self.paths.comfy/"data").write_bytes(b"new content")
        atomic_json(self.paths.state/"nodes.json",{"node":{"ver":"2.0.0"}})
        selected = artifacts.save(self.paths)["manifest"]
        self.remove_install()
        return old, selected

    def test_recipe_tracks_installation_inputs_and_guard_only(self):
        home = self.paths.root/"recipe-source"; home.mkdir()
        manifest = dict(bootstrap.MANIFEST)
        atomic_json(home/"runtime.json",manifest)
        for name in ("admission.py", "gate.py", "companion.py", "downloads.py", "bootstrap.py"):
            (home/name).write_text("original",encoding="utf-8")
        with patch("remote.artifacts.__file__",str(home/"artifacts.py")):
            first = artifacts.recipe()
            for name in ("companion.py", "downloads.py", "bootstrap.py"):
                (home/name).write_text("changed",encoding="utf-8")
            manifest["ports"] = {"companion":9999}; atomic_json(home/"runtime.json",manifest)
            self.assertEqual(artifacts.recipe(),first)
            manifest["install_revision"] += 1; atomic_json(home/"runtime.json",manifest)
            self.assertNotEqual(artifacts.recipe(),first)
            manifest["install_revision"] -= 1; atomic_json(home/"runtime.json",manifest)
            (home/"gate.py").write_text("new admission policy",encoding="utf-8")
            self.assertNotEqual(artifacts.recipe(),first)

    def test_corrupt_selected_archive_restores_last_good_and_promotes_after_validation(self):
        old, selected = self.two_versions()
        atomic_json(self.paths.state/"session.json",config())
        archive=self.paths.artifacts/selected["archive"];archive.write_bytes(b"corrupt selected")
        def validate(root):
            self.assertEqual((root/"ComfyUI/data").read_bytes(),b"ComfyUI")
            self.assertEqual(read_json(self.paths.artifacts/"selected.json"),selected)
        with patch.object(Path,"symlink_to",self.fake_link): self.assertTrue(artifacts.restore(self.paths,validate))
        self.assertEqual((self.paths.comfy/"data").read_bytes(),b"ComfyUI")
        self.assertEqual(read_json(self.paths.state/"nodes.json"),old["node_manifest"])
        self.assertEqual(read_json(self.paths.state/"session.json"),config())
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),old)
        self.assertEqual(read_json(self.paths.artifacts/"last-good.json"),old)
        self.assertEqual(archive.read_bytes(),b"corrupt selected")
        self.assertEqual(read_json(self.paths.state/"runtime-restored.json"),{"pointer":"last-good.json","sha256":old["sha256"],"fallback":True,"previous_error":"artifact_corrupt"})

    def test_corrupt_selected_json_restores_last_good(self):
        old, selected = self.two_versions()
        (self.paths.artifacts/"selected.json").write_text("invalid json",encoding="utf-8")
        with patch.object(Path,"symlink_to",self.fake_link):self.assertTrue(artifacts.restore(self.paths))
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),old)
        self.assertEqual(read_json(self.paths.state/"runtime-restored.json")["previous_error"],"invalid_state")

    def test_selected_validation_failure_restores_last_good_in_fresh_stage(self):
        old, selected = self.two_versions();validated=[]
        def validate(root):
            validated.append(root)
            if (root/"ComfyUI/data").read_bytes()==b"new content": raise Fault("runtime_incompatible","Synthetic dependency mismatch")
        with patch.object(Path,"symlink_to",self.fake_link):self.assertTrue(artifacts.restore(self.paths,validate))
        self.assertEqual(len(validated),2);self.assertNotEqual(validated[0],validated[1])
        self.assertFalse(validated[0].exists());self.assertEqual(read_json(self.paths.artifacts/"selected.json"),old)

    def test_corrupt_selected_with_incompatible_last_good_fails_closed(self):
        old, selected=self.two_versions()
        (self.paths.artifacts/selected["archive"]).write_bytes(b"corrupt")
        incompatible=dict(old,base={"different":True});atomic_json(self.paths.artifacts/"last-good.json",incompatible)
        self.assertFault("artifact_corrupt",artifacts.restore,self.paths)
        self.assertFalse(any((self.paths.root/n).exists() for n in artifacts.COMPONENTS))
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),selected)

    def test_corrupt_both_artifacts_never_publish(self):
        old, selected=self.two_versions()
        for manifest in (old,selected):(self.paths.artifacts/manifest["archive"]).write_bytes(b"corrupt")
        self.assertFault("artifact_corrupt",artifacts.restore,self.paths)
        self.assertFalse(any((self.paths.root/n).exists() for n in artifacts.COMPONENTS))

    def test_disk_failure_does_not_retry_or_promote_last_good(self):
        old, selected=self.two_versions()
        with patch("remote.artifacts.shutil.disk_usage",return_value=type("Space",(),{"free":0})()):self.assertFault("disk_full",artifacts.restore,self.paths)
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),selected)

    def test_failed_last_good_validation_preserves_original_selection_and_files(self):
        old,selected=self.two_versions()
        (self.paths.artifacts/selected["archive"]).write_bytes(b"corrupt selected")
        def invalid(root):raise Fault("guard_missing","Synthetic invalid guard")
        with patch.object(Path,"symlink_to",self.fake_link):self.assertFault("artifact_corrupt",artifacts.restore,self.paths,invalid)
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),selected)
        self.assertEqual(read_json(self.paths.artifacts/"last-good.json"),old)
        self.assertTrue((self.paths.artifacts/old["archive"]).is_file())
        self.assertFalse(any((self.paths.root/n).exists() for n in artifacts.COMPONENTS))

    def test_deterministic_archive_ignores_mtime_and_secrets(self):
        self.fixture();a=self.paths.root/"a.tar.gz";b=self.paths.root/"b.tar.gz"
        artifacts.archive_bytes(self.paths.root,a)
        for file in artifacts.files(self.paths.root): os.utime(file,(1234567,1234567))
        artifacts.archive_bytes(self.paths.root,b);self.assertEqual(digest(a),digest(b))
        with tarfile.open(a) as tar:
            names=tar.getnames();self.assertFalse(any("models" in n or ".env" in n for n in names))

    def test_save_change_detection_and_last_good(self):
        self.fixture();first=artifacts.save(self.paths)
        unchanged=artifacts.save(self.paths);self.assertTrue(first["changed"]);self.assertFalse(unchanged["changed"])
        (self.paths.comfy/"data").write_bytes(b"changed")
        second=artifacts.save(self.paths)
        self.assertEqual(read_json(self.paths.artifacts/"last-good.json")["sha256"],first["manifest"]["sha256"])
        self.assertNotEqual(second["manifest"]["sha256"],first["manifest"]["sha256"])
        self.assertTrue(artifacts.verify_pointer(self.paths,first["manifest"]).exists())

    def test_registered_node_state_is_part_of_change_detection(self):
        self.fixture();first=artifacts.save(self.paths)
        atomic_json(self.paths.state/"nodes.json",{"node":{"ver":"1.0.0"}})
        second=artifacts.save(self.paths)
        self.assertTrue(second["changed"])
        self.assertEqual(second["manifest"]["node_manifest"],{"node":{"ver":"1.0.0"}})

    def test_restore_publishes_complete_runtime_and_registered_node_state(self):
        self.fixture();atomic_json(self.paths.state/"nodes.json",{"node":{"ver":"1.0.0"}});saved=artifacts.save(self.paths)
        for name in artifacts.COMPONENTS: __import__("shutil").rmtree(self.paths.root/name)
        (self.paths.state/"nodes.json").unlink()
        def fake_link(path,target):path.write_bytes(b"fake interpreter")
        checked=[]
        def validate(root):
            checked.append(root)
            self.assertEqual((root/"ComfyUI/data").read_bytes(),b"ComfyUI")
        with patch.object(Path,"symlink_to",fake_link):self.assertTrue(artifacts.restore(self.paths,validate))
        self.assertEqual(len(checked),1);self.assertTrue(all((self.paths.root/n).is_dir() for n in artifacts.COMPONENTS))
        self.assertEqual(read_json(self.paths.state/"nodes.json"),saved["manifest"]["node_manifest"])

    def test_restore_validation_failure_leaves_no_partial_install(self):
        self.fixture();saved=artifacts.save(self.paths)
        for name in artifacts.COMPONENTS: __import__("shutil").rmtree(self.paths.root/name)
        def fake_link(path,target):path.write_bytes(b"fake interpreter")
        with patch.object(Path,"symlink_to",fake_link):
            self.assertFault("incompatible",artifacts.restore,self.paths,lambda root:(_ for _ in ()).throw(Fault("incompatible","Synthetic failure")))
        self.assertFalse(any((self.paths.root/n).exists() for n in artifacts.COMPONENTS))
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),saved["manifest"])

    def test_corrupt_copy_keeps_selected_manifest(self):
        self.fixture();first=artifacts.save(self.paths)["manifest"]
        (self.paths.comfy/"data").write_bytes(b"changed")
        def corrupt(source,destination,*args): Path(destination).write_bytes(b"corrupt")
        with patch("remote.artifacts.shutil.copyfile",side_effect=corrupt): self.assertFault("artifact_corrupt",artifacts.save,self.paths)
        self.assertEqual(read_json(self.paths.artifacts/"selected.json"),first)

    def test_corrupt_archive_fails_verification(self):
        self.fixture();manifest=artifacts.save(self.paths)["manifest"]
        (self.paths.artifacts/manifest["archive"]).write_bytes(b"corrupt")
        self.assertFault("artifact_corrupt",artifacts.verify_pointer,self.paths,manifest)

    def test_compatibility_mismatch_does_not_restore(self):
        self.fixture();manifest=artifacts.save(self.paths)["manifest"]
        for name in artifacts.COMPONENTS: __import__("shutil").rmtree(self.paths.root/name)
        with patch("remote.artifacts.compatibility",return_value={"different":True}): self.assertFalse(artifacts.restore(self.paths))

    def test_required_inherited_package_drift_forces_clean_install(self):
        self.assertFalse(artifacts.inherited_compatible({"package-that-does-not-exist-ecc2":"1.0"}))
        self.assertTrue(artifacts.inherited_compatible({}))

    def test_extract_rejects_traversal_links_and_devices(self):
        for name,kind in (("../escape",tarfile.REGTYPE),("ComfyUI/../escape",tarfile.REGTYPE),("ComfyUI/link",tarfile.SYMTYPE),("ComfyUI/device",tarfile.CHRTYPE),("C:/Windows/x",tarfile.REGTYPE)):
            data=io.BytesIO()
            with tarfile.open(fileobj=data,mode="w") as tar:
                info=tarfile.TarInfo(name);info.type=kind;info.size=1 if kind==tarfile.REGTYPE else 0
                tar.addfile(info,io.BytesIO(b"x") if info.size else None)
            data.seek(0)
            with tarfile.open(fileobj=data) as tar:self.assertFault("unsafe_artifact",artifacts.safe_extract,tar,self.paths.root/"extract",1)

    def test_git_credentials_block_preservation(self):
        self.fixture();git=self.paths.comfy/".git";git.mkdir();(git/"config").write_text('[remote "origin"]\nurl = https://user:pass@github.com/org/repo.git\n')
        self.assertFault("unsafe_artifact",artifacts.save,self.paths)


class CacheTests(Temporary):
    def test_standard_paths_copy_invalidate_without_loader_patch(self):
        model_paths(self.paths);p=self.paths.models/"loras/a.bin";p.write_bytes(b"model")
        c=Cache(self.paths,idle=lambda:True);c.prepare({"models":["loras/a.bin"]});c.worker.join(10)
        self.assertFalse(c.snapshot()["busy"]);self.assertEqual(c.snapshot()["status"],"complete")
        self.assertEqual((self.paths.cache/"loras/a.bin").read_bytes(),b"model")
        options=read_json(self.paths.root/"extra-model-paths.yaml");self.assertTrue(options["easy_ssd"]["is_default"])
        p.write_bytes(b"changed");c.reconcile();self.assertFalse((self.paths.cache/"loras/a.bin").exists())

    def test_disk_budget_and_queue_protect_cache(self):
        model_paths(self.paths);p=self.paths.models/"loras/a.bin";p.write_bytes(b"model");c=Cache(self.paths,idle=lambda:False)
        with patch("remote.cache.shutil.disk_usage",return_value=type("Space",(),{"free":1})()):self.assertFault("disk_full",c.prepare,{"models":["loras/a.bin"]})
        self.assertFault("cache_busy",c.clear)

    def test_cancel_waiting_cache_preserves_drive_original(self):
        model_paths(self.paths);p=self.paths.models/"loras/a.bin";p.write_bytes(b"model");c=Cache(self.paths,idle=lambda:False)
        c.prepare({"models":["loras/a.bin"]});c.cancel();c.worker.join(5)
        self.assertEqual(c.snapshot()["status"],"cancelled");self.assertEqual(p.read_bytes(),b"model")


class ManagerTests(Temporary):
    def test_version_and_expected_installed_state_block_change(self):
        n=Nodes(self.paths,"4.2.2",call=lambda *args:"new",idle=lambda:True)
        self.assertFault("manager_version",n.list)
        state={"item":{"ver":"1.0.0"}}
        def call(route,*args):return "4.2.2" if route=="/v2/manager/version" else state
        n=Nodes(self.paths,"4.2.2",call=call,idle=lambda:True)
        self.assertFault("node_conflict",n.operate,"update",{"id":"item","version":"2.0.0","expected_version":"0.9.0"})
        self.assertFalse(n.busy)

    def test_install_postcondition_and_manifest(self):
        state={};called=[]
        def call(route,data=None):
            called.append(route)
            if route=="/v2/manager/version":return "V4.2.2"
            if route=="/v2/customnode/installed":return dict(state)
            if route.endswith("/status"):return {"is_processing":False,"total_count":0,"done_count":10,"in_progress_count":0,"pending_count":0}
            if route.endswith("/task"):
                params=data["params"];state[params["id"]]={"ver":params["selected_version"],"enabled":True}
            if "history?ui_id=" in route:return {"history":{route.split("=")[-1]:{"status":{"completed":True,"status_str":"success"}}}}
            return {}
        registry=Registry("0.39.0","1.55.14",request=lambda *args:[{"node_id":"item","version":"1.0.0","status":"NodeVersionStatusActive","deprecated":False}])
        n=Nodes(self.paths,"4.2.2",call=call,idle=lambda:True,registry=registry)
        result=n.operate("install",{"id":"item","version":"1.0.0"})
        self.assertTrue(result["verified"]);self.assertTrue(result["requires_restart"])
        self.assertEqual(read_json(self.paths.state/"nodes.json")["item"]["ver"],"1.0.0")


class CompanionTests(Temporary):
    def service(self):
        s=Service(self.paths,config());self.addCleanup(s.downloads.executor.shutdown,True);return s

    def test_stop_outputs_gate_and_failed_gate_resume(self):
        s=self.service();(s.outputs.root/"image.png").write_bytes(b"image")
        self.assertFault("outputs_pending",s.route,"POST","/stop/prepare",{}, {})
        self.assertTrue((self.paths.state/"stopping").exists())
        m=s.route("POST","/outputs/prepare",{},{});s.route("POST","/outputs/ack",{},m)
        self.assertTrue(s.route("POST","/stop/prepare",{}, {})["safe_to_stop"])
        s.route("POST","/outputs/resume",{},{});self.assertFalse((self.paths.state/"stopping").exists())

    def test_download_background_blocks_stop(self):
        s=self.service();s.downloads.jobs=[{"status":"running"}]
        self.assertFault("runtime_busy",s.route,"POST","/stop/prepare",{}, {})

    def test_workflow_import_conflict_and_traversal(self):
        s=self.service();data={"workflow":{"nodes":[]},"name":"a.json"}
        self.assertTrue(s.route("POST","/workflows/import",{},data)["imported"])
        self.assertTrue(s.route("POST","/workflows/import",{},data)["imported"])
        self.assertFault("workflow_conflict",s.route,"POST","/workflows/import",{},dict(data,workflow={"nodes":[1]}))
        self.assertFault("invalid_workflow",s.route,"POST","/workflows/import",{},dict(data,name="../a.json"))

    def test_workflow_model_choices_respect_loader_category(self):
        s=self.service();s.cpu=False
        checkpoint=self.paths.models/"checkpoints/same.safetensors";checkpoint.parent.mkdir();checkpoint.write_bytes(b"checkpoint")
        info={"VAELoader":{"input":{"required":{"vae_name":[["valid-vae.safetensors"]]}}}}
        graph={"1":{"class_type":"VAELoader","inputs":{"vae_name":"same.safetensors"}}}
        with patch("remote.companion.comfy_json",return_value=info):result=s.workflow("check",{"workflow":graph})
        self.assertFalse(result["valid"]);self.assertEqual(result["missing_models"],["same.safetensors"])
        graph["1"]["inputs"]["vae_name"]="valid-vae.safetensors"
        with patch("remote.companion.comfy_json",return_value=info):self.assertTrue(s.workflow("check",{"workflow":graph})["valid"])

    def test_workflow_unknown_widget_mapping_is_unresolved(self):
        s=self.service();s.cpu=False
        info={"DynamicNode":{"input":{"required":{"seed":["INT"],"model":[["valid.bin"]]}}}}
        graph={"nodes":[{"id":1,"type":"DynamicNode","widgets_values":[7,"randomize","valid.bin"]}]}
        with patch("remote.companion.comfy_json",return_value=info):result=s.workflow("check",{"workflow":graph})
        self.assertFalse(result["valid"]);self.assertEqual(result["unresolved"][0]["reason"],"ui_widget_mapping")

    def test_library_pagination_and_null_unknown_metrics(self):
        s=self.service();folder=self.paths.models/"loras";folder.mkdir()
        for i in range(120):(folder/f"{i:03}.bin").write_bytes(b"model")
        result=s.route("GET","/models",{"offset":["10"],"limit":["7"]},{})
        self.assertEqual(result["total"],120);self.assertEqual(len(result["items"]),7);self.assertEqual(result["items"][0]["name"],"010.bin")
        snapshot=s.snapshot();self.assertIsNone(snapshot["metrics"]["cpu_percent"]);self.assertEqual(len(snapshot["models"]["items"]),50)

    def test_http_auth_identity_and_json_body_limit(self):
        s=self.service();server=Server(("127.0.0.1",0),s)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f"http://127.0.0.1:{server.server_port}"
        headers={"Authorization":"Bearer "+config()["bearer_token"],"X-Easy-Account":"account","X-Easy-Session":"session","X-Easy-Nonce":"nonce"}
        with self.assertRaises(HTTPError) as err:urlopen(url+"/health")
        self.assertEqual(err.exception.code,401)
        err.exception.close()
        with urlopen(Request(url+"/health",headers=headers)) as response:result=json.load(response)
        self.assertEqual(result["result"]["session_id"],"session");self.assertNotIn("bearer_token",json.dumps(result))
        changed=dict(headers,**{"X-Easy-Nonce":"wrong"})
        with self.assertRaises(HTTPError) as err:urlopen(Request(url+"/health",headers=changed))
        self.assertEqual(err.exception.code,409)
        err.exception.close()
        with self.assertRaises(HTTPError) as err:urlopen(Request(url+"/outputs/resume",data=b"",headers=dict(headers,**{"Content-Length":str(1024*1024+1)})),timeout=5)
        self.assertEqual(json.load(err.exception)["error"]["code"],"request_too_large")
        err.exception.close()

    def test_route_payload_limits_accept_large_workflow_and_output_ack_only(self):
        self.assertEqual(body_limit("/workflows/check"),16*1024*1024)
        self.assertEqual(body_limit("/workflows/import"),16*1024*1024)
        self.assertEqual(body_limit("/outputs/ack"),16*1024*1024)
        self.assertEqual(body_limit("/downloads"),1024*1024)
        s=self.service();server=Server(("127.0.0.1",0),s)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f"http://127.0.0.1:{server.server_port}"
        headers={"Authorization":"Bearer "+config()["bearer_token"],"X-Easy-Account":"account","X-Easy-Session":"session","X-Easy-Nonce":"nonce","Content-Type":"application/json"}
        graph={"workflow":{"nodes":[],"notes":"x"*(1024*1024+1)},"filename":"large.json"}
        payload=json.dumps(graph,ensure_ascii=False,separators=(",",":")).encode()
        server.large_bodies.acquire()
        try:
            with self.assertRaises(HTTPError) as err:urlopen(Request(url+"/workflows/import",data=b"",headers=dict(headers,**{"Content-Length":str(len(payload))})),timeout=5)
            self.assertEqual(json.load(err.exception)["error"]["code"],"runtime_busy");err.exception.close()
        finally:server.large_bodies.release()
        with urlopen(Request(url+"/workflows/import",data=payload,headers=headers),timeout=10) as response:self.assertTrue(json.load(response)["result"]["imported"])
        acknowledged=[{"relative_path":f"{i:05}_"+"a"*200+".png","size":1,"sha256":"a"*64} for i in range(5000)]
        manifest={"manifest_id":"synthetic-large-manifest","files":[dict(row,remote_path=str(s.outputs.root/row["relative_path"])) for row in acknowledged]}
        atomic_json(self.paths.state/"output-manifest.json",manifest);(self.paths.state/"stopping").touch()
        payload=json.dumps({"manifest_id":manifest["manifest_id"],"files":acknowledged},ensure_ascii=False,separators=(",",":")).encode()
        self.assertGreater(len(payload),1024*1024)
        with patch.object(s.outputs,"files",return_value=manifest["files"]),patch("remote.outputs.inside",return_value=self.paths.state/"stopping"):
            with urlopen(Request(url+"/outputs/ack",data=payload,headers=headers),timeout=10) as response:self.assertTrue(json.load(response)["result"]["verified"])
        for route in ("/downloads","/workflows/import","/outputs/ack"):
            declared=body_limit(route)+1
            with self.assertRaises(HTTPError) as err:urlopen(Request(url+route,data=b"",headers=dict(headers,**{"Content-Length":str(declared)})),timeout=5)
            self.assertEqual(json.load(err.exception)["error"]["code"],"request_too_large");err.exception.close()

    def test_bootstrap_identity_mismatch_and_no_legacy_operations(self):
        atomic_json(self.paths.state/"session.json",config())
        self.assertFault("identity_mismatch",bootstrap.dispatch,{"action":"stop","account_id":"other","session_id":"session","nonce":"nonce"},self.paths)
        self.assertFault("invalid_action",bootstrap.dispatch,{"action":"unassign"},self.paths)


class UnpreparedStopTests(Temporary):
    def dispatch(self, **change):
        request=dict(config(), action="stop_unprepared", **change)
        return bootstrap.dispatch(request, self.paths)

    def setUp(self):
        super().setUp()
        atomic_json(self.paths.state/"session.json", config())
        self.patch=patch("remote.bootstrap.listening", return_value=False)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_empty_incomplete_runtime_can_stop_without_reading_or_writing_artifacts(self):
        self.paths.artifacts.mkdir(); prior=self.paths.artifacts/"selected.json"; prior.write_bytes(b"prior preserved untouched")
        with patch("remote.bootstrap.artifacts.save", side_effect=AssertionError("No artifact mutation allowed")):
            result=self.dispatch()
        self.assertTrue(result["safe_to_stop"]); self.assertTrue(result["unprepared"])
        self.assertFalse(result["runtime_required"]); self.assertEqual(result["pending_outputs"],0)
        self.assertEqual(prior.read_bytes(), b"prior preserved untouched")
        self.assertFalse((self.paths.state/"operation-status.json").exists())

    def test_installed_or_service_record_cannot_bypass_normal_stop(self):
        atomic_json(self.paths.state/"installed.json",{})
        self.assertFault("runtime_already_prepared", self.dispatch)
        (self.paths.state/"installed.json").unlink()
        atomic_json(self.paths.state/"comfy-process.json",{"pid":999})
        self.assertFault("process_unknown", self.dispatch)

    def test_active_port_and_wrong_identity_are_rejected(self):
        with patch("remote.bootstrap.listening",return_value=True): self.assertFault("remote_port_conflict",self.dispatch)
        for key in ("account_id", "session_id", "nonce", "gpu", "output_mode", "bearer_token"):
            self.assertFault("identity_mismatch",self.dispatch,**{key:"other"})

    def test_any_active_or_inactive_output_preserves_vm(self):
        for relative in ("output-pc/session/result.png", "output-pc/older/result.png", "output/result.png", "output-drive/result.png"):
            file=self.paths.root/relative; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(b"original")
            self.assertFault("outputs_pending",self.dispatch); self.assertEqual(file.read_bytes(),b"original"); file.unlink()
        file=self.paths.drive/"output/a.part"; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(b"partial")
        self.assertFault("outputs_pending",self.dispatch)

    def test_output_root_file_or_unreadable_directory_never_counts_as_empty(self):
        (self.paths.root/"output").write_bytes(b"original")
        self.assertFault("outputs_pending",self.dispatch)
        (self.paths.root/"output").unlink(); (self.paths.root/"output").mkdir()
        def unreadable(path, **kwargs):
            kwargs["onerror"](PermissionError("synthetic"))
            return iter(())
        with patch("remote.bootstrap.os.walk",side_effect=unreadable): self.assertFault("unsafe_output",self.dispatch)

    def test_stat_failure_is_not_treated_as_missing_or_empty(self):
        original=Path.lstat
        for unsafe in (self.paths.state/"installed.json",self.paths.state/"comfy-process.json",self.paths.root/"output-pc"):
            def denied(path, *args, **kwargs):
                if path==unsafe: raise PermissionError("synthetic")
                return original(path,*args,**kwargs)
            with patch.object(Path,"lstat",denied): self.assertFault("invalid_state",self.dispatch)


if __name__=="__main__":unittest.main()

