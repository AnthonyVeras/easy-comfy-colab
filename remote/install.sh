#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/content/comfy-colab
SOURCE="$ROOT/easy-install-source"
INSTALL="$ROOT/ComfyUI-Easy-Install"
COMFY="$INSTALL/ComfyUI"
DRIVE=/content/drive/MyDrive/ComfyColab
REPO=https://github.com/Tavris1/ComfyUI-Easy-Install.git
REVISION=2a979fae03ac6c4adc607634b0ef8432e80ecc3e
COMFY_REVISION=1568e6cfd04586a4b3c4e1817ea7dde09b1bf9e7
LOG="$ROOT/install.log"
export PIP_CACHE_DIR="$DRIVE/.cache/pip"

# A versão atual de ComfyUI-GGUF reconhece Qwen Image 2.1 ao converter,
# mas omite o identificador gravado no GGUF da lista aceita pelo loader.
patch_gguf_qwen21() {
  local loader="$COMFY/custom_nodes/ComfyUI-GGUF/loader.py"
  [[ -f "$loader" ]] || return 0
  python3 - "$loader" <<'PY'
from pathlib import Path
import sys

loader = Path(sys.argv[1])
lines = loader.read_text().splitlines(keepends=True)
for index, line in enumerate(lines):
    if line.startswith('IMG_ARCH_LIST = '):
        if '"qwen_image21"' in line:
            break
        needle = '"qwen_image"}'
        if needle not in line:
            raise SystemExit('Formato da lista IMG_ARCH_LIST mudou; correção GGUF não aplicada.')
        lines[index] = line.replace(needle, '"qwen_image", "qwen_image21"}')
        loader.write_text(''.join(lines))
        print('ComfyUI-GGUF: qwen_image21 habilitado.')
        break
else:
    raise SystemExit('IMG_ARCH_LIST não encontrada no ComfyUI-GGUF.')
PY
}

mkdir -p "$ROOT" "$INSTALL"
exec > >(tee -a "$LOG") 2>&1

if [[ ! -d /content/drive/MyDrive ]]; then
  echo 'Google Drive não está montado em /content/drive/MyDrive.' >&2
  exit 1
fi

# Algumas VMs recentes do Colab anunciam a A100 e expõem /dev/nvidia*, mas
# omitem as bibliotecas de usuário do driver. Instala a versão exata do módulo.
if ! python3 -c 'import torch; assert torch.cuda.is_available()' >/dev/null 2>&1; then
  driver="$(grep -oE '[0-9]{3}\.[0-9]{2}\.[0-9]{2}' /proc/driver/nvidia/version | head -n 1)"
  major="${driver%%.*}"
  package_version="$(apt-cache policy "libnvidia-compute-$major" | awk -v driver="$driver" '$1 ~ "^" driver "-" {print $1; exit}')"
  if [[ -z "$package_version" ]]; then
    echo "Bibliotecas NVIDIA para o módulo $driver não estão disponíveis no apt." >&2
    exit 1
  fi
  echo "Instalando bibliotecas NVIDIA $package_version para o módulo $driver..."
  packages=(compute decode cfg1 gpucomp)
  apt_args=()
  for package in "${packages[@]}"; do
    apt_args+=("libnvidia-$package-$major=$package_version")
  done
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${apt_args[@]}" "nvidia-persistenced=$package_version"
fi

python3 - <<'PY'
import torch
print('PyTorch:', torch.__version__)
print('CUDA disponível:', torch.cuda.is_available())
if not torch.cuda.is_available():
    raise SystemExit('Sessão sem GPU CUDA; instalação interrompida.')
print('GPU:', torch.cuda.get_device_name(0))
print('VRAM total (GiB):', round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1))
PY

if [[ -f "$ROOT/installed.ok" ]] && \
   [[ "$(cat "$ROOT/installed.ok")" == "$REVISION $COMFY_REVISION" ]] && \
   [[ -f "$COMFY/main.py" ]] && \
   [[ -s "$DRIVE/models/upscale_models/RealESRGAN_x4plus.safetensors" ]]; then
  patch_gguf_qwen21
  echo 'Instalação desta VM já está preparada.'
  exit 0
fi

if [[ ! -d "$SOURCE/.git" ]]; then
  git clone --depth 1 --branch MAC-Linux "$REPO" "$SOURCE"
fi
if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$REVISION" ]]; then
  git -C "$SOURCE" fetch --depth 1 origin "$REVISION"
  git -C "$SOURCE" checkout --detach "$REVISION"
fi
echo "Easy Install: $(git -C "$SOURCE" rev-parse --short HEAD)"

if [[ ! -f "$COMFY/main.py" ]]; then
  git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git "$COMFY"
fi
if [[ "$(git -C "$COMFY" rev-parse HEAD)" != "$COMFY_REVISION" ]]; then
  git -C "$COMFY" fetch --depth 1 origin "$COMFY_REVISION"
  git -C "$COMFY" checkout --detach "$COMFY_REVISION"
fi
echo "ComfyUI: $(git -C "$COMFY" rev-parse --short HEAD)"

if ! python3 -m ensurepip --version >/dev/null 2>&1; then
  python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  DEBIAN_FRONTEND=noninteractive apt-get install -y "python${python_version}-venv"
fi
python3 -m venv --system-site-packages "$INSTALL/.venv"
PYTHON="$INSTALL/.venv/bin/python"
PIP=("$PYTHON" -m pip --disable-pip-version-check)

# Colab já fornece um PyTorch CUDA compatível com o driver da GPU recebida.
# O instalador desktop fixa outra versão e substituiria essa pilha a cada sessão.
grep -viE '^(torch|torchvision|torchaudio)([<>=!~[:space:]]|$)' \
  "$COMFY/requirements.txt" > "$ROOT/comfy-requirements.txt"
"${PIP[@]}" install -r "$ROOT/comfy-requirements.txt"
"${PIP[@]}" install -r "$COMFY/manager_requirements.txt" psutil

mkdir -p "$DRIVE/models" "$DRIVE/input" "$DRIVE/output" "$DRIVE/user"

# Usa os workflows e ajustes distribuídos no Helper do Easy Install.
"$PYTHON" - "$SOURCE/Helper-CEI-NEXT-unix.zip" "$DRIVE/user" <<'PY'
from pathlib import Path
from zipfile import ZipFile
import sys

archive, user_dir = Path(sys.argv[1]), Path(sys.argv[2])
prefix = 'ComfyUI-Easy-Install/ComfyUI/user/'
with ZipFile(archive) as z:
    for info in z.infolist():
        if not info.filename.startswith(prefix) or info.is_dir():
            continue
        relative = Path(info.filename.removeprefix(prefix))
        if '..' in relative.parts:
            raise SystemExit('Caminho inválido no Helper-CEI')
        target = user_dir / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(z.read(info))
PY

if [[ ! -L "$COMFY/models" ]]; then
  for category in "$COMFY"/models/*/; do
    [[ -d "$category" ]] || continue
    mkdir -p "$DRIVE/models/$(basename "$category")"
  done
  mv "$COMFY/models" "$INSTALL/default-models"
  ln -s "$DRIVE/models" "$COMFY/models"
fi

# Um modelo pequeno e público permite comprovar inferência CUDA real no teste.
UPSCALE_MODEL="$DRIVE/models/upscale_models/RealESRGAN_x4plus.safetensors"
if [[ ! -s "$UPSCALE_MODEL" ]]; then
  mkdir -p "$(dirname "$UPSCALE_MODEL")"
  curl --fail --location --retry 3 \
    'https://huggingface.co/Comfy-Org/Real-ESRGAN_repackaged/resolve/main/RealESRGAN_x4plus.safetensors' \
    --output "$UPSCALE_MODEL.partial"
  if (( $(stat -c %s "$UPSCALE_MODEL.partial") < 1000000 )); then
    echo 'O arquivo de teste RealESRGAN está incompleto.' >&2
    exit 1
  fi
  mv "$UPSCALE_MODEL.partial" "$UPSCALE_MODEL"
fi

# A lista de nodes vem diretamente da ramificação MAC-Linux escolhida pelo usuário.
NODE_LIST="$ROOT/easy-install-nodes.txt"
awk '$1 == "get_node" && $2 ~ /^https:\/\// && !seen[$3]++ {print $2, $3}' \
  "$SOURCE/ComfyUI-Easy-Install.sh" > "$NODE_LIST"
FAILURES="$ROOT/node-install-failures.txt"
: > "$FAILURES"
mkdir -p "$COMFY/custom_nodes"
while read -r url folder; do
  [[ -n "$url" && -n "$folder" ]] || continue
  if [[ "$folder" == comfyui-manager ]]; then
    echo 'Gerenciador legado ignorado; o ComfyUI atual usa manager_requirements.txt.'
    continue
  fi
  target="$COMFY/custom_nodes/$folder"
  if [[ ! -d "$target/.git" ]]; then
    echo "Instalando node: $folder"
    if ! git clone --depth 1 "$url" "$target"; then
      printf '%s: clone\n' "$folder" >> "$FAILURES"
      continue
    fi
  fi
  if [[ -s "$target/requirements.txt" ]]; then
    grep -viE '^(torch|torchvision|torchaudio)([<>=!~[:space:]]|$)' \
      "$target/requirements.txt" > "$ROOT/node-requirements.txt" || true
    if [[ -s "$ROOT/node-requirements.txt" ]]; then
      if ! timeout 600 "${PIP[@]}" install -r "$ROOT/node-requirements.txt"; then
        printf '%s: requirements\n' "$folder" >> "$FAILURES"
      fi
    fi
  fi
  if [[ -s "$target/install.py" ]]; then
    if ! timeout 300 "$PYTHON" "$target/install.py"; then
      printf '%s: install.py\n' "$folder" >> "$FAILURES"
    fi
  fi
done < "$NODE_LIST"

patch_gguf_qwen21

"$PYTHON" - <<'PY'
import torch
if not torch.cuda.is_available():
    raise SystemExit('Uma dependência substituiu o PyTorch CUDA; verifique install.log.')
print('GPU preservada após instalação:', torch.cuda.get_device_name(0))
PY

echo "Nodes do Easy Install: $(wc -l < "$NODE_LIST")"
if [[ -s "$FAILURES" ]]; then
  echo 'Alguns nodes exigem ajustes adicionais:'
  cat "$FAILURES"
fi
echo 'Instalação remota preparada.'
printf '%s %s\n' "$REVISION" "$COMFY_REVISION" > "$ROOT/installed.ok"
