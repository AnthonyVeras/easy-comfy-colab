#!/usr/bin/env bash
# Apenas caminhos e isolamento local; nenhuma credencial distribuída.
umask 077
PROFILE_ID="${COMFY_PROFILE_ID:-default}"
[[ "$PROFILE_ID" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || { echo 'Perfil inválido.' >&2; return 1; }
RUNTIME_ROOT="$HOME/.local/share/easy-comfy-colab"
VENV="$RUNTIME_ROOT/venv"
COLAB="$VENV/bin/colab"
COLAB_PYTHON="$VENV/bin/python"
KEY="$HOME/.ssh/easy_comfy_colab_ed25519"
STATE="$HOME/.local/state/easy-comfy-colab/$PROFILE_ID"
SESSION=comfy-colab
export COMFY_COLAB_TOKEN_PATH="$RUNTIME_ROOT/profiles/$PROFILE_ID/token.json"
export COMFY_COLAB_CONFIG_PATH="$RUNTIME_ROOT/profiles/$PROFILE_ID/sessions.json"
COLAB_FLAGS=(--config "$COMFY_COLAB_CONFIG_PATH")
KNOWN="$STATE/known_hosts"
mkdir -p "$STATE" "$RUNTIME_ROOT/profiles/$PROFILE_ID"
