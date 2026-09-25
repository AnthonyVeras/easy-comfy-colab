#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="${1:?Informe o caminho do projeto no WSL}"
VENV="$HOME/.local/share/easy-comfy-colab/venv"
SITE="$($VENV/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"

install -m 644 "$PROJECT/app/colab_auth_hook.py" "$SITE/comfy_colab_auth_hook.py"
printf 'import comfy_colab_auth_hook\n' > "$SITE/comfy_colab_auth_hook.pth"

$VENV/bin/python -c 'import comfy_colab_auth_hook'
