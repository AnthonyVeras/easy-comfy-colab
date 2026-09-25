#!/usr/bin/env bash
# Integração local: o cancelador deve encerrar também os filhos do início.
set -euo pipefail
project="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d /tmp/comfy-cancel-test.XXXXXX)"
state="$HOME/.local/state/easy-comfy-colab/qa_v2_cancel"
mkdir -p "$state"
cleanup() {
  [[ ! -f "$work/child" ]] || kill "$(cat "$work/child")" 2>/dev/null || true
  rm -f "$state/start.pid" "$work/start.sh" "$work/child"
  rmdir "$work" "$state" 2>/dev/null || true
}
trap cleanup EXIT
cat > "$work/start.sh" <<'SH'
#!/usr/bin/env bash
trap 'exit 130' TERM INT
echo $$ > "$1/start.pid"
sleep 120 &
echo $! > "$2/child"
wait
SH
setsid bash "$work/start.sh" "$state" "$work" &
launcher=$!
for _ in $(seq 1 30); do [[ ! -s "$work/child" ]] || break; sleep 0.1; done
COMFY_PROFILE_ID=qa_v2_cancel bash "$project/app/cancel_start.sh"
wait "$launcher" || true
for _ in $(seq 1 30); do
  if ! kill -0 "$(cat "$work/child")" 2>/dev/null; then echo 'PASS: início e processo filho encerrados.'; exit 0; fi
  sleep 0.1
done
echo 'FAIL: filho da inicialização continua ativo.' >&2
exit 1
