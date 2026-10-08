"""Cross-boundary stop pipeline with real temporary output bytes, no Colab calls."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from controller.core import Controller
from controller.state import Store
from remote.common import Paths
from remote.companion import Service


class DriveBoundary(unittest.TestCase):
    def test_drive_stop_acknowledges_durable_manifest_without_pc_transfer(self):
        with tempfile.TemporaryDirectory() as temp:
            paths=Paths(Path(temp)/"runtime",Path(temp)/"drive/NewProduct");paths.state.mkdir(parents=True);paths.models.mkdir(parents=True)
            store=Store(Path(temp)/"state",crypt=lambda b,decrypt=False:b)
            self.addCleanup(store.close)
            account=store.add("Synthetic account");store.bind(account,{"sub":"owned-subject","email":"test@example.invalid","email_verified":True})
            controller=Controller(store)
            controller.session.update(state="ready",account_id=account,session_id="session",nonce="nonce",subject="owned-subject",endpoint="owned-endpoint",output_mode="drive",gpu="CPU",prepared=True,services_started=True)
            cfg=dict(account_id=account,session_id="session",nonce="nonce",output_mode="drive",gpu="CPU",bearer_token="b"*48)
            service=Service(paths,cfg);self.addCleanup(service.downloads.executor.shutdown,True)
            (service.outputs.root/"image.png").write_bytes(b"actual-persistent-bytes")
            calls=[]
            def call(route,data=None,**kwargs):
                calls.append(route)
                result=service.route("POST" if data is not None else "GET",route,{},data or {})
                if route=="/outputs/prepare":
                    result=copy.deepcopy(result)
                    for file in result["files"]:file["remote_path"]="/content/drive/MyDrive/EasyComfyColab2/output/"+file["relative_path"]
                return result
            controller.companion=SimpleNamespace(call=call,health=lambda:service.health())
            controller.transport=Mock();controller.colab=Mock()
            controller.require_remote=Mock(return_value=controller.companion)
            controller.bootstrap=Mock(side_effect=lambda action:service.route("POST","/stop/prepare",{},{}))
            with patch("controller.core.Path.home",return_value=Path(temp)/"pc"):
                result=controller.stop()
            self.assertEqual(result["state"],"idle")
            controller.bootstrap.assert_called_once_with("stop")
            self.assertEqual(calls,["/outputs/prepare","/outputs/ack","/runtime/save"])
            self.assertFalse((Path(temp)/"pc").exists())
            self.assertEqual((service.outputs.root/"image.png").read_bytes(),b"actual-persistent-bytes")
            service.downloads.executor.shutdown(wait=True)
            store.close()


if __name__=="__main__":unittest.main()
