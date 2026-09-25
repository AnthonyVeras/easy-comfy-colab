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
MEDIA_ROOT="$LOCAL_DATA"
[[ -z "${COMFY_MEDIA_ROOT:-}" ]] || MEDIA_ROOT="$(wslpath -a "$COMFY_MEDIA_ROOT")"

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
  MODE=comfy
  [[ "$session_status" != *"Hardware: CPU"* ]] || MODE=downloads
  if ! transport_ready; then
    open_transport || { echo 'Reconexão falhou; VM mantida para preservar os outputs.' >&2; exit 1; }
  fi
  mkdir -p "$MEDIA_ROOT/output" "$LOCAL_DATA/user"
  echo 'Sincronizando resultados e workflows com o notebook (até 60 segundos)...'
  mkdir -p "$LOCAL_DATA/user/default/workflows"
  printf -v sync_ssh '%q ' ssh "${ssh_options[@]}"
  sync_local() {
    timeout 30 rsync -rt --update --partial --timeout=15 -e "$sync_ssh" \
      "root@colab:/content/drive/MyDrive/ComfyColab/$1/" "$LOCAL_DATA/$1/"
  }
  sync_local user/default/workflows || echo 'Workflows completos permanecem no Google Drive.' >&2
  # Even after switching back to Drive, do not abandon unsaved PC outputs.
  pc_outputs="$(ssh "${ssh_options[@]}" root@colab 'test ! -d /content/comfy-colab/output-pc || echo yes')"
  if [[ "$pc_outputs" == yes ]]; then
    source "$PROJECT/app/output_control.sh"
    paused_pid="$(ssh "${ssh_options[@]}" root@colab 'python3 /content/comfy-colab/output_storage.py pause')"
    [[ "$paused_pid" =~ ^[0-9]+$ ]] || { echo 'Não foi possível preparar a cópia final. VM mantida.' >&2; exit 1; }
    trap 'ssh "${ssh_options[@]}" root@colab "kill -CONT $paused_pid" >/dev/null 2>&1 || true' EXIT
    echo 'Copiando e verificando outputs no PC antes de encerrar...'
    if ! sync_pc_outputs final; then
      echo 'Cópia de outputs falhou. VM mantida ligada; reconecte e tente novamente.' >&2
      exit 1
    fi
  fi
  active_output="$(ssh "${ssh_options[@]}" root@colab 'cat /content/comfy-colab/output-mode 2>/dev/null || echo drive')"
  if [[ "$active_output" == drive ]]; then
    timeout 30 rsync -rt --update --partial --timeout=15 -e "$sync_ssh" root@colab:/content/drive/MyDrive/ComfyColab/output/ "$MEDIA_ROOT/output/" || echo 'Cópia local incompleta; resultados permanecem no Drive.' >&2
  fi
  echo 'Encerrando sessão Colab...'
  "$COLAB" "${COLAB_FLAGS[@]}" stop -s "$SESSION"
  trap - EXIT
  close_transport
  "$COLAB_PYTHON" "$PROJECT/app/assignment_guard.py" release "$ALLOCATION_MARKER"
else
  close_transport
  "$COLAB_PYTHON" "$PROJECT/app/assignment_guard.py" release "$ALLOCATION_MARKER"
  echo 'Nenhuma sessão Comfy Colab ativa.'
fi
