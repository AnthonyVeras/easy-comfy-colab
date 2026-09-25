#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/environment.sh"
command="${1:?Comando obrigatório}"
shift
case "$command" in
  @colab) exec "$COLAB" "${COLAB_FLAGS[@]}" "$@" ;;
  @python) exec "$COLAB_PYTHON" "$@" ;;
  @check) test -x "$COLAB" && test -f "$KEY" ;;
  *) exec "$command" "$@" ;;
esac
