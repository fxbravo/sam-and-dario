"""Défilement des bulles de la scène à la molette (tâche 16).

La scène demande la souris à son terminal (format SGR). tmux lui transmet alors la molette quand le pointeur est
sur la scène, au lieu d'ouvrir son mode copie ; les clics sur la barre restent à tmux (étape 0 du plan). Seule la
molette sert : le reste de ce qui arrive sur l'entrée est jeté, comme avant. Les positions de défilement restent
en mémoire, dans la scène, jamais dans la base.
"""

import os
import re
import select
import shutil
import sys
import termios
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from . import scene as scene

STEP = 3  # lignes par cran de molette
MOUSE_ON, MOUSE_OFF = "\x1b[?1000h\x1b[?1006h", "\x1b[?1000l\x1b[?1006l"
AGENTS = ("claude", "codex")
SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
PARTIAL = re.compile(rb"\x1b(\[(<[\d;]*)?)?\Z")  # début d'événement coupé en fin de lecture


class Wheel(NamedTuple):
    delta: int  # -1 vers le haut, +1 vers le bas
    column: int  # colonne du pointeur dans la scène, à partir de 1


class Reader:
    """Lit les octets du terminal de la scène. Un événement coupé entre deux lectures attend la suivante."""

    def __init__(self) -> None:
        self._rest = b""

    def feed(self, data: bytes) -> list:
        data, wheels = self._rest + data, []
        for found in SGR.finditer(data):
            button, column, _, kind = found.groups()
            if kind == b"M" and int(button) in (64, 65):
                wheels.append(Wheel(-1 if int(button) == 64 else 1, int(column)))
        tail = PARTIAL.search(data[_last_end(data):])
        self._rest = tail.group(0) if tail else b""
        return wheels


def _last_end(data: bytes) -> int:
    end = 0
    for found in SGR.finditer(data):
        end = found.end()
    return end


class Scroll(NamedTuple):
    """Positions affichées de chaque bulle, et l'identifiant de la réponse pour laquelle chacune vaut."""
    offsets: dict
    ids: dict


def follow(state: Scroll, ids: dict) -> Scroll:
    """Une nouvelle réponse (nouvel identifiant, même à texte identique) fait revenir sa bulle au début."""
    offsets = {agent: (0 if ids.get(agent) != state.ids.get(agent) else state.offsets.get(agent, 0))
               for agent in AGENTS}
    return Scroll(offsets, dict(ids))


def fit(state: Scroll, limits: dict) -> Scroll:
    """Borne chaque position à la place disponible : rien ne s'accumule au-delà du début ni de la fin."""
    offsets = {agent: max(0, min(state.offsets.get(agent, 0), limits.get(agent, 0))) for agent in AGENTS}
    return state._replace(offsets=offsets)


def apply(state: Scroll, wheel: Wheel, width: int, limits: dict) -> Scroll:
    """Un cran de molette sur la moitié gauche fait défiler Claude, sur la moitié droite Codex."""
    agent = "claude" if wheel.column <= width // 2 else "codex"
    moved = {**state.offsets, agent: state.offsets.get(agent, 0) + wheel.delta * STEP}
    return fit(state._replace(offsets=moved), limits)


class Cache:
    """Les lignes découpées de chaque réponse, par largeur : la molette ne redécoupe ni ne relit rien."""

    def __init__(self) -> None:
        self._lines = {}

    def lines(self, agent: str, reply_id, text: str, width: int, wrap: Callable) -> list:
        key = (agent, reply_id, text, width)
        if key not in self._lines:
            self._lines = {k: v for k, v in self._lines.items() if k[0] != agent or k[1] == reply_id}
            self._lines[key] = wrap(text, width)
        return self._lines[key]


# --- Boucle de la scène ---------------------------------------------------------------------------------------------

def _quiet_keyboard() -> Optional[tuple]:
    """Coupe l'écho et le mode ligne du terminal de la scène. Renvoie le réglage d'origine (None hors terminal)."""
    if not sys.stdin.isatty():
        return None
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    quiet = termios.tcgetattr(descriptor)
    quiet[3] &= ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(descriptor, termios.TCSANOW, quiet)
    return descriptor, original


def _wait_input(keyboard: Optional[tuple], timeout: float) -> bytes:
    """Ce qui est arrivé sur l'entrée de la scène, en attendant au plus `timeout` : une molette réveille aussitôt."""
    if keyboard is None:
        time.sleep(timeout)
        return b""
    received = b""
    while select.select([keyboard[0]], [], [], timeout if not received else 0)[0]:
        chunk = os.read(keyboard[0], 1024)
        if not chunk:
            break
        received += chunk
    return received


def run(folder: Path) -> None:
    """Redessine à chaque changement de taille, de données (relues chaque seconde) ou de défilement. Le clavier
    est muet ; seule la molette agit, sur la bulle sous le pointeur."""
    keyboard = _quiet_keyboard()
    out = sys.stdout
    out.write(MOUSE_ON)
    try:
        _loop(folder, keyboard, out)
    finally:
        out.write(MOUSE_OFF)
        out.flush()
        if keyboard is not None:
            termios.tcsetattr(keyboard[0], termios.TCSANOW, keyboard[1])


def _loop(folder: Path, keyboard: Optional[tuple], out) -> None:
    """Ordre d'un tour (relecture de Codex) : les crans s'appliquent d'abord à la réponse affichée au moment du
    geste ; ensuite seulement la base est relue, et une nouvelle réponse s'ouvre à la ligne 1. Une erreur de
    lecture reste affichée jusqu'à la prochaine lecture réussie ; une erreur de préparation des lignes s'affiche,
    et le tour suivant réessaie."""
    out.write("\x1b[?25l\x1b[2J")
    out.flush()
    reader, cache = Reader(), Cache()
    state, data, error, shown, read_at, size = Scroll({}, {}), None, None, None, 0.0, None
    while True:
        wheels = reader.feed(_wait_input(keyboard, scene.SIZE_PERIOD))
        now, previous = time.monotonic(), size
        size = tuple(shutil.get_terminal_size())
        hidden = size[0] < scene.MIN_WIDTH or size[1] < scene.MIN_HEIGHT
        try:
            if wheels and data is not None and error is None and not hidden:
                limits = scene.bubble_limits(data, *size, cache)
                for wheel in wheels:
                    state = apply(state, wheel, size[0], limits)
            if data is None or size != previous or now - read_at >= scene.READ_PERIOD:
                read_at = now
                loaded = scene.load(folder)  # verrou temporaire : None, sans attendre ; on garde ce qui est chargé
                if isinstance(loaded, Exception):
                    error = loaded
                elif loaded is not None:
                    data, error = loaded, None
            if error is not None:
                output, shown = scene._failure(size, error, shown)
            elif data is None:
                output = None
            else:
                state = follow(state, data.ids)
                if not hidden:  # bulles masquées : les positions sont gardées, et bornées à leur retour
                    state = fit(state, scene.bubble_limits(data, *size, cache))
                output, shown = scene.draw(data, size, shown, state.offsets, cache)
        except Exception as failure:  # noqa: BLE001 — la scène montre l'erreur au lieu de fermer la fenêtre
            output, shown = scene._failure(size, failure, shown)
        if output:
            out.write(output)
            out.flush()
