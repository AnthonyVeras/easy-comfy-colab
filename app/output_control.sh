#!/usr/bin/env bash
# Functions also sourced by stop.sh, using its existing SSH master.
sync_pc_outputs() {
  local checksum=()
  [[ "${1:-}" != final ]] || checksum=(--checksum)
  local destination="${MEDIA_ROOT:-$LOCAL_DATA}/output"
  mkdir -p "$destination"
  # ponytail: scan the output tree; use a manifest if very large libraries slow this down.
  (
    flock -w 180 8 || { echo 'Outra cópia de outputs ainda está em andamento.' >&2; exit 1; }
    printf -v output_ssh '%q ' ssh "${ssh_options[@]}"
    rsync -rt --partial --delay-updates --timeout=30 "${checksum[@]}" -e "$output_ssh" \
      root@colab:/content/comfy-colab/output-pc/ "$destination/"
  ) 8>"$STATE/output-sync.lock"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -Eeuo pipefail
  PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
  source "$PROJECT/app/environment.sh"
  source "$PROJECT/app/ssh_transport.sh"
  LOCAL_DATA="$PROJECT"
  [[ "$PROFILE_ID" == default ]] || LOCAL_DATA="$PROJECT/accounts/$PROFILE_ID"
  MEDIA_ROOT="$LOCAL_DATA"
  [[ -z "${COMFY_MEDIA_ROOT:-}" ]] || MEDIA_ROOT="$(wslpath -a "$COMFY_MEDIA_ROOT")"
  transport_ready || { echo 'Sem conexão: outputs ainda estão na VM.' >&2; exit 1; }
  if [[ "${1:-sync}" == restart ]]; then
    [[ "${COMFY_OUTPUT_MODE:-drive}" == pc || "${COMFY_OUTPUT_MODE:-drive}" == drive ]] || exit 1
    scp "${ssh_options[@]}" "$PROJECT/remote/output_storage.py" "$PROJECT/remote/restart.py" root@colab:/content/comfy-colab/
    scp "${ssh_options[@]}" "$PROJECT/custom_nodes/comfy_colab_remote_download/service.py" root@colab:/content/comfy-colab/ComfyUI-Easy-Install/ComfyUI/custom_nodes/comfy_colab_remote_download/service.py
    exec ssh "${ssh_options[@]}" root@colab "python3 /content/comfy-colab/restart.py ${COMFY_OUTPUT_MODE:-drive}"
  fi
  # A successful idle check avoids copying a video/image still being written.
  ready="$(ssh "${ssh_options[@]}" root@colab "python3 -c 'import json; from urllib.request import urlopen; q=json.load(urlopen(\"http://127.0.0.1:8188/queue\",timeout=5)); print(\"idle\" if set(q) >= {\"queue_running\",\"queue_pending\"} and not q[\"queue_running\"] and not q[\"queue_pending\"] else \"busy\")'")"
  [[ "$ready" == idle ]] || { echo 'Aguardando a fila para copiar outputs.'; exit 0; }
  exists="$(ssh "${ssh_options[@]}" root@colab 'test ! -d /content/comfy-colab/output-pc || echo yes')"
  [[ "$exists" == yes ]] || exit 0
  sync_pc_outputs
  echo 'Outputs copiados para o PC.'
fi
