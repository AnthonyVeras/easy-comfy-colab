import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remote import bootstrap
from remote.common import Fault, Paths, atomic_json, read_json
from remote.companion import Service
from remote.outputs import Outputs


class OutputModeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.paths=Paths(Path(temp.name)/"runtime",Path(temp.name)/"Drive/New")
        self.paths.state.mkdir(parents=True)
        self.config=dict(account_id="a",session_id="s",nonce="n",bearer_token="b"*48,gpu="CPU",output_mode="pc")

    def service(self):
        service=Service(self.paths,self.config)
        self.addCleanup(service.downloads.executor.shutdown,True)
        atomic_json(self.paths.state/"session.json",self.config)
        return service

    def fault(self,code,fn,*args):
        with self.assertRaises(Fault) as caught:fn(*args)
        self.assertEqual(caught.exception.code,code)

    def ack(self,manifest,**flags):
        return {"manifest_id":manifest["manifest_id"],"files":[{k:row[k] for k in ("relative_path","size","sha256")} for row in manifest["files"]],**flags}

    def test_continuous_delivery_never_pauses_and_merges_batches(self):
        outputs=Outputs(self.paths,self.config)
        for i in range(102):(outputs.root/f"{i:03}.png").write_bytes(f"bytes{i}".encode())
        first=outputs.delivery()
        self.assertEqual(len(first["files"]),100)
        self.assertFalse(first["paused"])
        self.assertFalse((self.paths.state/"stopping").exists())
        report=outputs.ack(self.ack(first,delivery=True))
        self.assertEqual(report["delivered"],100)
        self.assertEqual(report["pending_outputs"],2)
        second=outputs.delivery();self.assertEqual(len(second["files"]),2)
        outputs.ack(self.ack(second,delivery=True))
        self.assertEqual(outputs.pending(quick=True),0)
        with patch("remote.outputs.digest",side_effect=AssertionError("unchanged files must not rehash")):
            self.assertEqual(outputs.delivery()["files"],[])
        self.assertFalse((self.paths.state/"output-manifest.json").exists())
        self.assertFalse((self.paths.state/"stopping").exists())

    def test_live_ack_source_mutation_mismatched_batch_duplicate_and_final_gate(self):
        outputs=Outputs(self.paths,self.config);file=outputs.root/"one.png";file.write_bytes(b"first")
        manifest=outputs.delivery();copied=self.ack(manifest,delivery=True)
        self.fault("output_not_paused",outputs.ack,self.ack(manifest))
        self.fault("output_mismatch",outputs.ack,dict(copied,files=[]))
        self.fault("output_mismatch",outputs.ack,dict(copied,files=copied["files"]*2))
        file.write_bytes(b"other")
        self.fault("output_mismatch",outputs.ack,copied)
        self.assertEqual(outputs.pending(quick=True),1)
        self.assertFalse(outputs.ack_path.exists())
        self.assertFalse((self.paths.state/"stopping").exists())
        final=outputs.prepare();outputs.ack(self.ack(final))
        self.assertEqual(outputs.pending(),0)
        self.assertTrue((self.paths.state/"stopping").exists())

    def test_live_capture_queue_pause_mode_incomplete_and_changes_are_safe(self):
        outputs=Outputs(self.paths,dict(self.config,gpu="T4"),idle=lambda:False)
        self.fault("generation_busy",outputs.delivery)
        self.assertFalse((self.paths.state/"stopping").exists())
        outputs=Outputs(self.paths,self.config)
        (outputs.root/"still.part").write_bytes(b"partial")
        self.assertEqual(outputs.delivery()["files"],[])
        (outputs.root/"one.png").write_bytes(b"image")
        with patch.object(outputs,"file_row",side_effect=Fault("output_changed","Changed")):
            self.assertEqual(outputs.delivery()["files"],[])
        (self.paths.state/"stopping").touch();self.fault("session_paused",outputs.delivery)
        drive=Outputs(self.paths,dict(self.config,output_mode="drive"))
        self.fault("invalid_mode",drive.delivery)

    def test_old_outputs_must_be_verified_before_switch_then_roots_survive(self):
        service=self.service();original=service.outputs.root/"one.png";original.write_bytes(b"image")
        change={"previous_output_mode":"pc","output_mode":"drive"}
        with patch("remote.companion.listening",return_value=False):
            self.fault("output_switch_not_ready",service.switch_outputs,change)
            manifest=service.outputs.prepare()
            self.fault("outputs_pending",service.switch_outputs,change)
            service.outputs.ack(self.ack(manifest))
            service.switch_outputs(change)
        self.assertEqual(service.health()["output_mode"],"drive")
        self.assertEqual(read_json(self.paths.state/"session.json")["output_mode"],"drive")
        self.assertEqual(original.read_bytes(),b"image")
        self.assertTrue((self.paths.state/"stopping").exists())
        driveresult=service.outputs.root/"one.png";driveresult.write_bytes(b"different")
        manifest=service.outputs.prepare();service.outputs.ack(self.ack(manifest))
        with patch("remote.companion.listening",return_value=False):service.switch_outputs({"previous_output_mode":"drive","output_mode":"pc"})
        self.assertEqual(service.outputs.root,original.parent)
        self.assertEqual(service.outputs.pending(quick=True),0)
        self.assertEqual(driveresult.read_bytes(),b"different")
        self.assertTrue((self.paths.state/"output-ack-drive.json").exists())
        self.assertTrue((self.paths.state/"output-ack-pc.json").exists())

    def test_switch_rechecks_source_runtime_background_and_closed_port(self):
        service=self.service();file=service.outputs.root/"one.png";file.write_bytes(b"image")
        manifest=service.outputs.prepare();service.outputs.ack(self.ack(manifest))
        change={"previous_output_mode":"pc","output_mode":"drive"}
        with patch("remote.companion.listening",return_value=True):self.fault("output_switch_not_ready",service.switch_outputs,change)
        with patch("remote.companion.listening",return_value=False):
            self.fault("output_mode_conflict",service.switch_outputs,dict(change,previous_output_mode="drive"))
            with patch.object(service,"preserved",side_effect=Fault("runtime_not_preserved","Unsaved")):
                self.fault("runtime_not_preserved",service.switch_outputs,change)
            service.downloads.jobs=[{"status":"running"}]
            self.fault("runtime_busy",service.switch_outputs,change)
            service.downloads.jobs=[];file.write_bytes(b"changed")
            self.fault("outputs_pending",service.switch_outputs,change)
        self.assertEqual(read_json(self.paths.state/"session.json")["output_mode"],"pc")
        self.assertEqual(file.read_bytes(),b"changed")

    def test_previous_pc_outputs_block_drive_stop_even_when_inactive(self):
        service=self.service();file=service.outputs.root/"one.png";file.write_bytes(b"image")
        manifest=service.outputs.prepare();service.outputs.ack(self.ack(manifest))
        with patch("remote.companion.listening",return_value=False):service.switch_outputs({"previous_output_mode":"pc","output_mode":"drive"})
        self.assertEqual(service.pending_outputs(),0)
        file.write_bytes(b"late output")
        self.assertEqual(service.pending_outputs(),1)
        self.fault("outputs_pending",service.route,"POST","/stop/prepare",{},{})
        self.assertTrue(file.exists())

    def test_bootstrap_switch_uses_same_identity_and_committed_mode_on_failure(self):
        atomic_json(self.paths.state/"session.json",dict(self.config,gpu="T4"))
        calls=[]
        def companion(paths,route,data=None):
            calls.append((route,data))
            if route=="/outputs/switch":atomic_json(self.paths.state/"session.json",dict(self.config,gpu="T4",output_mode=data["output_mode"]))
            return {}
        request=dict(self.config,action="restart",gpu="T4",output_mode="drive",previous_output_mode="pc")
        with patch("remote.bootstrap.companion_request",side_effect=companion),patch("remote.bootstrap.owned_process",return_value=42),patch("remote.bootstrap.os.kill") as kill,patch("remote.bootstrap.listening",return_value=False),patch("remote.bootstrap.start",side_effect=Fault("service_exited","Failed")):
            self.fault("service_exited",bootstrap.dispatch,request,self.paths)
        kill.assert_called_once()
        self.assertEqual([route for route,_ in calls],["/restart/prepare","/outputs/switch"])
        saved=read_json(self.paths.state/"session.json")
        self.assertEqual(saved["output_mode"],"drive")
        self.assertEqual(saved["account_id"],self.config["account_id"])
        self.assertEqual(saved["session_id"],self.config["session_id"])
        self.assertEqual(saved["nonce"],self.config["nonce"])


if __name__=="__main__":unittest.main()
