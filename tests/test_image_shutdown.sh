#!/usr/bin/env bash
# Integração inteiramente simulada: nenhuma conexão com Colab ou Drive real.
set -euo pipefail
project="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d /tmp/comfy-image-stop.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
export FAKE_ROOT="$work"
mkdir -p "$work/project/app" "$work/project/remote" "$work/bin" "$work/state"
cp "$project/stop.sh" "$work/project/"
cp "$project/app/runtime_image.sh" "$project/app/output_control.sh" "$work/project/app/"
touch "$work/project/remote/"{install.sh,runtime_image.py,mcp_http.py}
cat > "$work/project/app/environment.sh" <<'SH'
PROFILE_ID=default
STATE="$FAKE_ROOT/state"
COLAB="$FAKE_ROOT/bin/colab"
COLAB_PYTHON=true
COLAB_FLAGS=()
SESSION=test
SH
cat > "$work/project/app/ssh_transport.sh" <<'SH'
ssh_options=()
transport_ready() { return 0; }
close_transport() { echo close >> "$FAKE_ROOT/events"; }
open_transport() { echo reconnect >> "$FAKE_ROOT/events"; }
SH
cat > "$work/bin/colab" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == status ]]; then echo "Hardware: ${TEST_GPU:-L4}"; else echo stop >> "$FAKE_ROOT/events"; fi
SH
cat > "$work/bin/ssh" <<'SH'
#!/usr/bin/env bash
case "$*" in
  *'echo installed'*) [[ "${TEST_STATE_FAIL:-0}" == 0 ]] || exit 255; echo installed;;
  *'runtime_image.py build'*) echo image >> "$FAKE_ROOT/events"; exit "${TEST_IMAGE_FAIL:-0}";;
  *'test ! -d /content/comfy-colab/output-pc'*) [[ "${TEST_PC:-0}" != 1 ]] || echo yes;;
  *'output_storage.py pause'*) echo pause >> "$FAKE_ROOT/events"; echo 123;;
  *'kill -CONT'*) echo resume >> "$FAKE_ROOT/events";;
  *'cat /content/comfy-colab/output-mode'*) echo drive;;
esac
exit 0
SH
cat > "$work/bin/rsync" <<'SH'
#!/usr/bin/env bash
case "$*" in *output-pc*) echo outputs >> "$FAKE_ROOT/events"; exit "${TEST_OUTPUT_FAIL:-0}";; esac
exit 0
SH
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/bin/scp"
chmod +x "$work/bin/"*
export PATH="$work/bin:$PATH"
run_case() { : > "$work/events"; bash "$work/project/stop.sh" > "$work/log" 2>&1; }
export TEST_IMAGE_FAIL=1
if run_case; then echo 'FAIL: desligamento aceitou falha da imagem'; exit 1; fi
[[ "$(cat "$work/events")" == image ]]
export TEST_IMAGE_FAIL=0
run_case
[[ "$(head -n 1 "$work/events")" == image ]]
grep -q '^stop$' "$work/events"
export TEST_STATE_FAIL=1
if run_case; then echo 'FAIL: desligamento aceitou estado SSH desconhecido'; exit 1; fi
! grep -q '^stop$' "$work/events"
export TEST_STATE_FAIL=0 TEST_PC=1 TEST_OUTPUT_FAIL=1
if run_case; then echo 'FAIL: desligamento aceitou perda de outputs'; exit 1; fi
grep -q '^resume$' "$work/events"
! grep -q '^stop$' "$work/events"
export TEST_OUTPUT_FAIL=0 TEST_PC=0 TEST_GPU=CPU
run_case
! grep -q '^image$' "$work/events"
grep -q '^stop$' "$work/events"
: > "$work/events"
bash "$work/project/app/runtime_image.sh" > "$work/log" 2>&1
[[ "$(cat "$work/events")" == image ]]
(
  exec 9>"$work/state/runtime-image.lock"
  flock -n 9
  if bash "$work/project/app/runtime_image.sh" > "$work/log" 2>&1; then
    echo 'FAIL: aceitou duas atualizações ao mesmo tempo'; exit 1
  fi
)
echo 'PASS: botão manual, imagem antes de stop, CPU e proteção de outputs; nenhuma VM real usada.'
