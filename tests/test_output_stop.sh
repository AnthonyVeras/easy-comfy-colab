#!/usr/bin/env bash
# A failed final transfer must neither stop the VM nor leave ComfyUI paused.
set -euo pipefail
project="$(cd "$(dirname "$0")/.." && pwd)"
fixture="$(mktemp -d /tmp/comfy-output-test.XXXXXX)"
export FIXTURE="$fixture"
mkdir -p "$fixture/app" "$fixture/bin" "$fixture/state"
cp "$project/stop.sh" "$fixture/stop.sh"
cp "$project/app/output_control.sh" "$fixture/app/output_control.sh"
cat > "$fixture/app/environment.sh" <<'SH'
PROFILE_ID=default
STATE="$FIXTURE/state"
COLAB="$FIXTURE/bin/colab"
COLAB_PYTHON=true
COLAB_FLAGS=()
SESSION=comfy-colab
SH
cat > "$fixture/app/ssh_transport.sh" <<'SH'
ssh_options=()
transport_ready() { return 0; }
open_transport() { return 0; }
close_transport() { touch "$FIXTURE/closed"; }
SH
cat > "$fixture/bin/colab" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == status ]]; then echo 'Hardware: A100'; else touch "$FIXTURE/stopped"; fi
SH
cat > "$fixture/bin/ssh" <<'SH'
#!/usr/bin/env bash
case "$*" in
  *'output_storage.py pause'*) echo 123 ;;
  *'kill -CONT 123'*) touch "$FIXTURE/resumed" ;;
  *'test ! -d /content/comfy-colab/output-pc'*) echo yes ;;
  *'cat /content/comfy-colab/output-mode'*) echo pc ;;
esac
SH
cat > "$fixture/bin/rsync" <<'SH'
#!/usr/bin/env bash
if [[ "$*" == *'output-pc/'* ]]; then
  [[ "$*" == *'--checksum'* ]] || exit 2
  [[ "${FAIL_COPY:-1}" == 0 ]] || exit 17
  touch "$FIXTURE/copied"
fi
SH
chmod +x "$fixture/bin/"*
export PATH="$fixture/bin:$PATH"
unset COMFY_MEDIA_ROOT
if bash "$fixture/stop.sh" >"$fixture/failure.log" 2>&1; then
  echo 'FAIL: stop should fail when copying fails'; exit 1
fi
test ! -f "$fixture/stopped"
test ! -f "$fixture/closed"
test -f "$fixture/resumed"
FAIL_COPY=0 bash "$fixture/stop.sh" >"$fixture/success.log" 2>&1
test -f "$fixture/copied"
test -f "$fixture/stopped"
test -f "$fixture/closed"
echo 'PASS: copy failure preserves VM and resumes server; verified copy allows stop.'
