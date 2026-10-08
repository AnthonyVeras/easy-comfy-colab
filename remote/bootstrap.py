"""JSON-only bootstrap for a controller-owned, new Colab runtime."""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import hmac
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

if __package__:
    from . import artifacts
    from .cache import model_paths
    from .common import Fault, Paths, atomic_json, comfy_json, public_identity, read_json, validate_identity
else:
    import artifacts
    from cache import model_paths
    from common import Fault, Paths, atomic_json, comfy_json, public_identity, read_json, validate_identity

MANIFEST = read_json(Path(__file__).with_name("runtime.json"))
OPERATION_STATUS = None


def command_stage(command):
    # Only fixed stage labels leave the VM; arguments and subprocess output can contain secrets.
    if len(command) > 2 and command[1:3] == ["-m", "pip"]:
        return "wheel-resolution" if "--dry-run" in command else "wheel-install"
    if len(command) > 2 and command[1:3] == ["-m", "venv"]: return "environment-create"
    if Path(str(command[0])).name == "git": return "core-revision"
    if Path(str(command[0])).name == "comfy": return "cli-config"
    return "runtime-verification"


def run(command, timeout=180):
    started = time.monotonic()
    status = {"stage": command_stage(command), "state": "running", "started_at": time.time(), "elapsed_seconds": 0, "exit_code": None, "reason": None}
    def record():
        if OPERATION_STATUS is not None: atomic_json(OPERATION_STATUS, status)
    record()
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        reason = "deadline" if isinstance(error, subprocess.TimeoutExpired) else "command-unavailable" if isinstance(error, OSError) else "command-rejected"
        # Recognize a few actionable causes without emitting their potentially private surrounding text.
        if isinstance(error, subprocess.CalledProcessError):
            details = str(error.stderr or "")
            for text, hint in (("No matching distribution found", "wheel-unavailable"), ("ResolutionImpossible", "dependency-conflict"), ("No space left on device", "disk-full")):
                if text in details: reason = hint; break
        status.update(state="failed", elapsed_seconds=round(time.monotonic()-started, 3), exit_code=getattr(error, "returncode", None), reason=reason)
        record()
        raise Fault("runtime_command_failed", f"Pinned runtime stage {status['stage']} failed ({reason}, exit {status['exit_code']}). Inspect operation-status.json; preserve the VM and its Colab Torch stack.") from None
    status.update(state="complete", elapsed_seconds=round(time.monotonic()-started, 3), exit_code=0)
    record()
    return result.stdout


def python_at(root, component="venv"):
    return root / component / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def validate_runtime(root):
    if run(["git", "-C", str(root / "ComfyUI"), "rev-parse", "HEAD"], timeout=15).strip() != MANIFEST["core"]["revision"]:
        raise Fault("core_changed", "ComfyUI revision differs from the pinned runtime; preservation and startup were blocked.")
    guard = root / "ComfyUI/custom_nodes/easy_runtime_guard"
    for saved, original in (("__init__.py", "admission.py"), ("gate.py", "gate.py")):
        if not (guard / saved).is_file() or (guard / saved).read_bytes() != Path(__file__).with_name(original).read_bytes():
            raise Fault("guard_missing", "Runtime admission protection is missing or changed; preservation and startup were blocked.")
    code = """import json,importlib.metadata as m,pathlib,sys
import torch,aiohttp,yaml,safetensors
from transformers import CLIPTokenizer,CLIPTextModel
spec=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert all(m.version(n)==v for n,v in spec.items())
names=json.loads(pathlib.Path(sys.argv[2]).read_text())
assert all(not pathlib.Path(m.distribution(n).locate_file('')).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()) for n in names)
print(json.dumps({'torch':m.version('torch'),'cuda':torch.cuda.is_available(),'torch_home':str(pathlib.Path(torch.__file__).resolve()),'versions':{n:m.version(n) for n in names}}))
"""
    try: data = json.loads(run([str(python_at(root)), "-c", code, str(root / "locks/core-packages.json"), str(root / "locks/protected-packages.json")], timeout=60))
    except Exception: raise Fault("runtime_incompatible", "Runtime dependency versions are incompatible with this Colab base; no installation was replaced.") from None
    base = json.loads(run([sys.executable, "-c", "import json,torch,pathlib;print(json.dumps({'home':str(pathlib.Path(torch.__file__).resolve()),'cuda':torch.cuda.is_available()}))"], timeout=45))
    if not base["cuda"] or not data["cuda"] or data["torch_home"] != base["home"] or data["versions"] != read_json(root / "locks/protected-packages.json"):
        raise Fault("torch_changed", "The runtime substituted Colab CUDA packages; preservation and startup were blocked.")
    run([str(python_at(root, "mcp-venv")), "-c", "import json,pathlib,sys; from importlib.metadata import version; import comfy_mcp.server; assert all(version(n)==v for n,v in json.loads(pathlib.Path(sys.argv[1]).read_text()).items()); assert version('comfy-mcp')=='" + MANIFEST["mcp"] + "'; assert version('comfy-cli')=='" + MANIFEST["comfy_cli"] + "'", str(root / "locks/mcp-packages.json")], timeout=45)


def ensure_environment(environment):
    if environment.is_symlink(): raise Fault("unsafe_environment", "A runtime environment cannot point outside its owned directory.")
    if importlib.util.find_spec("pip") is None: raise Fault("base_pip_missing", "Colab's base pip is unavailable. Preserve this VM and reconnect to a supported Colab base.")
    # Colab 3.13 omits ensurepip. Reuse its pip through system-site-packages; never seed or modify the base.
    # Re-running without --clear also completes a prior venv attempt that failed before enabling inheritance.
    run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(environment)])
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    code = "import pip,sys,pathlib;root=pathlib.Path(sys.argv[1]).resolve();assert pathlib.Path(sys.prefix).resolve()==root;assert sys.prefix!=sys.base_prefix;assert 'include-system-site-packages = true' in (root/'pyvenv.cfg').read_text().lower()"
    run([str(python), "-c", code, str(environment)], timeout=30)


def install_locked(paths, component, requirements, protected):
    environment = paths.root / component
    ensure_environment(environment)
    python = str(python_at(paths.root, component))
    locks = paths.root / "locks"; locks.mkdir(exist_ok=True)
    requested = locks / (component + "-requirements.in")
    requested.write_text(requirements, encoding="utf-8")
    constraint = locks / "colab-constraints.txt"
    constraint.write_text("\n".join(name + "==" + version for name, version in protected.items()) + "\n", encoding="utf-8")
    atomic_json(locks / "protected-packages.json", protected)
    lock = locks / (component + ".lock")
    if not lock.exists():
        report_path = locks / (component + "-resolution.json")
        run([python, "-m", "pip", "--isolated", "install", "--index-url", "https://pypi.org/simple", "--disable-pip-version-check", "--dry-run", "--report", str(report_path), "--only-binary=:all:", "-c", str(constraint), "-r", str(requested)], timeout=240)
        report = read_json(report_path)
        lines = []
        for package in report["install"]:
            name = package["metadata"]["name"].lower().replace("_", "-")
            if name in protected: raise Fault("torch_changed", "The resolver requested a replacement of Colab CUDA packages; startup was blocked.")
            info = package["download_info"]; parts = urlsplit(info["url"])
            sha = info.get("archive_info", {}).get("hashes", {}).get("sha256")
            if parts.scheme != "https" or parts.hostname != "files.pythonhosted.org" or parts.username or parts.password or not sha:
                raise Fault("unlocked_dependency", "A dependency has no verified PyPI wheel; a tested runtime package is required.")
            lines.append(name + " @ " + info["url"] + " --hash=sha256:" + sha)
        lock.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    run([python, "-m", "pip", "--isolated", "install", "--index-url", "https://pypi.org/simple", "--disable-pip-version-check", "--require-hashes", "--no-deps", "--only-binary=:all:", "-r", str(lock)], timeout=300)
    # Capture the required dependency closure, including inherited Colab packages.
    code = """import importlib.metadata as m,json,pathlib,sys
from packaging.requirements import Requirement
todo=[Requirement(x).name for x in pathlib.Path(sys.argv[1]).read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')]
seen={}
while todo:
 name=todo.pop().lower().replace('_','-')
 if name in seen: continue
 dist=m.distribution(name); seen[name]=dist.version
 for text in dist.requires or []:
  req=Requirement(text)
  if req.marker is None or req.marker.evaluate({'extra':''}): todo.append(req.name)
inherited={n:v for n,v in seen.items() if not pathlib.Path(m.distribution(n).locate_file('')).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())}
print(json.dumps({'packages':seen,'inherited':inherited},sort_keys=True))
"""
    packages = json.loads(run([python, "-c", code, str(requested)]))
    atomic_json(locks / ("core-packages.json" if component == "venv" else "mcp-packages.json"), packages["packages"])
    atomic_json(locks / (component + "-inherited.json"), packages["inherited"])


def prepare(paths, config):
    if not paths.drive.parent.is_dir(): raise Fault("drive_required", "Mount Google Drive before preparing the new runtime.")
    if paths.drive == Paths().drive and not os.path.ismount(paths.drive.parents[1]):
        raise Fault("drive_required", "The Drive mount could not be verified; local directories do not count as persistent storage.")
    paths.root.mkdir(parents=True, exist_ok=True); paths.state.mkdir(mode=0o700, exist_ok=True)
    previous = read_json(paths.state / "session.json", None)
    if previous and public_identity(previous) != public_identity(config): raise Fault("identity_mismatch", "This VM already belongs to another runtime identity; it was preserved.")
    for folder in (paths.models, paths.drive / "input", paths.drive / "user/default/workflows", paths.artifacts): folder.mkdir(parents=True, exist_ok=True)
    atomic_json(paths.state / "session.json", config)
    model_paths(paths)
    if config["gpu"] == "CPU": return {"installed": True, "mode": "downloads", "restored": False}
    cuda = json.loads(run([sys.executable, "-c", "import torch,json;print(json.dumps({'available':torch.cuda.is_available(),'version':torch.__version__}))"], timeout=45))
    if not cuda["available"]: raise Fault("cuda_unavailable", "Colab CUDA is unavailable. Reconnect with a healthy GPU runtime; the app will not replace its driver or Torch.")
    installed = read_json(paths.state / "installed.json", {})
    if installed.get("recipe") == artifacts.recipe() and installed.get("base") == artifacts.compatibility():
        guard = paths.comfy / "custom_nodes/easy_runtime_guard/__init__.py"
        if not guard.is_file() or guard.read_bytes() != Path(__file__).with_name("admission.py").read_bytes(): raise Fault("guard_missing", "Runtime admission protection is missing or changed; startup was blocked.")
        if not guard.with_name("gate.py").is_file() or guard.with_name("gate.py").read_bytes() != Path(__file__).with_name("gate.py").read_bytes(): raise Fault("guard_missing", "Runtime admission policy is missing or changed; startup was blocked.")
        validate_runtime(paths.root); return {"installed": True, "restored": False}
    restored = artifacts.restore(paths, validate=validate_runtime)
    if not restored:
        if paths.comfy.exists():
            revision = run(["git", "-C", str(paths.comfy), "rev-parse", "HEAD"]).strip()
            if revision != MANIFEST["core"]["revision"]: raise Fault("core_conflict", "An unexpected ComfyUI revision exists in this new runtime; no files were replaced.")
        else:
            run(["git", "init", str(paths.comfy)])
            run(["git", "-C", str(paths.comfy), "remote", "add", "origin", MANIFEST["core"]["repository"]])
            run(["git", "-C", str(paths.comfy), "fetch", "--depth", "1", "origin", MANIFEST["core"]["revision"]])
            run(["git", "-C", str(paths.comfy), "checkout", "--detach", "FETCH_HEAD"])
        protected = {d.metadata["Name"].lower().replace("_", "-"): d.version for d in importlib.metadata.distributions() if d.metadata["Name"].lower().replace("_", "-") in {"torch", "torchvision", "torchaudio", "triton"} or d.metadata["Name"].lower().startswith("nvidia-")}
        requirements = (paths.comfy / "requirements.txt").read_text()
        required = [line for line in requirements.splitlines() if line.strip() and not line.startswith("#") and line.split("==")[0] not in {"torch", "torchvision", "torchaudio"}]
        required += ["comfyui-manager==" + MANIFEST["manager"], "uv==" + MANIFEST["resolver"]]
        install_locked(paths, "venv", "\n".join(required) + "\n", protected)
        install_locked(paths, "mcp-venv", "comfy-mcp==" + MANIFEST["mcp"] + "\ncomfy-cli==" + MANIFEST["comfy_cli"] + "\n", protected)
        node = paths.comfy / "custom_nodes/easy_runtime_guard"; node.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(__file__).with_name("admission.py"), node / "__init__.py")
        shutil.copyfile(Path(__file__).with_name("gate.py"), node / "gate.py")
        validate_runtime(paths.root)
    node = paths.comfy / "custom_nodes/easy_runtime_guard"; node.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("admission.py"), node / "__init__.py")
    shutil.copyfile(Path(__file__).with_name("gate.py"), node / "gate.py")
    atomic_json(paths.state / "installed.json", {"recipe": artifacts.recipe(), "base": artifacts.compatibility()})
    model_paths(paths)
    return {"installed": True, "restored": restored, "mode": "comfy", "recovery": read_json(paths.state / "runtime-restored.json", None) if restored else None}


def owned_process(paths, name):
    info = read_json(paths.state / (name + "-process.json"), {})
    pid = info.get("pid")
    if type(pid) is not int or pid <= 1: return None
    try:
        actual = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if [x.decode() for x in actual if x] != info.get("command"): return None
        return pid
    except OSError: return None


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def spawn(paths, name, command, cwd=None, environment=None):
    if owned_process(paths, name): return
    port = {"comfy": 8188, "companion": 8190, "mcp": 8189}[name]
    if listening(port): raise Fault("remote_port_conflict", "A remote service port is owned by an unknown process; it was preserved.")
    with (paths.state / (name + ".log")).open("ab") as log:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    atomic_json(paths.state / (name + "-process.json"), {"pid": process.pid, "command": command})


def companion_request(paths, route, data=None):
    config = read_json(paths.state / "session.json")
    headers = {"Authorization": "Bearer " + config["bearer_token"], "X-Easy-Account": config["account_id"], "X-Easy-Session": config["session_id"], "X-Easy-Nonce": config["nonce"], "Content-Type": "application/json"}
    request = Request("http://127.0.0.1:8190" + route, data=None if data is None else json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), headers=headers)
    try:
        timeout = 1200 if route in {"/runtime/save", "/stop/prepare"} else 300 if route in {"/outputs/prepare", "/outputs/ack"} else 30
        with urlopen(request, timeout=timeout) as response: result = json.load(response)
    except HTTPError as error:
        try: body = json.load(error); raise Fault(body["error"]["code"], body["error"]["message"]) from None
        except (ValueError, KeyError): raise Fault("companion_failed", "The runtime companion refused the operation.") from None
    except Exception: raise Fault("companion_unavailable", "Reconnect to the owned runtime companion before continuing.") from None
    return result.get("result", result)


def start(paths, config):
    if config["gpu"] != "CPU": validate_runtime(paths.root)
    if __package__: from .outputs import Outputs
    else: from outputs import Outputs
    output = Outputs(paths, config).root
    environment = os.environ.copy()
    environment["PIP_CONSTRAINT"] = str(paths.root / "locks/colab-constraints.txt")
    environment["UV_CONSTRAINT"] = environment["PIP_CONSTRAINT"]
    environment["COMFY_BIN"] = str(paths.root / "mcp-venv/bin/comfy")
    environment["COMFY_PROJECT"] = str(paths.comfy)
    environment["COMFY_LOCAL_URL"] = "http://127.0.0.1:8188"
    environment["PATH"] = str(paths.root / "venv/bin") + os.pathsep + environment.get("PATH", "")
    environment.pop("COMFYUI_URL", None); environment.pop("COMFYUI_HOST", None)
    companion_python = str(python_at(paths.root)) if config["gpu"] != "CPU" else sys.executable
    spawn(paths, "companion", [companion_python, str(Path(__file__).with_name("companion.py"))])
    if config["gpu"] != "CPU":
        spawn(paths, "comfy", [str(python_at(paths.root)), str(paths.comfy / "main.py"), "--listen", "127.0.0.1", "--port", "8188", "--enable-manager", "--disable-auto-launch", "--extra-model-paths-config", str(paths.root / "extra-model-paths.yaml"), "--input-directory", str(paths.drive / "input"), "--output-directory", str(output), "--user-directory", str(paths.drive / "user")], cwd=str(paths.comfy), environment=environment)
        comfy_bin = str(paths.root / "mcp-venv/bin/comfy")
        run([comfy_bin, "set-default", str(paths.comfy)], timeout=30)
        code = "from comfy_mcp.server import mcp;mcp.run(transport='streamable-http',host='127.0.0.1',port=8189,json_response=True,stateless_http=True)"
        spawn(paths, "mcp", [str(python_at(paths.root, "mcp-venv")), "-c", code], cwd=str(paths.comfy), environment=environment)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        for name in ("companion", "comfy", "mcp") if config["gpu"] != "CPU" else ("companion",):
            if not owned_process(paths, name): raise Fault("service_exited", "A runtime service exited; preserve the VM and inspect recovery.")
        try:
            health = companion_request(paths, "/health")
            if any(health.get(k) != v for k, v in public_identity(config).items()): raise Fault("identity_mismatch", "Runtime companion identity did not match the allocation.")
            if config["gpu"] == "CPU" or (comfy_json("/system_stats") and listening(8189)):
                companion_request(paths, "/outputs/resume", {})
                return {"ready": True, **public_identity(config), "output_mode":config["output_mode"], "remote_ports": {"comfy": None if config["gpu"] == "CPU" else 8188, "companion": 8190, "mcp": None if config["gpu"] == "CPU" else 8189}, "paths": {"root": str(paths.root), "models": str(paths.models), "workflows": str(paths.drive / "user/default/workflows"), "outputs": str(output)}}
        except Fault as error:
            if error.code == "identity_mismatch": raise
        time.sleep(0.5)
    raise Fault("readiness_timeout", "Runtime services did not become usable in the readiness budget; the VM was preserved.")


def stop_unprepared(paths, config, request):
    if any(request.get(k) != config[k] for k in ("gpu", "output_mode")) or not hmac.compare_digest(str(request.get("bearer_token", "")), config["bearer_token"]):
        raise Fault("identity_mismatch", "Incomplete runtime stop did not match its owned session configuration.")
    def inspected(path):
        try: return path.lstat()
        except FileNotFoundError: return None
        except OSError: raise Fault("invalid_state", "Incomplete runtime files could not be inspected; preserve the VM for recovery.") from None
    if inspected(paths.state / "installed.json") is not None:
        raise Fault("runtime_already_prepared", "An installed runtime must use the normal output and preservation gates.")
    for name in ("comfy", "companion", "mcp"):
        # Even a stale or malformed service record requires the normal recovery path.
        if inspected(paths.state / (name+"-process.json")) is not None:
            raise Fault("process_unknown", "A service record exists; reconnect before stopping the runtime.")
    if any(listening(port) for port in (8188, 8190, 8189)):
        raise Fault("remote_port_conflict", "A runtime service port is active; reconnect before stopping.")
    for output in (paths.root / "output-pc", paths.root / "output", paths.root / "output-drive", paths.drive / "output"):
        metadata = inspected(output)
        if metadata is None: continue
        if stat.S_ISLNK(metadata.st_mode): raise Fault("unsafe_output", "An output directory contains a symbolic link.")
        if not stat.S_ISDIR(metadata.st_mode): raise Fault("outputs_pending", "An output path contains a file; recover delivery before stopping.")
        def unreadable(error): raise Fault("unsafe_output", "An output directory could not be inspected; preserve the VM for recovery.")
        for directory, dirs, names in os.walk(output, followlinks=False, onerror=unreadable):
            if names or any((Path(directory)/name).is_symlink() for name in dirs):
                raise Fault("outputs_pending", "Output files exist; recover the normal delivery gate before stopping.")
    # No service ever started and no outputs exist. Nothing new needs preserving;
    # existing Drive artifacts are deliberately neither read nor written by this gate.
    return {**public_identity(config), "safe_to_stop":True, "unprepared":True, "installed":False,
            "pending_outputs":0, "runtime_required":False, "output_mode":config["output_mode"]}


def dispatch(request, paths=None):
    global OPERATION_STATUS
    paths = paths or Paths()
    action = request.get("action")
    if action not in {"prepare", "start", "restart", "freeMemory", "stop", "stop_unprepared", "health", "report", "runtime.save", "outputs.prepare", "outputs.ack", "resume"}:
        raise Fault("invalid_action", "Unknown runtime action.")
    config = read_json(paths.state / "session.json", None)
    if action == "prepare":
        config = {k: request[k] for k in ("account_id", "session_id", "nonce", "bearer_token", "gpu", "output_mode")}
        validate_identity(config)
        if config["gpu"] not in {"CPU", "T4", "L4", "A100", "G4", "H100"}: raise Fault("invalid_gpu", "Select a supported Colab accelerator.")
        OPERATION_STATUS = paths.state / "operation-status.json"
        return prepare(paths, config)
    if not config: raise Fault("runtime_missing", "Prepare this new runtime before starting services.")
    validate_identity(config)
    if any(request.get(k) != config[k] for k in ("account_id", "session_id", "nonce")):
        raise Fault("identity_mismatch", "Runtime action did not match its owned allocation.")
    OPERATION_STATUS = paths.state / "operation-status.json"
    if action == "stop_unprepared": return stop_unprepared(paths, config, request)
    if action == "start": return start(paths, config)
    if action == "restart":
        target_mode = request.get("output_mode", config["output_mode"])
        if target_mode not in {"pc", "drive"} or request.get("previous_output_mode", config["output_mode"]) != config["output_mode"]:
            raise Fault("output_mode_conflict", "Output mode changed during restart. Reconnect and verify the current destination.")
        companion_request(paths, "/restart/prepare", {})
        pid = owned_process(paths, "comfy")
        if not pid: raise Fault("process_unknown", "ComfyUI process identity could not be verified.")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic()+30
        while listening(8188) and time.monotonic() < deadline: time.sleep(0.2)
        if listening(8188): raise Fault("restart_timeout", "ComfyUI did not release its port; no second server was started.")
        if target_mode != config["output_mode"]:
            companion_request(paths, "/outputs/switch", {"output_mode":target_mode, "previous_output_mode":config["output_mode"]})
            config = read_json(paths.state / "session.json")
        return start(paths, config)
    routes = {"freeMemory": ("/free", {"unload_models": True, "free_memory": True}), "stop": ("/stop/prepare", {}), "health": ("/health", None), "report": ("/snapshot", None), "runtime.save": ("/runtime/save", {}), "outputs.prepare": ("/outputs/prepare", {}), "outputs.ack": ("/outputs/ack", request.get("params", {})), "resume": ("/outputs/resume", {})}
    if action == "freeMemory": return {"released": bool(comfy_json(*routes[action]) is not None)}
    return companion_request(paths, *routes[action])


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdin", action="store_true"); group.add_argument("--request")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        raw = sys.stdin.read(1024*1024+1) if args.stdin else args.request
        if len(raw) > 1024*1024: raise Fault("request_too_large", "Runtime request exceeded the size limit.")
        request = json.loads(raw)
        if not isinstance(request, dict): raise Fault("invalid_request", "A runtime request must be a JSON object.")
        if not args.stdin and "bearer_token" in request: raise Fault("secret_argument", "Send session credentials through stdin, never process arguments.")
        result = {"ok": True, "result": dispatch(request)}
    except Fault as error: result = {"ok": False, "error": {"code": error.code, "message": error.message}}
    except Exception: result = {"ok": False, "error": {"code": "runtime_failed", "message": "The runtime operation failed safely; preserve the allocation and inspect recovery."}}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__": main()
