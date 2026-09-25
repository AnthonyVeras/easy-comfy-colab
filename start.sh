#!/usr/bin/env bash
set -Eeuo pipefail

# Grupo próprio: cancelar o início também termina SSH, instalação e autorização.
if [[ "${COMFY_START_GROUP:-0}" != 1 ]]; then
  exec env COMFY_START_GROUP=1 setsid --wait bash "$0" "$@"
fi

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT/app/environment.sh"
ALLOCATION_MARKER="$STATE/allocation.json"
GPU="${COMFY_GPU:-A100}"
OUTPUT_MODE="${COMFY_OUTPUT_MODE:-drive}"
[[ "$OUTPUT_MODE" == pc || "$OUTPUT_MODE" == drive ]] || { echo "Destino de outputs inválido." >&2; exit 1; }
SECONDS=0
MODE=comfy
[[ "$GPU" != CPU ]] || MODE=downloads
[[ "$GPU" =~ ^(G4|A100|L4|T4|CPU)$ ]] || { echo "Hardware inválido." >&2; exit 1; }
if [[ "$PROFILE_ID" == default ]]; then
  LOCAL_DATA="$PROJECT"
else
  LOCAL_DATA="$PROJECT/accounts/$PROFILE_ID"
fi
MEDIA_ROOT="$LOCAL_DATA"
[[ -z "${COMFY_MEDIA_ROOT:-}" ]] || MEDIA_ROOT="$(wslpath -a "$COMFY_MEDIA_ROOT")"
mkdir -p "$STATE" "$MEDIA_ROOT/input" "$MEDIA_ROOT/output" "$PROJECT/custom_nodes" "$LOCAL_DATA/user"
exec 9>"$STATE/start.lock"
if ! flock -n 9; then
  echo 'Já existe uma inicialização em andamento para esta conta.' >&2
  exit 1
fi
printf '%s\n' "$$" > "$STATE/start.pid"
trap 'rm -f "$STATE/start.pid"' EXIT
trap 'exit 130' TERM INT

if [[ ! -x "$COLAB" || ! -f "$KEY" ]]; then
  echo 'Colab CLI ou chave SSH ausente no Ubuntu WSL.' >&2
  exit 1
fi

source "$PROJECT/app/ssh_transport.sh"

copy_to_vm() {
  local attempt
  for attempt in 1 2 3; do
    if scp -r "${ssh_options[@]}" "$1" "$2"; then
      return 0
    fi
    if (( attempt < 3 )); then
      echo "A conexão SSH para a cópia falhou; nova tentativa em 8 segundos ($attempt/3)..." >&2
      sleep 8
    fi
  done
  return 1
}

if transport_ready; then
  if curl --silent --fail --max-time 5 http://127.0.0.1:18188/system_stats >/dev/null && \
     { [[ "$MODE" == downloads ]] || python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 18189), 2); s.close()' 2>/dev/null; }; then
    active_output="$(ssh "${ssh_options[@]}" root@colab 'cat /content/comfy-colab/output-mode 2>/dev/null || echo drive')"
    [[ "$active_output" == "$OUTPUT_MODE" ]] || { echo 'A sessão ativa usa outro destino. A preferência será aplicada na próxima sessão.' >&2; exit 1; }
    echo 'ComfyUI já está aberto em http://127.0.0.1:18188/'
    echo 'Comfy MCP já está acessível em http://127.0.0.1:18189/mcp'
    exit 0
  fi
fi
close_transport


CREATED_SESSION=0
cleanup_on_error() {
  result=$?
  trap - EXIT
  rm -f "$STATE/start.pid"
  if (( result != 0 )); then
    close_transport
    if (( CREATED_SESSION )); then
      echo 'Falha na inicialização; encerrando a nova sessão para evitar consumo de créditos.' >&2
      "$COLAB" "${COLAB_FLAGS[@]}" stop -s "$SESSION" >/dev/null 2>&1 || true
    fi
    "$COLAB_PYTHON" "$PROJECT/app/assignment_guard.py" release "$ALLOCATION_MARKER" || true
  fi
}
trap cleanup_on_error EXIT

session_status="$("$COLAB" "${COLAB_FLAGS[@]}" status -s "$SESSION")"
if [[ "$session_status" == *"not found"* ]]; then
  echo "Criando sessão $SESSION com GPU $GPU..."
  "$COLAB_PYTHON" "$PROJECT/app/assignment_guard.py" begin "$ALLOCATION_MARKER" "$GPU"
  CREATED_SESSION=1
  hardware_args=()
  [[ "$GPU" == CPU ]] || hardware_args=(--gpu "$GPU")
  "$COLAB" "${COLAB_FLAGS[@]}" new -s "$SESSION" "${hardware_args[@]}"
  "$COLAB_PYTHON" "$PROJECT/app/assignment_guard.py" clear "$ALLOCATION_MARKER"
  ssh-keygen -q -R "$SESSION" -f "$KNOWN" >/dev/null 2>&1 || true
fi

# A reconexão segue o hardware da VM existente, mesmo se a preferência mudou.
if [[ "$session_status" != *"not found"* ]]; then
  if [[ "$session_status" == *"Hardware: CPU"* ]]; then
    MODE=downloads
  else
    MODE=comfy
  fi
fi

echo 'Abrindo conexão privada com a VM...'
open_transport

for drive_attempt in 1 2; do
  if ssh "${ssh_options[@]}" root@colab 'test -d /content/drive/MyDrive'; then
    break
  fi
  echo "Montando Google Drive (tentativa $drive_attempt/2)..."
  python3 "$PROJECT/mount_drive.py" "$COLAB" "$SESSION"
  if ssh "${ssh_options[@]}" root@colab 'test -d /content/drive/MyDrive'; then
    break
  fi
  echo 'O DriveFS não montou; verificando o diagnóstico na VM.' >&2
  ssh "${ssh_options[@]}" root@colab \
    'find /root/.config/Google/DriveFS/Logs -name drive_fs.txt -type f -exec tail -n 20 {} \; 2>/dev/null || true' \
    >&2 || true
done
if ! ssh "${ssh_options[@]}" root@colab 'test -d /content/drive/MyDrive'; then
  echo 'Google Drive indisponível após duas tentativas.' >&2
  exit 1
fi

# Conexão reaproveitada e sincronização incremental: nunca apaga arquivos remotos.
command -v rsync >/dev/null || { echo 'Instale rsync no Ubuntu WSL.' >&2; exit 1; }
ssh "${ssh_options[@]}" root@colab 'command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync)'
{
  echo '#!/usr/bin/env bash'
  declare -p ssh_options
  echo 'exec ssh "${ssh_options[@]}" "$@"'
} > "$STATE/ssh-wrapper.sh"
chmod 700 "$STATE/ssh-wrapper.sh"
sync_dir() {
  rsync -rt --update --exclude __pycache__ --exclude .git -e "$STATE/ssh-wrapper.sh" "$1/" "root@colab:$2/"
}
if [[ "$MODE" == downloads ]]; then
  echo 'Preparando modo CPU para downloads...'
  ssh "${ssh_options[@]}" root@colab 'mkdir -p /content/comfy-colab /content/drive/MyDrive/ComfyColab/models /content/drive/MyDrive/ComfyColab/output /content/drive/MyDrive/ComfyColab/user/default/workflows; python3 -m pip install -q aiohttp requests psutil'
  copy_to_vm "$PROJECT/custom_nodes/comfy_colab_remote_download/service.py" root@colab:/content/comfy-colab/service.py
  copy_to_vm "$PROJECT/remote/download_server.py" root@colab:/content/comfy-colab/download_server.py
  ssh "${ssh_options[@]}" root@colab 'if ! curl -sf http://127.0.0.1:8188/system_stats >/dev/null; then nohup python3 /content/comfy-colab/download_server.py >/content/comfy-colab/downloads.log 2>&1 </dev/null & echo $! >/content/comfy-colab/downloads.pid; fi'
else
  echo 'Instalando ComfyUI-Easy-Install e seus nodes no Colab...'
  ssh "${ssh_options[@]}" root@colab 'bash -s' < "$PROJECT/remote/install.sh"
  echo 'Sincronizando custom nodes locais...'
  rsync -rtc --exclude __pycache__ --exclude .git -e "$STATE/ssh-wrapper.sh" "$PROJECT/custom_nodes/" root@colab:/content/comfy-colab/ComfyUI-Easy-Install/ComfyUI/custom_nodes/
  echo 'Enviando entradas e workflows alterados...'
  sync_dir "$MEDIA_ROOT/input" /content/drive/MyDrive/ComfyColab/input
  sync_dir "$LOCAL_DATA/user" /content/drive/MyDrive/ComfyColab/user
  copy_to_vm "$PROJECT/remote/output_storage.py" root@colab:/content/comfy-colab/output_storage.py
  echo 'Iniciando servidor ComfyUI...'
  ssh "${ssh_options[@]}" root@colab "COMFY_OUTPUT_MODE=$OUTPUT_MODE bash -s" < "$PROJECT/remote/run.sh"
  echo 'Preparando Comfy MCP oficial na VM...'
  copy_to_vm "$PROJECT/remote/mcp_http.py" root@colab:/content/comfy-colab/mcp_http.py
  ssh "${ssh_options[@]}" root@colab 'python3 /content/comfy-colab/mcp_http.py'
fi

echo 'Verificando os serviços pela conexão compartilhada...'

for _ in $(seq 1 60); do
  if curl --silent --fail --max-time 5 http://127.0.0.1:18188/system_stats > "$STATE/system_stats.json" && \
     { [[ "$MODE" == downloads ]] || python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 18189), 2); s.close()' 2>/dev/null; }; then
    if [[ "$MODE" == comfy ]]; then
      revision="$(sha256sum "$PROJECT/remote/install.sh" | cut -d' ' -f1)"
      tested="$(ssh "${ssh_options[@]}" root@colab 'cat /content/comfy-colab/smoke.ok 2>/dev/null || true')"
      if [[ "$tested" != "$revision" || "${COMFY_FULL_TEST:-0}" == 1 ]]; then
        python3 "$PROJECT/smoke_test.py"
        printf '%s' "$revision" | ssh "${ssh_options[@]}" root@colab 'cat > /content/comfy-colab/smoke.ok'
      fi
    fi
    echo "Inicialização concluída em ${SECONDS}s (modo $MODE)."
    if [[ "$MODE" == comfy ]]; then
      echo 'ComfyUI pronto em http://127.0.0.1:18188/'
      echo 'Comfy MCP pronto em http://127.0.0.1:18189/mcp'
    else
      echo 'Serviço de downloads em CPU pronto.'
    fi
    rm -f "$STATE/start.pid"
    trap - EXIT
    exit 0
  fi
  if ! kill -0 "$(cat "$STATE/tunnel.pid")" 2>/dev/null; then
    cat "$STATE/tunnel.log" >&2
    exit 1
  fi
  sleep 2
done

echo 'O servidor não respondeu pelo túnel SSH.' >&2
cat "$STATE/tunnel.log" >&2
exit 1
