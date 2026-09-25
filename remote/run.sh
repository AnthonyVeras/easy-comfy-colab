#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/content/comfy-colab
INSTALL="$ROOT/ComfyUI-Easy-Install"
COMFY="$INSTALL/ComfyUI"
DRIVE=/content/drive/MyDrive/ComfyColab
PYTHON="$INSTALL/.venv/bin/python"
PIDFILE="$ROOT/comfyui.pid"
LOG="$ROOT/comfyui.log"

if [[ ! -x "$PYTHON" || ! -f "$COMFY/main.py" || ! -d "$DRIVE/models" ]]; then
  echo 'A instalação do Easy Install ou a montagem do Drive está incompleta.' >&2
  exit 1
fi

if curl --silent --fail http://127.0.0.1:8188/system_stats >/dev/null; then
  active_output="$(cat "$ROOT/output-mode" 2>/dev/null || echo drive)"
  [[ "$active_output" == "${COMFY_OUTPUT_MODE:-drive}" ]] || { echo 'Use Reiniciar ComfyUI para aplicar o novo destino de outputs.' >&2; exit 1; }
  echo 'Servidor ComfyUI já está ativo.'
  exit 0
fi

OUTPUT="$(python3 "$ROOT/output_storage.py" "${COMFY_OUTPUT_MODE:-drive}")"
cd "$COMFY"
nohup "$PYTHON" main.py \
  --listen 127.0.0.1 \
  --port 8188 \
  --enable-manager \
  --input-directory "$DRIVE/input" \
  --output-directory "$OUTPUT" \
  --user-directory "$DRIVE/user" \
  > "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

for _ in $(seq 1 120); do
  if curl --silent --fail http://127.0.0.1:8188/system_stats >/dev/null; then
    echo 'Servidor ComfyUI responde em 127.0.0.1:8188.'
    exit 0
  fi
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    tail -n 100 "$LOG" >&2
    exit 1
  fi
  sleep 2
done

tail -n 100 "$LOG" >&2
exit 1
