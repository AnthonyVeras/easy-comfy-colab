#!/usr/bin/env bash
# Simula o limite de uma conexão Colab, sem alocar uma VM.
set -euo pipefail
project="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d /tmp/comfy-ssh-test.XXXXXX)"
export FAKE_ROOT="$work"
mkdir "$work/bin" "$work/state"
cat > "$work/bin/ssh" <<'SH'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$FAKE_ROOT/calls"
master=0
op=''
args=("$@")
for ((i=0;i<${#args[@]};i++)); do
  [[ "${args[i]}" != -M ]] || master=1
  [[ "${args[i]}" != -O ]] || op="${args[i+1]}"
done
if ((master)); then
  if [[ ! -f "$FAKE_ROOT/attempted" ]]; then
    touch "$FAKE_ROOT/attempted"
    echo 'Already-active SSH session (HTTP 429)' >&2
    exit 255
  fi
  [[ ! -f "$FAKE_ROOT/master" ]] || exit 255
  echo $$ > "$FAKE_ROOT/master"
  trap 'rm -f "$FAKE_ROOT/master"; exit 0' TERM
  while :; do sleep 0.1; done
fi
[[ "$*" == *'ControlPath='* && "$*" == *'ProxyCommand=false'* ]] || exit 255
[[ -f "$FAKE_ROOT/master" ]] || exit 255
kill -0 "$(cat "$FAKE_ROOT/master")" 2>/dev/null || exit 255
if [[ "$op" == exit ]]; then kill "$(cat "$FAKE_ROOT/master")"; fi
SH
chmod +x "$work/bin/ssh"
export PATH="$work/bin:$PATH"
STATE="$work/state"
KEY="$work/key"
KNOWN="$work/known"
COLAB=/fake/colab
COLAB_FLAGS=()
SESSION=comfy-colab
MODE=comfy
source "$project/app/ssh_transport.sh"
cleanup() {
  close_transport
  rm -f "$work/bin/ssh" "$work/calls" "$work/attempted" "$work/master" "$work/state/tunnel.log"
  rmdir "$work/bin" "$work/state" "$work"
}
trap cleanup EXIT
open_transport
ssh "${ssh_options[@]}" root@colab 'read-smoke-marker'
ssh "${ssh_options[@]}" root@colab 'write-smoke-marker'
transport_ready
[[ $(grep -c -- ' -M ' "$work/calls") == 2 ]]
close_transport
if transport_ready; then echo 'FAIL: conexão ficou ativa' >&2; exit 1; fi
echo 'PASS: recuperou 429 e compartilhou um único master para comandos e portas.'
