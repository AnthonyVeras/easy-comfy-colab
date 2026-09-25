#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT/app/environment.sh"
ALLOCATION_MARKER="$STATE/allocation.json"
if [[ "$PROFILE_ID" == default ]]; then
  LOCAL_DATA="$PROJECT"
else
  LOCAL_DATA="$PROJECT/accounts/$PROFILE_ID"
fi
mkdir -p "$STATE"

if [[ -s "$STATE/start.pid" ]]; then
  bash "$PROJECT/app/cancel_start.sh" || true
  # O início precisa concluir a liberação antes de encerrar uma eventual sessão restante.
  if ! timeout 90 flock "$STATE/start.lock" true; then
    echo 'A inicialização ainda está encerrando. Tente encerrar novamente em alguns segundos.' >&2
    exit 1
  fi
fi

source "$PROJECT/app/ssh_transport.sh"

session_status="$("$COLAB" "${COLAB_FLAGS[@]}" status -s "$SESSION")"
if [[ "$session_status" != *"not found"* ]]; then
  mkdir -p "$LOCAL_DATA/output" "$LOCAL_DATA/user"
  echo 'Sincronizando resultados e workflows com o notebook (até 60 segundos)...'
  mkdir -p "$LOCAL_DATA/user/default/workflows"
  printf -v sync_ssh '%q ' ssh "${ssh_options[@]}"
  sync_local() {
    timeout 30 rsync -rt --update --partial --timeout=15 -e "$sync_ssh" \
      "root@colab:/content/drive/MyDrive/ComfyColab/$1/" "$LOCAL_DATA/$1/"
  }
  sync_local output || echo 'Cópia local incompleta; resultados completos permanecem no Google Drive.' >&2
  sync_local user/default/workflows || echo 'Workflows completos permanecem no Google Drive.' >&2
  close_transport
  echo 'Encerrando sessão Colab...'
  "$COLAB" "${COLAB_FLAGS[@]}" stop -s "$SESSION"
  "$COLAB_PYTHON" "$PROJECT/app/assignment_guard.py" release "$ALLOCATION_MARKER"
else
  close_transport
  "$COLAB_PYTHON" "$PROJECT/app/assignment_guard.py" release "$ALLOCATION_MARKER"
  echo 'Nenhuma sessão Comfy Colab ativa.'
fi
