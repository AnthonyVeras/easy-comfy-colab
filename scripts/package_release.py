"""Package tracked public sources and a freshly built application only."""

import hashlib
import sys
import zipfile

from check_public_tree import ROOT, main as check_public_tree, tracked_files


def main() -> int:
    if check_public_tree():
        return 1
    binary = ROOT / "dist" / "Easy Comfy Colab"
    if not (binary / "Easy Comfy Colab.exe").is_file():
        raise SystemExit("Run Build-App.ps1 first.")
    destination = ROOT / "release"
    destination.mkdir(exist_ok=True)
    archive = destination / "Easy-Comfy-Colab-2.0.7-Windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for relative in tracked_files():
            output.write(ROOT / relative, "Easy Comfy Colab/" + relative.as_posix())
        for path in sorted(binary.rglob("*")):
            if path.is_file():
                output.write(path, "Easy Comfy Colab/Easy Comfy Colab/" + path.relative_to(binary).as_posix())
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    (destination / "SHA256SUMS.txt").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(f"Package: {archive.name} ({archive.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"SHA256: {checksum}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
