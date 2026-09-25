"""Rastreia e libera uma alocação interrompida antes de o Colab CLI salvá-la.

Executado pelo Python do mesmo venv do Colab CLI, dentro do WSL.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from colab_cli.common import state


state.config_path = os.environ.get("COMFY_COLAB_CONFIG_PATH")


def assignments():
    return state.client.list_assignments()


def endpoint_set() -> set[str]:
    return {item.endpoint for item in assignments()}


def release(marker: Path) -> None:
    if not marker.exists():
        return
    data = json.loads(marker.read_text(encoding="utf-8"))
    age = time.time() - float(data["started_at"])
    if age > 900:
        raise RuntimeError(
            "Alocação pendente antiga; confira as sessões no Colab antes de iniciar novamente."
        )

    baseline = set(data["baseline"])
    expected_gpu = data["gpu"]
    candidates = []
    for assignment in assignments():
        accelerator = getattr(assignment.accelerator, "value", assignment.accelerator)
        if assignment.endpoint not in baseline and (
            accelerator == expected_gpu
            or (expected_gpu == "CPU" and accelerator in (None, "NONE", ""))
        ):
            candidates.append(assignment.endpoint)

    if len(candidates) > 1:
        raise RuntimeError(
            "Mais de uma alocação nova apareceu; não foi seguro escolher qual encerrar."
        )
    if candidates:
        state.client.unassign(candidates[0])
        print(f"Alocação interrompida liberada: {candidates[0]}", flush=True)
    marker.unlink(missing_ok=True)


def begin(marker: Path, gpu: str) -> None:
    if marker.exists():
        release(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    data = {"started_at": time.time(), "gpu": gpu, "baseline": sorted(endpoint_set())}
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(marker)


def main() -> int:
    action = sys.argv[1]
    marker = Path(sys.argv[2])
    if action == "begin":
        begin(marker, sys.argv[3])
    elif action == "release":
        release(marker)
    elif action == "clear":
        marker.unlink(missing_ok=True)
    else:
        raise ValueError("Ação de alocação inválida")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Não foi possível liberar a alocação: {exc}", file=sys.stderr)
        raise SystemExit(1)
