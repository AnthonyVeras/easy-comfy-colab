"""Check tracked files before publication. Prints filenames/rules, never secrets.

This small policy check complements a dedicated secret scanner such as Gitleaks.
It is not a guarantee that arbitrary personal information can be detected.
"""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {"accounts", "input", "output", "user", "models", "logs", "profiles", ".venv"}
PRIVATE_SUFFIXES = {".exe", ".zip", ".pem", ".key", ".safetensors", ".gguf", ".ckpt", ".pth", ".log", ".lnk"}
RULES = {
    "Hugging Face token": rb"\bhf_[A-Za-z0-9]{25,}\b",
    "GitHub token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b",
    "Google OAuth token": rb"\bya29\.[A-Za-z0-9_-]{20,}",
    "Private key": rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    "Credential JSON": rb'"(?:access_token|refresh_token)"\s*:\s*"[A-Za-z0-9_./+-]{20,}"',
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    )
    return [Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name]


def main() -> int:
    files = tracked_files()
    if not files:
        print("No tracked files. Stage the intended public files first.")
        return 1
    failures = []
    for relative in files:
        path = ROOT / relative
        if relative.parts[0] in PRIVATE_DIRS or relative.suffix.lower() in PRIVATE_SUFFIXES:
            failures.append((relative, "runtime data/binary forbidden in source repository"))
        if re.search(r"(?:token|credential|secret).*\.json$", relative.name, re.I):
            failures.append((relative, "credential filename"))
        content = path.read_bytes()
        if len(content) > 2_000_000:
            failures.append((relative, "unexpected large file"))
        for rule, pattern in RULES.items():
            if re.search(pattern, content):
                failures.append((relative, rule))
    for relative, rule in failures:
        print(f"BLOCKED: {relative.as_posix()}: {rule}")
    if not failures:
        print(f"Public tree policy: {len(files)} tracked files checked; no findings.")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
