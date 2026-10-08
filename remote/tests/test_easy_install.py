import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from remote import bootstrap
from remote.common import Fault, Paths, atomic_json, digest, read_json


def archive(files):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w") as zipped:
        for name,data in files.items(): zipped.writestr(name,data)
    return buffer.getvalue()


class EasyInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.paths=Paths(Path(self.temp.name)/"runtime",Path(self.temp.name)/"Drive/New")
        self.paths.root.mkdir(); self.paths.drive.mkdir(parents=True)
        self.manifest=copy.deepcopy(bootstrap.MANIFEST)
        self.patch=patch.object(bootstrap,"MANIFEST",self.manifest); self.patch.start(); self.addCleanup(self.patch.stop)

    def fault(self,code,fn,*args):
        with self.assertRaises(Fault) as caught:fn(*args)
        self.assertEqual(caught.exception.code,code)

    def package(self,**values):
        return dict(id="test-pack",aliases=["test-pack","Test-Pack"],folder="Test-Pack",source="cnr",
                    repository="https://github.com/example/Test-Pack",version="1.2.3",requirements=["numpy>=1.25"])|values

    def test_pinned_sources_are_verified_cached_and_only_parsed_never_executed(self):
        sources={"installer":b'get_node https://github.com/example/Test-Pack Test-Pack\nrm -rf something\n',
                 "helper":archive({"ignored.sh":"do not execute"}),"license":b"MIT fixture"}
        for key,payload in sources.items():
            self.manifest["easy_install"][key]["sha256"]=hashlib.sha256(payload).hexdigest()
        self.manifest["easy_install"]["helper"]["bytes"]=len(sources["helper"])
        def reviewed(url,checksum,limit):
            return next(payload for payload in sources.values() if hashlib.sha256(payload).hexdigest()==checksum)
        with patch.object(bootstrap,"download_reviewed",side_effect=reviewed) as download,patch.object(bootstrap,"run",side_effect=AssertionError("No installer executed")):
            files,catalog=bootstrap.easy_install_sources(self.paths)
            self.assertEqual(catalog,{"Test-Pack":"https://github.com/example/Test-Pack"})
            self.assertEqual(download.call_count,3)
            bootstrap.easy_install_sources(self.paths); self.assertEqual(download.call_count,3)
        self.assertEqual(read_json(self.paths.root/"locks/easy-install/provenance.json")["revision"],self.manifest["easy_install"]["revision"])

    def test_resource_hash_and_size_reject_modified_upstream_payload(self):
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        with patch.object(bootstrap,"urlopen",return_value=Response(b"changed")):
            self.fault("integrity_failed",bootstrap.download_reviewed,"https://example.invalid/fixed","0"*64,20)
        with patch.object(bootstrap,"urlopen",return_value=Response(b"x"*21)):
            self.fault("integrity_failed",bootstrap.download_reviewed,"https://example.invalid/fixed",hashlib.sha256(b"x"*21).hexdigest(),20)
        self.fault("unlocked_dependency",bootstrap.download_reviewed,"https://user:pass@example.invalid/fixed","a"*64,20)

    def test_templates_copy_only_json_without_replacing_user_edits(self):
        prefix="ComfyUI-Easy-Install/ComfyUI/user/default/workflows/"
        helper=self.paths.root/"helper.zip"
        helper.write_bytes(archive({prefix+"Getting Started/example.json":'{"nodes":[]}',
                                   "ComfyUI-Easy-Install/ComfyUI/user/user.css":"ignored", "install.py":"ignored"}))
        result=bootstrap.install_easy_templates(self.paths,helper)
        self.assertEqual(result["copied"],1)
        target=self.paths.drive/"user/default/workflows/Easy Install/Getting Started/example.json"
        target.write_text('{"user":"custom"}',encoding="utf-8")
        self.assertEqual(bootstrap.install_easy_templates(self.paths,helper)["existing"],1)
        self.assertEqual(json.loads(target.read_text()),{"user":"custom"})
        self.assertFalse((self.paths.drive/"user/user.css").exists()); self.assertFalse((self.paths.root/"install.py").exists())

    def test_template_traversal_duplicate_invalid_json_and_size_are_rejected(self):
        prefix="ComfyUI-Easy-Install/ComfyUI/user/default/workflows/"
        helper=self.paths.root/"helper.zip"
        helper.write_bytes(archive({prefix+"../escape.json":'{"nodes":[]}'}))
        self.fault("invalid_path",bootstrap.install_easy_templates,self.paths,helper)
        helper.write_bytes(archive({prefix+"invalid.json":"[]"}))
        self.fault("invalid_workflow",bootstrap.install_easy_templates,self.paths,helper)
        helper.write_bytes(archive({prefix+"invalid.json":'{"value":NaN}'}))
        self.fault("easy_install_unsafe",bootstrap.install_easy_templates,self.paths,helper)

    def test_failed_template_write_removes_only_owned_file_and_corrupt_existing_is_preserved(self):
        prefix="ComfyUI-Easy-Install/ComfyUI/user/default/workflows/"
        helper=self.paths.root/"helper.zip";helper.write_bytes(archive({prefix+"example.json":'{"nodes":[]}'}))
        target=self.paths.drive/"user/default/workflows/Easy Install/example.json"
        with patch.object(bootstrap.os,"fsync",side_effect=OSError("Disk full")):
            self.fault("template_write_failed",bootstrap.install_easy_templates,self.paths,helper)
        self.assertFalse(target.exists())
        target.write_text('{"broken":',encoding="utf-8")
        self.fault("template_conflict",bootstrap.install_easy_templates,self.paths,helper)
        self.assertEqual(target.read_text(),'{"broken":')

    def test_failed_template_write_cannot_delete_another_writers_replacement(self):
        prefix="ComfyUI-Easy-Install/ComfyUI/user/default/workflows/"
        helper=self.paths.root/"helper.zip";helper.write_bytes(archive({prefix+"example.json":'{"nodes":[]}'}))
        target=self.paths.drive/"user/default/workflows/Easy Install/example.json"
        # Open handles prevent replacing this fixture on Windows; model the distinct inode identity.
        with patch.object(bootstrap.os,"fsync",side_effect=OSError("Disk full")),patch.object(Path,"lstat",return_value=type('Identity',(),{'st_ino':-1,'st_dev':-1,'st_mode':0o100600})()):
            self.fault("template_write_failed",bootstrap.install_easy_templates,self.paths,helper)
        self.assertEqual(json.loads(target.read_text()),{"nodes":[]})

    def test_manifest_keeps_broad_pinned_catalog_with_explicit_partial_and_blocked_features(self):
        packages=self.manifest['packages']
        self.assertEqual(len(packages),23)
        enabled=[p for p in packages if p['status']=='enabled']
        self.assertEqual(len(enabled),20)
        self.assertEqual({p['id'] for p in packages if p['status']=='unsupported'},{'comfyui-rmbg','qwen3-tts-comfyui','comfyui-easy-sam3'})
        for p in packages:
            self.assertTrue(p['original_requirements'] or not p['requirements'])
            self.assertRegex(p.get('revision',p.get('artifact',{}).get('sha256','')),r'^[0-9a-f]{40}$|^[0-9a-f]{64}$')
        blocked={'fvcore','iopath','groundingdino-py','sox','mediapipe','albumentations'}
        from packaging.requirements import Requirement
        names={Requirement(r).name.lower().replace('_','-') for p in enabled for r in p['requirements']}
        self.assertFalse(names&blocked)
        self.assertEqual({n for n in names if n.startswith('opencv')},{'opencv-contrib-python-headless'})
        self.assertEqual({n for n in names if n.startswith('onnxruntime')},{'onnxruntime'})
        aux=next(p for p in enabled if p['id']=='comfyui_controlnet_aux')
        self.assertTrue(any('Partial package' in text for text in aux['limitations']))
        self.assertIn('omegaconf==2.4.0rc1',self.manifest['easy_install']['requirements'])

    def test_resolved_cohort_is_exact_deduplicated_non_cuda_and_does_not_affect_mcp(self):
        pins=bootstrap.runtime_constraints('venv')
        self.assertEqual(len(pins),150)
        self.assertIn('comfyui-frontend-package==1.55.14',pins)
        self.assertIn('comfyui-manager==4.2.2',pins)
        self.assertEqual(bootstrap.runtime_constraints('mcp-venv'),[])
        self.manifest['easy_install']['resolved_constraints']=['NumPy==2.3.3','numpy==2.3.3']
        self.assertEqual(bootstrap.runtime_constraints('venv'),['numpy==2.3.3'])
        self.manifest['easy_install']['resolved_constraints'].append('numpy==2.3.2')
        self.fault('unlocked_dependency',bootstrap.runtime_constraints,'venv')
        for pin in ['torch==2.11.0','TorchVision==0.26.0','torchaudio==2.11.0','triton==3.6.0','nvidia-cublas==13.1.0.3']:
            self.manifest['easy_install']['resolved_constraints']=[pin]
            self.fault('torch_changed',bootstrap.runtime_constraints,'venv')
        for pin in ['numpy>=2','numpy==2.*','numpy @ https://example.invalid/pkg.whl','numpy==2.3.3; python_version>="3.13"']:
            self.manifest['easy_install']['resolved_constraints']=[pin]
            self.fault('unlocked_dependency',bootstrap.runtime_constraints,'venv')

    def test_resolver_cannot_add_new_cuda_package_even_when_not_preinstalled(self):
        self.paths.root.joinpath('locks').mkdir()
        atomic_json(self.paths.root/'locks/venv-resolution.json',{'install':[{'metadata':{'name':'nvidia-new-runtime'}}]})
        with patch.object(bootstrap,'ensure_environment'),patch.object(bootstrap,'run',return_value='') as run:
            self.fault('torch_changed',bootstrap.install_locked,self.paths,'venv','numpy==2.3.3\n',{'torch':'2.11.0+cu130'})
        self.assertEqual(run.call_count,1)  # dry-run only, never wheel installation
        constraints=(self.paths.root/'locks/venv-constraints.txt').read_text()
        self.assertIn('torch==2.11.0+cu130',constraints)
        self.assertNotIn('torch==2.11.0\n',constraints)
        self.assertFalse((self.paths.root/'locks/venv.lock').exists())

    def test_cnr_install_verifies_pin_and_metadata_and_generates_tracking_without_hooks(self):
        payload=archive({"__init__.py":"NODE_CLASS_MAPPINGS={}","pyproject.toml":'[project]\nname="Test-Pack"\nversion="1.2.3"\n',"requirements.txt":"numpy>=1.25\n","install.py":"raise RuntimeError('never execute')"})
        package=self.package(artifact={"url":"https://cdn.comfy.org/reviewed.zip","sha256":hashlib.sha256(payload).hexdigest()})
        self.manifest["packages"]=[package]
        with patch.object(bootstrap,"download_reviewed",return_value=payload),patch.object(bootstrap,"run",side_effect=AssertionError("No node hooks executed")):
            requirements=bootstrap.install_easy_packages(self.paths,{"Test-Pack":package["repository"]})
        self.assertEqual(requirements,["numpy>=1.25"])
        target=self.paths.comfy/"custom_nodes/Test-Pack"
        self.assertIn("install.py",(target/".tracking").read_text())
        self.assertTrue(bootstrap.verify_cnr_metadata(target,package))
        bootstrap.verify_easy_packages(self.paths.root)
        (target/"__init__.py").write_text("modified",encoding="utf-8")
        self.fault("base_node_changed",bootstrap.verify_easy_packages,self.paths.root)

    def test_package_zip_traversal_symlinks_and_wrong_package_metadata_are_rejected(self):
        target=self.paths.root/"extract"; target.mkdir()
        self.fault("invalid_path",bootstrap.extract_easy_package,archive({"../outside":"bad"}),target)
        self.fault("easy_install_unsafe",bootstrap.extract_easy_package,archive({".git/HEAD":"bad"}),target)
        safe=archive({"__init__.py":"x","pyproject.toml":'[project]\nname="wrong"\nversion="1.2.3"\n'})
        package=self.package(artifact={"url":"https://cdn.comfy.org/pin.zip","sha256":hashlib.sha256(safe).hexdigest()}); self.manifest["packages"]=[package]
        with patch.object(bootstrap,"download_reviewed",return_value=safe):
            self.fault("base_node_changed",bootstrap.install_easy_packages,self.paths,{"Test-Pack":package["repository"]})
        self.assertFalse((self.paths.comfy/"custom_nodes/Test-Pack").exists())

    def test_unreviewed_catalog_or_existing_folder_is_not_replaced(self):
        package=self.package(artifact={"url":"https://cdn.comfy.org/pin.zip","sha256":"a"*64}); self.manifest["packages"]=[package]
        self.fault("unlocked_dependency",bootstrap.install_easy_packages,self.paths,{"Test-Pack":"https://github.com/other/repo"})
        target=self.paths.comfy/"custom_nodes/Test-Pack"; target.mkdir(parents=True); (target/"original").write_bytes(b"original")
        self.fault("base_node_conflict",bootstrap.install_easy_packages,self.paths,{"Test-Pack":package["repository"]})
        self.assertEqual((target/"original").read_bytes(),b"original")

    def test_known_unsupported_packages_are_explicit_and_do_not_install_impossible_dependency(self):
        self.manifest["packages"]=[self.package(status="unsupported",reason="No binary wheel",requirements=["groundingdino-py==0.4.0"])]
        with patch.object(bootstrap,"download_reviewed",side_effect=AssertionError("Unsupported package not installed")):
            self.assertEqual(bootstrap.install_easy_packages(self.paths,{}),[])

    def test_protected_requirements_validate_transitive_constraints_and_reject_urls(self):
        protected={"torch":"2.11.0+cu130","torchvision":"0.26.0+cu130","triton":"3.6.0","nvidia-cublas-cu13":"13.1.0"}
        result=bootstrap.protected_requirements(["torch>=2.4", "torchvision", "triton>=3", "numpy>=1.25", "nvidia-cublas-cu13>=13"],protected)
        self.assertEqual(result,["numpy>=1.25"])
        for line in ("torch==2.5.0", "torch<2.0", "nvidia-cublas-cu12", "triton==2.0"):
            self.fault("torch_changed",bootstrap.protected_requirements,[line],protected)
        for line in ("--extra-index-url https://elsewhere.invalid", "pkg @ https://elsewhere.invalid/pkg.whl", "git+https://github.com/example/repo"):
            self.fault("unlocked_dependency",bootstrap.protected_requirements,[line],protected)

    @unittest.skipUnless(shutil.which("git"),"Git required for source identity regression")
    def test_git_fallback_accepts_only_exact_origin_head_and_clean_source(self):
        package=self.package(source="git",revision="0"*40); self.manifest["packages"]=[package]
        target=self.paths.comfy/"custom_nodes/Test-Pack"; target.mkdir(parents=True)
        def git(*args):return subprocess.run(["git","-C",str(target),*args],check=True,capture_output=True,text=True).stdout.strip()
        git("init"); git("remote","add","origin",package["repository"])
        (target/"__init__.py").write_text("NODE_CLASS_MAPPINGS={}",encoding="utf-8")
        git("add","__init__.py"); git("-c","user.name=Local test","-c","user.email=test@example.invalid","commit","-m","fixture")
        package["revision"]=git("rev-parse","HEAD")
        self.assertTrue(bootstrap.verify_easy_node(self.paths.root,"test-pack",{"ver":"nightly"}))
        self.assertFalse(bootstrap.verify_easy_node(self.paths.root,"arbitrary",{"ver":"nightly"}))
        (target/"new_untracked.py").write_text("changed",encoding="utf-8")
        self.assertFalse(bootstrap.verify_easy_node(self.paths.root,"test-pack",{"ver":"nightly"}))
        for file in target.rglob("*"):
            if file.is_file():file.chmod(0o600)


if __name__=="__main__":unittest.main()
