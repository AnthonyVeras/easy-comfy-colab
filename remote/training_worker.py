"""Isolated Linux MOSS worker. Receives a controller-created recipe on stdin."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import venv

try:
    from .common import Paths, Fault, atomic_json, read_json, digest
    from .training import Training, TRAINER_REVISION, MODEL_REVISION, RECIPE, extract_dataset, verify_results
except ImportError:
    from common import Paths, Fault, atomic_json, read_json, digest
    from training import Training, TRAINER_REVISION, MODEL_REVISION, RECIPE, extract_dataset, verify_results

PACKAGES = ["numpy==2.5.2", "Pillow==12.3.0", "safetensors==0.8.0", "kohaku-engine==0.5.0", "kohakuvault==0.9.0", "lycoris-lora==4.1.3", "diffusers==0.41.0", "transformers==5.20.2", "accelerate==1.15.0", "huggingface-hub==1.33.0", "anyschedule==0.1.0", "packaging==26.3"]
cancelled = False


def cancel(signum, frame):
    global cancelled
    cancelled = True


def config_values(project, base, source, weights, data):
    p = project["settings"]
    return {
        "WEIGHTS": str(weights), "VARIANT": "sft", "TRANSFORMER": "moss_image_sft_int8_convrot.safetensors",
        "TEXT_ENCODER": "qwen3.5_9b_int8_convrot.safetensors", "VAE": "flux2-vae.safetensors", "BASE_PRECISION": "auto",
        "DATASETS": [{"source": "folder", "path": str(data), "recursive": False, "txt_as": "nl"}], "CAPTION": {"template": "nl"}, "CAPTION_VARIANTS": 1,
        "CACHE_DIR": str(base / "cache" / project["id"]), "LATENT_CACHE": "offline", "TEXT_CACHE": "offline",
        "TARGET_AREA": p["resolution"]**2, "RESOLUTION_MODE": "resize", "UPSCALE": "never", "CROP": "center", "FLIP": False,
        "MAX_TOKENS": 16384, "MAX_SAMPLES": 1, "TOKEN_BUDGET": "render", "NUM_WORKERS": 0, "DATA_SCAN_WORKERS": 2,
        "MEMORY_PROFILE": p["profile"], "TEXT_ENCODE": {"memory": "auto", "precision": "auto", "dtype": "bf16", "batch": 4, "max_tokens": 2048},
        "VAE_ENCODE": {"memory": "auto", "batch": 1, "compile": "off"}, "VAE_DTYPE": "bf16",
        "DIM": p["rank"], "ALPHA": float(p["rank"]), "LR": p["lr"], "STEPS": p["steps"], "GRAD_ACC": 2,
        "SCHEDULE": {"mode": "cosine", "warmup": min(50, p["steps"]//10), "end": -1, "min_value": 0.1},
        "TRAIN_TEXT_REFINER": False, "TUNE": "off", "RETUNE_STEPS": 0, "COMPILE": "off",
        "SAMPLE_EVERY": 0, "SAMPLE_PROMPTS": [], "SAVE_EVERY": p["save_every"], "SAVE_MINUTES": 15, "KEEP_LAST": 3,
        "SAVE_FORMAT": "both", "SAVE_MERGED": False, "CHECKPOINT_EXPORT": True, "FINAL_CHECKPOINT": True,
        "OUTPUT_DIR": str(Path(project["run"]).parent), "RUN_NAME": p["name"], "LOG_EVERY": 5, "PROGRESS": False, "SEED": p["seed"],
    }


def main():
    job = sys.argv[sys.argv.index("--job")+1]
    payload = json.load(sys.stdin)
    paths = Paths(Path(payload["root"]), Path(payload["drive"]))
    training = Training(paths)
    project, action = payload["project"], payload["action"]
    base = paths.root / "training"
    source, env = base / "trainer", base / "env"
    python = env / "bin/python"
    base.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, cancel)
    signal.signal(signal.SIGTERM, cancel)

    def state(**values):
        current = training.current()
        if current.get("job") != job: raise RuntimeError("Worker ownership changed")
        atomic_json(training.file, {**current, **values})

    def run(args, phase, cwd=None):
        if cancelled: raise InterruptedError("Cancelled")
        state(phase=phase)
        # Children share the worker process group, so cancellation reaches pip,
        # the trainer and its distributed children without leaving GPU work alive.
        with (paths.state / "training-worker.log").open("ab") as log:
            child = subprocess.Popen([str(a) for a in args], cwd=cwd, stdout=log, stderr=log, env={**os.environ, "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"})
            code = child.wait()
        if cancelled: raise InterruptedError("Cancelled")
        if code: raise RuntimeError(f"{phase} exited ({code}). Inspect training-worker.log on the VM.")

    try:
        if sys.platform != "linux": raise RuntimeError("Colab training requires Linux")
        if action == "prepare":
            if shutil.disk_usage(paths.root).free < 35*1024**3: raise RuntimeError("At least 35 GiB free VM disk is required")
            run(["nvidia-smi"], "gpu-check")
            driver = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], capture_output=True, text=True, check=True).stdout.strip().splitlines()[0]
            if int(driver.split(".")[0]) < 580: raise RuntimeError("This pinned PyTorch/CUDA 13 recipe requires NVIDIA driver 580 or newer. Select a compatible Colab runtime.")
            if not source.exists():
                run(["git", "init", source], "trainer-install")
                run(["git", "-C", source, "remote", "add", "origin", "https://github.com/KohakuBlueleaf/KohakuMossTrainer.git"], "trainer-install")
            head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            if head != TRAINER_REVISION:
                run(["git", "-C", source, "fetch", "--depth=1", "origin", TRAINER_REVISION], "trainer-install")
                run(["git", "-C", source, "checkout", "--detach", "FETCH_HEAD"], "trainer-install")
            run(["git", "-C", source, "diff", "--exit-code", "HEAD"], "trainer-verify")
            install_pin = {"trainer": TRAINER_REVISION, "packages": PACKAGES, "torch": "2.14.1", "cuda": "cu130"}
            if read_json(base / "installed.json", {}) != install_pin:
                state(phase="environment")
                venv.EnvBuilder(with_pip=True).create(env)
                run([python, "-m", "pip", "install", "torch==2.14.1", "--index-url", "https://download.pytorch.org/whl/cu130"], "torch-install")
                run([python, "-m", "pip", "install", *PACKAGES, "-e", source], "dependencies")
                run([python, "-m", "pip", "check"], "dependencies-check")
                atomic_json(base / "installed.json", install_pin)
            # Actual device capability and BF16 are checked in the isolated runtime.
            probe = base / "gpu.json"
            code = "import torch,json; p=torch.cuda.get_device_properties(0); assert torch.cuda.is_bf16_supported(), 'BF16 GPU required'; assert p.total_memory>=14*1024**3, 'At least 16 GB GPU required'; print(p); json.dump({'name':p.name,'vram_bytes':p.total_memory,'torch':torch.__version__,'cuda':torch.version.cuda},open("+repr(str(probe))+",'w'))"
            run([python, "-c", code], "gpu-check")
            state(gpu=read_json(probe))
            weights = paths.drive / "training/weights"
            files = ["diffusion_models/moss_image_sft_int8_convrot.safetensors", "text_encoders/qwen3.5_9b_int8_convrot.safetensors", "vae/flux2-vae.safetensors"]
            for name in files:
                code = "from huggingface_hub import hf_hub_download; hf_hub_download('Comfy-Org/MOSS-Image',"+repr(name)+",revision="+repr(MODEL_REVISION)+",local_dir="+repr(str(weights))+")"
                run([python, "-c", code], "weights-"+name.split("/")[0])
            dataset = base / "datasets" / project["id"]
            receipt = base / "datasets" / (project["id"]+".json")
            if not receipt.is_file():
                # Only the per-project scratch extraction is replaced on an interrupted import.
                if dataset.is_symlink() or not dataset.resolve().is_relative_to((base / "datasets").resolve()): raise RuntimeError("Unsafe dataset scratch path")
                if dataset.exists(): shutil.rmtree(dataset)
                state(phase="dataset")
                archive = paths.drive / "training/uploads" / (project["dataset_sha256"]+".zip")
                if digest(archive) != project["dataset_sha256"]: raise RuntimeError("Dataset checksum changed")
                rows = extract_dataset(archive, dataset, project["settings"]["trigger"])
                atomic_json(receipt, rows)
            validate = "from PIL import Image; from pathlib import Path; files=list(Path("+repr(str(dataset))+").glob('*')); images=[p for p in files if p.suffix!='.txt']; [(Image.open(p).verify()) for p in images]; assert images"
            run([python, "-c", validate], "images-check")
            cfg = source / "configs/user" / (project["id"]+".py")
            cfg.parent.mkdir(parents=True, exist_ok=True)
            header = "from kohakuengine import use_config\nuse_config('../moss/_base/base_lora.py')\nuse_config('../moss/algo/algo_lora.py')\nuse_config('../moss/data/data_folder_nl.py')\nuse_config('../moss/precision/precision_bf16.py')\n"
            values = config_values(project, base, source, weights, dataset)
            cfg.write_text(header+"".join(f"{k} = {v!r}\n" for k, v in values.items()), encoding="utf-8")
            data_cfg = cfg.with_name(project["id"]+"_data.py")
            data_values = {"TEXT_ENCODER": "kohakumoss.encoders.text_encoder", "TEXT_ENCODER_KWARGS": {"path": str(weights), "device": "cuda"}, "IMAGE_ENCODER": "kohakumoss.encoders.vae_encoder", "IMAGE_ENCODER_KWARGS": {"path": str(weights / "vae/flux2-vae.safetensors"), "device": "cuda"}, "ENCODE_BATCH": 4}
            data_cfg.write_text("from kohakuengine import use_config\nuse_config("+repr(cfg.name)+")\n"+"".join(f"{k} = {v!r}\n" for k, v in data_values.items()), encoding="utf-8")
            run([env / "bin/kohakumoss-config", "check", cfg], "config-check", source)
            for script, phase in (("inspect_data.py", "inspect"), ("cache_text.py", "text-cache"), ("cache_latents.py", "latent-cache")):
                run([env / "bin/kogine", "run", "scripts/data/"+script, "--config", data_cfg], phase, source)
            atomic_json(base / "prepared" / (project["id"]+".json"), {"recipe": RECIPE, "gpu": read_json(probe), "config_sha256": digest(cfg), "images": len(read_json(receipt))})
            state(phase="ready")
        else:
            cfg = source / "configs/user" / (project["id"]+".py")
            receipt = read_json(base / "prepared" / (project["id"]+".json"))
            if digest(cfg) != receipt["config_sha256"]: raise RuntimeError("Prepared config changed; prepare again")
            args = [env / "bin/kogine", "run", "scripts/train/moss.py", "--config", cfg]
            if action == "resume": args += ["--set", "RESUME=latest"]
            run(args, "training", source)
            state(phase="complete")
    except InterruptedError:
        state(phase="stopped", error="Cancellation completed; resume from the latest complete checkpoint.")
    except Exception as error:
        state(phase="error", error=str(error)[:1000])
    finally:
        # A process-group SIGINT reaches all children. Keep admission blocked if
        # any child remains (including during a companion reconnect).
        state(phase=training.current().get("phase", "error"), busy=False, preserved=False)
        try: training.preserve()
        except Exception as error: state(preserved=False, error="Result verification failed: "+str(error)[:700])


if __name__ == "__main__": main()
