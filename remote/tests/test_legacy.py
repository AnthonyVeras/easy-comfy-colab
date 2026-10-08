import json
import os
from pathlib import Path
import tempfile
import errno
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote.common import Fault, Paths, atomic_json, digest, read_json
from remote.companion import Service
from remote.legacy import Legacy, WORKFLOW_LIMIT, publish_no_replace


class LegacyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.paths = Paths(Path(temp.name)/"runtime", Path(temp.name)/"Drive/NewProduct")
        self.paths.state.mkdir(parents=True); self.paths.drive.mkdir(parents=True)
        self.invalidated=[]; self.legacy=Legacy(self.paths,self.invalidated.append)
        self.config=dict(account_id="a",session_id="s",nonce="n",bearer_token="b"*48,output_mode="pc",gpu="CPU")

    def source(self, relative="models/checkpoints/model.safetensors", payload=b"selected model bytes"):
        file=self.legacy.root/relative; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(payload)
        return file

    def request(self, **values):
        return dict(kind="model",category="checkpoints",relative_path="model.safetensors") | values

    def fault(self, code, fn, *args):
        with self.assertRaises(Fault) as caught: fn(*args)
        self.assertEqual(caught.exception.code,code)

    def test_model_copy_preserves_source_and_records_hash_identity(self):
        source=self.source(); result=self.legacy.import_file(self.request())
        target=self.paths.models/"checkpoints/model.safetensors"
        self.assertEqual(target.read_bytes(),source.read_bytes()); self.assertEqual(result["sha256"],digest(source))
        self.assertTrue(result["source_preserved"]); self.assertFalse(result["unchanged"])
        self.assertEqual(result["relative_path"],"checkpoints/model.safetensors")
        self.assertEqual(self.invalidated,["checkpoints/model.safetensors"])
        self.assertEqual(read_json(self.paths.drive/"model-identities.json")[result["relative_path"]]["sha256"],digest(source))
        self.assertFalse(list(target.parent.glob(".legacy-*")))

    def test_repeat_identical_is_unchanged_and_conflict_does_not_overwrite(self):
        original=self.source(); self.legacy.import_file(self.request())
        self.assertTrue(self.legacy.import_file(self.request())["unchanged"]); self.assertEqual(len(self.invalidated),1)
        target=self.paths.models/"checkpoints/model.safetensors"; target.write_bytes(b"different content")
        self.fault("legacy_conflict",self.legacy.import_file,self.request())
        self.assertEqual(target.read_bytes(),b"different content"); self.assertEqual(original.read_bytes(),b"selected model bytes")

    def test_rename_preserves_extension_and_source_nested_path(self):
        source=self.source("models/checkpoints/nested/original.safetensors")
        result=self.legacy.import_file(self.request(relative_path="nested/original.safetensors",destination_name="chosen.SAFETENSORS"))
        self.assertEqual(Path(result["remote_path"]).read_bytes(),source.read_bytes())
        self.fault("invalid_path",self.legacy.import_file,self.request(relative_path="nested/original.safetensors",destination_name="chosen.gguf"))

    def test_invalid_relative_destination_and_extra_roots_are_rejected(self):
        self.source()
        for relative in ("../outside.safetensors","/content/model.safetensors","C:/model.safetensors","nested\\file.safetensors",None,"a//model.safetensors"):
            self.fault("invalid_path",self.legacy.import_file,self.request(relative_path=relative))
        for name in ("../x.safetensors","x/y.safetensors","x:y.safetensors","x\ny.safetensors","é"*128+".safetensors"):
            self.fault("invalid_path",self.legacy.import_file,self.request(destination_name=name))
        self.fault("invalid_model",self.legacy.import_file,self.request(category="runtime-artifacts"))
        self.fault("legacy_kind_invalid",self.legacy.import_file,self.request(kind="image"))

    def test_missing_model_requires_category_and_unsupported_suffix_is_rejected(self):
        self.fault("invalid_model",self.legacy.list,"model")
        self.fault("legacy_source_missing",self.legacy.import_file,self.request())
        self.source("models/checkpoints/runtime.tar.gz")
        self.fault("invalid_model",self.legacy.import_file,self.request(relative_path="runtime.tar.gz"))

    def test_pages_are_deterministic_bounded_and_exclude_old_runtime_or_images(self):
        for i in range(6): self.source(f"models/checkpoints/nested/model{i}.safetensors",str(i).encode())
        self.source("models/checkpoints/image.png"); self.source("models/checkpoints/runtime.tar.gz")
        first=self.legacy.list("model","checkpoints",1,2); second=self.legacy.list("model","checkpoints",1,2)
        self.assertEqual(first,second); self.assertEqual(first["total"],6)
        self.assertEqual([r["relative_path"] for r in first["items"]],["nested/model1.safetensors","nested/model2.safetensors"])
        self.assertTrue(all(row["source_category"]=="checkpoints" for row in first["items"]))

    def test_llm_aliases_are_distinct_and_map_only_to_llm_gguf(self):
        old=self.source("models/LLM/same.gguf",b"old LLM selected")
        self.source("models/llm_gguf/same.gguf",b"alternate source")
        listed=self.legacy.list("model","llm_gguf")
        self.assertEqual(listed["total"],2); self.assertEqual(len({x["id"] for x in listed["items"]}),2)
        result=self.legacy.import_file(self.request(category="llm_gguf",source_category="LLM",relative_path="same.gguf"))
        self.assertEqual(Path(result["remote_path"]).read_bytes(),old.read_bytes())
        self.assertEqual(result["relative_path"],"llm_gguf/same.gguf")
        self.fault("invalid_model",self.legacy.import_file,self.request(source_category="LLM"))
        self.fault("invalid_model",self.legacy.import_file,self.request(category="llm_gguf",source_category="../LLM"))

    def test_workflow_copy_keeps_json_bytes_and_rejects_invalid_or_large_json(self):
        raw=b'{"nodes":[], "extra":{"keep":"exact bytes"}}\n'
        source=self.source("user/default/workflows/nested/test.json",raw)
        row=self.legacy.list("workflow")["items"][0]
        result=self.legacy.import_file(dict(kind="workflow",relative_path=row["relative_path"]))
        self.assertEqual(Path(result["remote_path"]).read_bytes(),raw); self.assertEqual(source.read_bytes(),raw)
        self.fault("legacy_kind_invalid",self.legacy.import_file,dict(kind="workflow",category="checkpoints",relative_path="test.json"))
        for content in (b"[]",b"{}",b'{"value":NaN}',b"invalid json"):
            source.write_bytes(content)
            self.fault("invalid_workflow",self.legacy.import_file,dict(kind="workflow",relative_path="nested/test.json",destination_name="other.json"))
        with source.open("wb") as stream: stream.truncate(WORKFLOW_LIMIT+1)
        self.fault("workflow_too_large",self.legacy.import_file,dict(kind="workflow",relative_path="nested/test.json"))

    def test_symlinks_in_source_or_destination_ancestors_are_rejected(self):
        self.source()
        original=Path.is_symlink
        for unsafe in (self.legacy.root,self.legacy.root/"models/checkpoints",self.legacy.root/"models/checkpoints/model.safetensors",self.paths.drive,self.paths.models):
            with patch.object(Path,"is_symlink",lambda p: p==unsafe or original(p)):
                self.fault("invalid_path",self.legacy.import_file,self.request())

    def test_source_mutation_and_failed_integrity_never_publish(self):
        source=self.source(); original=digest
        def changed(file):
            if file==source: source.write_bytes(b"mutated source")
            return original(file)
        with patch("remote.legacy.digest",side_effect=changed): self.fault("legacy_source_changed",self.legacy.import_file,self.request())
        self.assertFalse((self.paths.models/"checkpoints/model.safetensors").exists())
        self.assertFalse(list((self.paths.models/"checkpoints").glob(".legacy-*")))
        source.write_bytes(b"stable original")
        with patch("remote.legacy.digest",side_effect=lambda p: "0"*64 if p.name.endswith(".partial") else original(p)):
            self.fault("integrity_failed",self.legacy.import_file,self.request())
        self.assertEqual(source.read_bytes(),b"stable original")

    def test_atomic_destination_race_preserves_other_writer(self):
        source=self.source()
        def race(partial,target):
            target.write_bytes(b"created by another writer")
            publish_no_replace(partial,target)
        with patch("remote.legacy.publish_no_replace",side_effect=race): self.fault("legacy_conflict",self.legacy.import_file,self.request())
        self.assertEqual((self.paths.models/"checkpoints/model.safetensors").read_bytes(),b"created by another writer")
        self.assertEqual(source.read_bytes(),b"selected model bytes")

    def test_publication_failure_preserves_original_without_target(self):
        source=self.source()
        with patch("remote.legacy.publish_no_replace",side_effect=Fault("atomic_publication_unavailable","safe failure")):
            self.fault("atomic_publication_unavailable",self.legacy.import_file,self.request())
        self.assertEqual(source.read_bytes(),b"selected model bytes")
        self.assertFalse((self.paths.models/"checkpoints/model.safetensors").exists())

    def test_linux_no_replace_flag_handles_race_without_fallback_overwrite(self):
        source=self.source(); target=self.paths.drive/"target.safetensors"
        class Rename:
            def __call__(self, oldfd,old,newfd,new,flags):
                self.args=(oldfd,old,newfd,new,flags)
                return -1
        rename=Rename()
        with patch("remote.legacy.os.name","posix"),patch("remote.legacy.ctypes.CDLL",return_value=SimpleNamespace(renameat2=rename)),patch("remote.legacy.ctypes.get_errno",return_value=errno.EEXIST),patch("remote.legacy.os.link") as link:
            self.fault("legacy_conflict",publish_no_replace,source,target)
        self.assertEqual(rename.args[-1],1); link.assert_not_called()
        self.assertEqual(source.read_bytes(),b"selected model bytes")

    def test_linux_unsupported_rename_and_link_fail_closed(self):
        source=self.source(); target=self.paths.drive/"target.safetensors"
        class Rename:
            def __call__(self,*args):return -1
        with patch("remote.legacy.os.name","posix"),patch("remote.legacy.ctypes.CDLL",return_value=SimpleNamespace(renameat2=Rename())),patch("remote.legacy.ctypes.get_errno",return_value=errno.EINVAL),patch("remote.legacy.os.link",side_effect=OSError(errno.EPERM,"synthetic unsupported")):
            self.fault("atomic_publication_unavailable",publish_no_replace,source,target)
        self.assertFalse(target.exists()); self.assertEqual(source.read_bytes(),b"selected model bytes")

    def test_service_allows_cpu_listing_but_blocks_import_while_busy_or_paused(self):
        self.source(); service=Service(self.paths,self.config)
        self.addCleanup(service.downloads.executor.shutdown,True)
        self.assertEqual(service.route("GET","/legacy/files",{"kind":["model"],"category":["checkpoints"]},{})["total"],1)
        service.cache.state["busy"]=True
        self.fault("runtime_busy",service.route,"POST","/legacy/import",{},self.request())
        service.cache.state["busy"]=False; (self.paths.state/"stopping").touch()
        self.fault("session_paused",service.route,"POST","/legacy/import",{},self.request())
        (self.paths.state/"stopping").unlink()
        result=service.route("POST","/legacy/import",{},self.request())
        self.assertTrue(result["imported"]); self.assertIsNone(service.catalog)


if __name__ == "__main__": unittest.main()
