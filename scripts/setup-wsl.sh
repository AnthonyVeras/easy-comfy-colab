#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT/app/environment.sh"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git openssh-client rsync curl util-linux
python3 -m venv "$VENV"
"$COLAB_PYTHON" -m pip install --disable-pip-version-check 'google-colab-cli==0.7.2'
bash "$PROJECT/app/install_hook.sh" "$PROJECT"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if [[ ! -f "$KEY" ]]; then
  ssh-keygen -q -t ed25519 -N '' -f "$KEY" -C easy-comfy-colab
fi
chmod 600 "$KEY"
echo 'WSL preparado. Abra o aplicativo para autorizar sua própria conta Google.'
