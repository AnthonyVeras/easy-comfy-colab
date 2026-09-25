#!/usr/bin/env bash
set -euo pipefail
PROFILE_ID="${COMFY_PROFILE_ID:-default}"
[[ "$PROFILE_ID" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || exit 1
pidfile="$HOME/.local/state/easy-comfy-colab/$PROFILE_ID/start.pid"
for _ in $(seq 1 30); do
  if [[ -s "$pidfile" ]]; then
    pid="$(cat "$pidfile")"
    if [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]]; then
      command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
      pgid="$(ps -o pgid= -p "$pid" | tr -d ' ')"
      if [[ "$command_line" == *'/start.sh'* && "$pgid" == "$pid" ]]; then
        kill -TERM -- "-$pid"
        echo 'Inicialização interrompida. Aguardando liberação da sessão.'
        exit 0
      fi
    fi
  fi
  sleep 0.2
done
echo 'Não foi encontrado um início ativo para cancelar.' >&2
exit 1
