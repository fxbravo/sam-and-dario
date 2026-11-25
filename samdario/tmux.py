"""Appels à tmux pour une session Sam & Dario : chaque session a son propre serveur et son propre socket."""

import subprocess
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Optional


def tmux_command(state: Path, *args: str) -> list[str]:
    return ["tmux", "-f", "/dev/null", "-S", str(state / "tmux.sock"), *args]


def tmux(state: Path, *args: str, data: Optional[bytes] = None) -> str:
    result = subprocess.run(tmux_command(state, *args), input=data, capture_output=True)
    # Certains échecs de création du socket sont signalés sur stderr avec un code 0.
    if result.returncode or result.stderr:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "tmux a quitté.")
    return result.stdout.decode().strip()


def paste_to_pane(state: Path, pane: str, text: str, run=tmux) -> None:
    buffer = f"samdario-{uuid.uuid4().hex}"
    run(state, "load-buffer", "-b", buffer, "-", data=(text + "\n\n").encode())
    try:
        run(state, "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", pane)
    except RuntimeError:
        with suppress(RuntimeError):
            run(state, "delete-buffer", "-b", buffer)
        raise
