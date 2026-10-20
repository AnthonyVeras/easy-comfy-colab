"""Build the Windows sidecar; only reviewed code/resources enter the bundle."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def resources(destination: Path):
    remote = destination / "remote"
    remote.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "remote").iterdir():
        if source.is_file() and (source.suffix in {".py", ".json"} or source.name == "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(source, remote / source.name)
    (remote / "web").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "remote/web/remote_download.js", remote / "web/remote_download.js")
    vendor = destination / "controller" / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "NOTICE", "LICENSE.google-colab-cli"):
        shutil.copy2(ROOT / "controller" / "vendor" / name, vendor / name)
    shutil.copy2(ROOT / "LICENSE", destination / "LICENSE")
    notices = destination / "third-party-python"
    notices.mkdir(parents=True, exist_ok=True)
    inventory = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        inventory.append({"name":name, "version":distribution.version,
            "license":distribution.metadata.get("License-Expression", distribution.metadata.get("License", "unspecified"))})
        for file in distribution.files or []:
            if ".dist-info" not in str(file) or not any(word in file.name.lower() for word in ("license", "notice", "copying")):
                continue
            target = notices / name / Path(str(file)).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(file), target)
    (notices / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    return destination


def command(python: str, source: Path):
    result = [python, "-m", "PyInstaller", "--noconfirm", "--onedir", "--console",
              "--name", "easy-comfy-controller", "--distpath", str(ROOT / "dist"),
              "--workpath", str(ROOT / "build" / "controller"),
              "--specpath", str(ROOT / "build"),
              "--paths", str(ROOT), "--paths", str(ROOT / "controller" / "vendor"),
              "--add-data", str(source) + ";."]
    for module in ("controller.google", "controller.transport", "controller.drive", "controller.training",
                   "controller.import_legacy", "colab_cli.client", "colab_cli.runtime",
                   "colab_cli.consumption", "colab_cli.utils"):
        result.extend(["--hidden-import", module])
    result.append(str(ROOT / "controller" / "main.py"))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=str(ROOT / ".venv" / "Scripts" / "python.exe"))
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("The Windows sidecar must be built on Windows.")
    source = resources(ROOT / "build" / "sidecar-resources")
    subprocess.run(command(args.python, source), cwd=ROOT, check=True)
    built = ROOT / "dist" / "easy-comfy-controller"
    destination = ROOT / "dist" / "controller"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built, destination, dirs_exist_ok=True)
    print(destination / "easy-comfy-controller.exe")


if __name__ == "__main__":
    main()
