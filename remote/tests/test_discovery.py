import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from remote import bootstrap
from remote.common import Fault, Paths, atomic_json, read_json
from remote.companion import CpuSample, Service
from remote.downloads import Downloads
from remote.nodes import Nodes
from remote.registry import CAPACITY, Registry, fetch


def release(key="pack", version="1.0.0", **values):
    return dict(node_id=key, version=version, status="NodeVersionStatusActive", deprecated=False, **values)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.paths = Paths(Path(temp.name)/"runtime", Path(temp.name)/"Drive/New")
        self.paths.state.mkdir(parents=True)
        self.config = dict(account_id="a", session_id="s", nonce="n", bearer_token="b"*48, gpu="CPU", output_mode="pc")

    def service(self):
        service = Service(self.paths, self.config)
        self.addCleanup(service.downloads.executor.shutdown, True)
        return service

    def fault(self, code, fn, *args):
        with self.assertRaises(Fault) as raised: fn(*args)
        self.assertEqual(raised.exception.code, code)

    def test_registry_search_uses_search_route_and_at_most_two_pages(self):
        called = []
        def request(route, params):
            called.append((route, params))
            start = (params["page"]-1)*100
            return {"page":params["page"], "limit":100, "total":250, "nodes":[{"id":f"p{i}", "name":f"Pack{i}", "status":"NodeStatusActive", "latest_version":release(f"p{i}")} for i in range(start, start+100)]}
        registry = Registry("0.39.0", "1.55.14", request)
        result = registry.catalog("impact & control", 95, 10, {"p99":{"ver":"0.9.0"}})
        self.assertEqual([row["id"] for row in result["items"]], [f"p{i}" for i in range(95,105)])
        self.assertEqual(len(called), 2)
        self.assertEqual(called[0], ("/nodes/search", {"limit":100,"search":"impact & control","page":1}))
        self.assertTrue(result["items"][4]["installed"])
        self.assertEqual(result["items"][4]["version"], "0.9.0")
        called.clear(); registry.catalog("", 0, 1)
        self.assertEqual(called[0][0], "/nodes")
        self.assertEqual(called[0][1]["comfyui_version"], "0.39.0")
        self.assertEqual(called[0][1]["form_factor"], "git-linux")

    def test_registry_clamped_or_changing_pages_fail_without_missing_rows(self):
        registry = Registry("0.39.0", "1.55.14", lambda *args:{"page":1,"limit":30,"total":100,"nodes":[]})
        self.fault("registry_state", registry.catalog)
        def changing(route, params):return {"page":params["page"],"limit":100,"total":250+params["page"],"nodes":[]}
        registry.request = changing
        self.fault("registry_changed", registry.catalog, "", 95, 10)

    def test_versions_reject_prerelease_deleted_flagged_and_deprecated(self):
        rows = [release(version="2.0.0"), release(version="1.9.0"), release(version="1.10.0"), release(version="3.0.0-beta"), release(version="4.0.0"), release(version="5.0.0"), release(version="6.0.0"), release(key="other")]
        rows[4]["deprecated"] = True; rows[5]["status"] = "NodeVersionStatusDeleted"; rows[6]["status"] = "NodeVersionStatusFlagged"
        calls = []
        def request(route, params):calls.append((route, params));return rows
        registry = Registry("0.39.0", "1.55.14", request)
        result = registry.versions("pack", 1, 1)
        self.assertEqual(result["items"][0]["version"], "1.10.0")
        self.assertEqual(result["total"], 3)
        self.assertEqual(calls[0], ("/nodes/pack/versions", {"statuses":"NodeVersionStatusActive"}))
        self.fault("node_version_unavailable", registry.verify_version, "pack", "5.0.0")
        self.fault("invalid_version", registry.verify_version, "pack", "3.0.0-beta")

    def test_compatibility_declared_and_unknown_are_distinct(self):
        registry = Registry("0.39.0", "1.55.14")
        self.assertEqual(registry.compatibility({})["state"], "unknown")
        self.assertEqual(registry.compatibility({"supported_comfyui_version":"^0.39.0"})["state"], "unknown")
        self.assertEqual(registry.compatibility({"supported_comfyui_version":">=0.39.0,<0.40", "supported_os":["POSIX :: Linux"], "supported_accelerators":["CUDA"]})["state"], "compatible")
        for metadata in ({"supported_comfyui_version":">=0.40"},{"supported_os":["Microsoft :: Windows"]},{"supported_accelerators":["ROCm"]},{"supported_comfyui_frontend_version":"<1.55"}):
            self.assertEqual(registry.compatibility(metadata)["state"], "incompatible")
        registry.request = lambda *args:[release(supported_os=["Microsoft :: Windows"])]
        self.fault("node_incompatible", registry.verify_version, "pack", "1.0.0")

    def test_registry_invalid_identifiers_queries_and_versions_are_bounded(self):
        registry = Registry("0.39.0", "1.55.14", lambda *args:[])
        for value in ("../pack", "https://evil.test", "pack?token=bad", None):self.fault("invalid_node", registry.versions, value)
        for value in ("x"*201, "x\n", None):self.fault("invalid_query", registry.catalog, value)
        registry.request = lambda *args:[{}]*5001
        self.fault("registry_state", registry.versions, "pack")

    def test_registry_fetch_caps_bytes_refuses_redirects_and_has_no_bearer(self):
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self, *args):self.close()
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return Response(json.dumps({"nodes":[]}).encode())
        opener = Opener()
        with patch("remote.registry.build_opener", return_value=opener):fetch("/nodes/search", {"search":"a&token=secret","page":1})
        self.assertEqual(opener.timeout,12)
        self.assertIn("search=a%26token%3Dsecret",opener.request.full_url)
        self.assertNotIn("Authorization",opener.request.headers)
        with patch("remote.registry.build_opener") as builder:
            builder.return_value.open.return_value=Response(b"x"*(2*1024*1024+1))
            self.fault("registry_too_large",fetch,"/nodes",{})
        held = [CAPACITY.acquire(blocking=False) for _ in range(3)]
        try:self.fault("registry_busy",fetch,"/nodes",{})
        finally:
            for value in held:
                if value:CAPACITY.release()

    def test_install_incompatible_version_never_adds_manager_task(self):
        calls=[]
        def manager(route, value=None):calls.append(route);return "V4.2.2" if route.endswith("/version") else {}
        registry=Registry("0.39.0","1.55.14",lambda *args:[release(supported_os=["Microsoft :: Windows"])])
        nodes=Nodes(self.paths,"4.2.2",call=manager,idle=lambda:True,registry=registry)
        self.fault("node_incompatible",nodes.operate,"install",{"id":"pack","version":"1.0.0"})
        self.assertNotIn("/v2/manager/queue/task",calls)
        self.assertFalse(nodes.busy)

    def test_workflow_list_pagination_and_saved_check_stay_inside_new_drive(self):
        service=self.service();root=self.paths.drive/"user/default/workflows";root.mkdir(parents=True)
        (root/"nested").mkdir()
        for name in ("a.json","b.json","nested/c.json"):(root/name).write_text(json.dumps({"1":{"class_type":"Load","inputs":{"model":"present.safetensors"}}}))
        (root/"ignore.txt").write_text("{}");(self.paths.drive/"outside.json").write_text("{}")
        result=service.route("GET","/workflows",{"offset":["1"],"limit":["1"]},{})
        self.assertEqual(result["total"],3);self.assertEqual(result["items"][0]["relative_path"],"b.json")
        self.assertEqual(result["items"][0]["remote_path"],str(root/"b.json"))
        service.cpu=False
        info={"Load":{"input":{"required":{"model":[["present.safetensors"]]}}}}
        with patch("remote.companion.comfy_json",return_value=info):
            checked=service.route("POST","/workflows/check",{}, {"relative_path":"nested/c.json"})
        self.assertTrue(checked["valid"]);self.assertEqual(checked["identity"]["session_id"],"s")
        for value in ("../outside.json","/content/old/wf.json","nested\\c.json"):
            self.fault("invalid_path",service.workflow,"check",{"relative_path":value})
        self.fault("invalid_workflow",service.workflow,"check",{"relative_path":"a.json","workflow":{}})
        self.fault("invalid_workflow",service.workflow,"check",{"relative_path":"ignore.txt"})
        (root/"bad.json").write_text("[")
        self.fault("invalid_workflow",service.workflow,"check",{"relative_path":"bad.json"})

    def test_saved_workflow_bounds_and_symlink_refusal(self):
        service=self.service();root=self.paths.drive/"user/default/workflows";root.mkdir(parents=True)
        file=root/"large.json"
        with file.open("wb") as stream:stream.truncate(16*1024*1024+1)
        self.fault("workflow_too_large",service.workflow,"check",{"relative_path":"large.json"})
        file.unlink();(root/"source.json").write_text("{}")
        with patch("remote.companion.Path.is_symlink",side_effect=lambda:True):
            self.fault("invalid_workflow",service.workflow,"check",{"relative_path":"source.json"})

    def test_download_busy_covers_jobs_outside_first_page(self):
        downloads=Downloads(self.paths);self.addCleanup(downloads.executor.shutdown,True)
        downloads.jobs=[{"status":"running"}]+[{"status":"complete"}]*100
        result=downloads.snapshot(0,50)
        self.assertTrue(result["busy"])
        self.assertTrue(all(row["status"]=="complete" for row in result["items"]))

    def test_cpu_sample_is_nonblocking_throttled_and_uses_counter_delta(self):
        sampler=CpuSample()
        with patch("remote.companion.time.monotonic",side_effect=[0,1,5,10]),patch("remote.companion.Path.read_text",side_effect=["cpu 40 0 10 100 10 0 0 0 999 999", "cpu 50 0 20 110 10 0 0 0 999 999", OSError()] ) as reader:
            self.assertIsNone(sampler.read());self.assertIsNone(sampler.read())
            self.assertEqual(sampler.read(),66.7)
            self.assertIsNone(sampler.read())
            self.assertEqual(reader.call_count,3)

    def test_bootstrap_failed_stage_is_actionable_without_private_output(self):
        command=["python","-m","pip","install","--dry-run","private-token"]
        status=self.paths.state/"operation-status.json"
        error=subprocess.CalledProcessError(2,command,output="secret output",stderr="ResolutionImpossible token=private-token")
        with patch.object(bootstrap,"OPERATION_STATUS",status),patch("remote.bootstrap.subprocess.run",side_effect=error):
            with self.assertRaises(Fault) as raised:bootstrap.run(command)
        self.assertEqual(raised.exception.code,"runtime_command_failed")
        self.assertIn("wheel-resolution",raised.exception.message)
        self.assertIn("dependency-conflict",raised.exception.message)
        report=read_json(status)
        self.assertEqual(report["exit_code"],2);self.assertEqual(report["state"],"failed")
        self.assertNotIn("private-token",json.dumps(report)+raised.exception.message)
        self.assertNotIn("secret output",json.dumps(report)+raised.exception.message)

    def test_environment_recovers_partial_venv_without_ensurepip_or_base_install(self):
        environment=self.paths.root/"venv";environment.mkdir(parents=True)
        (environment/"pyvenv.cfg").write_text("include-system-site-packages = false\n")
        preserved=environment/"owned-pack.txt";preserved.write_text("keep")
        commands=[]
        def run(command, **kwargs):commands.append(command);return ""
        with patch("remote.bootstrap.run",side_effect=run),patch("remote.bootstrap.importlib.util.find_spec",return_value=object()):bootstrap.ensure_environment(environment)
        self.assertIn("--without-pip",commands[0]);self.assertIn("--system-site-packages",commands[0])
        self.assertNotIn("--clear",commands[0]);self.assertNotIn("ensurepip",json.dumps(commands))
        self.assertEqual(preserved.read_text(),"keep")
        self.assertIn("import pip",commands[1][2]);self.assertIn("sys.prefix",commands[1][2])
        with patch("remote.bootstrap.importlib.util.find_spec",return_value=None),patch("remote.bootstrap.run") as runner:
            self.fault("base_pip_missing",bootstrap.ensure_environment,environment)
        runner.assert_not_called()

    def test_real_local_unseeded_venv_inherits_pip_at_owned_prefix(self):
        environment=self.paths.root/"venv"
        with patch.object(bootstrap,"OPERATION_STATUS",None):bootstrap.ensure_environment(environment)
        python=environment/("Scripts/python.exe" if __import__("os").name=="nt" else "bin/python")
        code="import json,pip,sys,pathlib;print(json.dumps({'prefix':sys.prefix,'base':sys.base_prefix,'pip_inherited':not pathlib.Path(pip.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())}))"
        data=json.loads(subprocess.run([str(python),"-c",code],capture_output=True,text=True,check=True).stdout)
        self.assertEqual(Path(data["prefix"]).resolve(),environment.resolve())
        self.assertNotEqual(data["prefix"],data["base"])
        self.assertTrue(data["pip_inherited"])


if __name__=="__main__":unittest.main()
