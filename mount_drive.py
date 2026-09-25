"""Monta o Drive no Colab e abre automaticamente a autorização no Windows."""

from __future__ import annotations

import subprocess
import sys
import os
import shlex


AUTH_PREFIX = "https://accounts.google.com/o/oauth2/v2/auth?"


def main() -> int:
    cli, session = sys.argv[1:3]
    command = (
        [cli]
        + (["--config", os.environ["COMFY_COLAB_CONFIG_PATH"]] if os.environ.get("COMFY_COLAB_CONFIG_PATH") else [])
        + ["drivemount", "-s", session]
    )
    # O Colab CLI lê /dev/tty durante a autorização do Drive. O app Windows
    # não possui console; `script` fornece um pseudoterminal e repassa o Enter
    # escrito pelo usuário no painel.
    process = subprocess.Popen(
        ["script", "-q", "-f", "-e", "-c", shlex.join(command), "/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            url = line.strip()
            if url.startswith(AUTH_PREFIX) and os.environ.get("COMFY_APP_MODE") != "1":
                try:
                    subprocess.Popen(
                        ["explorer.exe", url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    print(
                        "Autorize o Drive no navegador; depois volte a esta janela "
                        "e pressione Enter.",
                        flush=True,
                    )
                except OSError:
                    print(
                        "Abra o link acima no navegador, autorize e pressione Enter.",
                        flush=True,
                    )
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
