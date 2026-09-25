#!/usr/bin/env bash
# Um único transporte Colab; comandos, cópias e túneis compartilham o master.
# Requer STATE, KEY, KNOWN, COLAB, COLAB_FLAGS e SESSION definidos pelo chamador.
SSH_CONTROL="$STATE/ssh-control"
ssh_base_options=(
  -i "$KEY"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o ConnectTimeout=30
  -o ServerAliveInterval=20
  -o ServerAliveCountMax=3
  -o StrictHostKeyChecking=accept-new
  -o UserKnownHostsFile="$KNOWN"
  -o HostKeyAlias="$SESSION"
)
# ProxyCommand=false impede que a queda do master abra outra sessão sem querer.
ssh_options=("${ssh_base_options[@]}" -o ControlPath="$SSH_CONTROL" -o ControlMaster=no -o ProxyCommand=false)

transport_ready() {
  ssh "${ssh_options[@]}" -O check root@colab >/dev/null 2>&1
}

close_transport() {
  ssh "${ssh_options[@]}" -O exit root@colab >/dev/null 2>&1 || true
  if [[ -f "$STATE/tunnel.pid" ]]; then
    local pid command_line
    pid="$(cat "$STATE/tunnel.pid")"
    if [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]]; then
      command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
      if [[ "$command_line" == *'ssh '* && "$command_line" == *'127.0.0.1:18188:127.0.0.1:8188'* ]]; then
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.2
        done
      fi
    fi
    rm -f "$STATE/tunnel.pid"
  fi
  if ! transport_ready; then rm -f "$SSH_CONTROL"; fi
}

open_transport() {
  local attempt pid proxy_command
  printf -v proxy_command '%q ' "$COLAB" "${COLAB_FLAGS[@]}" ssh --proxy-mode -s "$SESSION" -i "$KEY"
  local forwards=(-L 127.0.0.1:18188:127.0.0.1:8188)
  [[ "$MODE" == downloads ]] || forwards+=(-L 127.0.0.1:18189:127.0.0.1:8189)
  for attempt in 1 2 3 4; do
    echo "Conectando SSH compartilhado à VM (tentativa $attempt/4)..."
    nohup ssh "${ssh_base_options[@]}" -M -S "$SSH_CONTROL" -N \
      -o ControlPersist=no -o ExitOnForwardFailure=yes \
      -o "ProxyCommand=$proxy_command" \
      "${forwards[@]}" root@colab \
      >"$STATE/tunnel.log" 2>&1 </dev/null 9>&- &
    pid=$!
    printf '%s\n' "$pid" > "$STATE/tunnel.pid"
    for _ in $(seq 1 90); do
      if transport_ready; then return 0; fi
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    cat "$STATE/tunnel.log" >&2
    close_transport
    if (( attempt < 4 )) && grep -q 'Already-active SSH session\|HTTP 429' "$STATE/tunnel.log"; then
      echo 'O Colab ainda está liberando o SSH anterior; aguardando para reconectar...'
      sleep "$((attempt * 3))"
    else
      return 1
    fi
  done
  return 1
}
