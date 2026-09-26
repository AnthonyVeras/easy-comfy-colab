#!/usr/bin/env bash
# Compartilhado pelo botão manual e pelo encerramento; usa o SSH já conectado.
update_runtime_image() {
  local helper
  local remote_dir=/content/comfy-colab/image-tools-2.0.5
  echo 'Atualizando imagem do Drive: preparando cópia da instalação atual...'
  ssh "${ssh_options[@]}" root@colab "mkdir -p $remote_dir" || return 1
  for helper in install.sh runtime_image.py mcp_http.py; do
    scp "${ssh_options[@]}" "$PROJECT/remote/$helper" "root@colab:$remote_dir/$helper" || return 1
  done
  ssh "${ssh_options[@]}" root@colab "nice -n 19 ionice -c 3 python3 $remote_dir/runtime_image.py build"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -Eeuo pipefail
  PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
  source "$PROJECT/app/environment.sh"
  source "$PROJECT/app/ssh_transport.sh"
  exec 9>"$STATE/runtime-image.lock"
  flock -n 9 || { echo 'Já existe uma atualização de imagem ou encerramento em andamento.' >&2; exit 1; }
  transport_ready || { echo 'Reconecte à VM antes de atualizar a imagem.' >&2; exit 1; }
  if ! update_runtime_image; then
    echo 'A imagem não foi atualizada. A VM e a imagem anterior foram preservadas.' >&2
    exit 1
  fi
fi
