"""La scène 16 bits (tâche 12) : Alma et Milo en pixels, chacun avec sa dernière réponse dans une bulle.

La scène tourne dans une seconde fenêtre tmux et ne fait que regarder : elle ouvre la base en lecture seule et
n'appelle aucune fonction qui écrit (ni `rules.status_line`, ni `auto.status`, ni `bar.activity`). Elle lit ce
qui est déjà enregistré, dont l'activité que la barre note (`bar_activity`). Le README décrit cette vue dans « La scène ».
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NamedTuple, Optional

from . import bar as bar
from . import crossread as crossread
from . import rules as rules
from . import team as team
from .scene_text import clean, clusters, columns, fit, wrap

MIN_WIDTH, MIN_HEIGHT = 60, 20
TOO_SMALL = "Agrandis la fenêtre pour voir la scène"
MORE, MORE_SHORT = "▼ molette pour la suite", "▼"  # tâche 16 : la bulle défile, plus de « suite dans le terminal »
THUMB, TRACK = "█", "░"
WAITING = "…"
ERROR = "Sam & Dario — erreur de la scène : "
WRITE_HINT = "◀ Terminaux pour écrire"  # on n'écrit jamais dans la scène
SPRITE_HEIGHT, SPRITE_WIDTH = 9, 16  # deux pixels par caractère (▀) : 18 × 16 pixels
SIZE_PERIOD, READ_PERIOD = 0.2, 1.0

# Palette 256 couleurs, choisie à la main au plus près du prototype (l'utilisateur a préféré 256 couleurs
# retouchées aux couleurs exactes : la plus proche par calcul rendait Alma saumon et Milo gris).
SKY = 117  # tâche 12 bis : un seul bleu clair, choisi par l'utilisateur, à la place des quatre bandes
HILL, GRASS, SOIL, INK, PAPER = 28, 70, 137, 234, 231
PIXELS = {".": None, "D": 130, "O": 208, "W": 231, "K": 234, "P": 217, "N": 17, "B": 25, "L": 153, "Y": 221}
SPRITES = {
    "claude": ("......DD........", ".....DOOD.......", "......DD........", "...DDDDDDDDDD...", "..DOOOOOOOOOOD..",
               ".DOOOOOOOOOOOOD.", ".DOOWWOOOOWWOOD.", ".DOOWKOOOOWKOOD.", ".DOOWKOOOOWKOOD.", ".DOPPOOOOOOPPOD.",
               ".DOOOOKKKKOOOOD.", "..DOOOOOOOOOOD..", "...DDDDDDDDDD...", "....DOOOOOOD....", "...DOODOODOOD...",
               "...DOOD..DOOD...", "...DDD....DDD...", "................"),
    "codex": (".......YY.......", ".......NN.......", "...NNNNNNNNNN...", "..NBBBBBBBBBBN..", ".NBBBBBBBBBBBBN.",
              ".NBLLLLLLLLLLBN.", ".NBLKLLLLLLKLBN.", ".NBLKLLLLLLKLBN.", ".NBLLLLLLLLLLBN.", ".NBBBBBBBBBBBBN.",
              ".NBBBNNNNNNBBBN.", "..NBBBBBBBBBBN..", "...NNNNNNNNNN...", "....NBBBBBBN....", "...NBBNBBNBBN...",
              "...NBBN..NBBN...", "...NNN....NNN...", "................"),
}


class Cell(NamedTuple):
    char: str  # "" : seconde moitié d'un caractère large, rien à écrire
    fg: int
    bg: int


class SceneData(NamedTuple):
    names: dict
    texts: dict  # dernière réponse par assistant, None tant qu'il n'a pas répondu dans sa session courante
    activity: dict
    awaited: Optional[str]
    status: str  # "OFF", "AUTO n/m" ou "PAUSE — <raison>", lu tel qu'enregistré
    decision_by: tuple  # auteurs des décisions présentées (tâche 13 : une ou deux)
    ids: dict = {}  # identifiant de la réponse affichée par assistant (tâche 16 : une nouvelle réponse remonte)


# --- Lecture de la base -----------------------------------------------------------------------------------------

def read_only(folder: Path) -> sqlite3.Connection:
    """Connexion en lecture seule ; l'URI est construite par pathlib (un `#` du chemin y est échappé)."""
    uri = (Path(folder) / "replies.db").resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=0)  # un verrou rend la main : la molette n'attend pas


def _meta(db, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _latest(db, sender: str, session: Optional[str]) -> tuple:
    """(identifiant, texte) de la dernière réponse de ce fil, ou (None, None). Texte nettoyé au dessin."""
    if not session:
        return None, None
    row = db.execute("SELECT id, body FROM replies WHERE sender = ? AND session = ? ORDER BY id DESC LIMIT 1",
                     (sender, session)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _claude_session(db) -> Optional[str]:
    """Session de la dernière soumission reçue par Claude : une fin de tour tardive d'une ancienne session
    (après /clear) n'en est pas une, contrairement à `claude_session`."""
    row = db.execute("""SELECT session FROM events WHERE agent = 'claude' AND kind IN ('submitted', 'relay')
                        ORDER BY id DESC LIMIT 1""").fetchone()
    return row[0] if row else None


def scene_data(db) -> SceneData:
    """Tout ce que montre la scène, par des lectures seulement."""
    names = team.names_of(db)
    auto = _meta(db, "auto", rules.OFF)
    reason = _meta(db, "reason")
    if auto == rules.ON:
        status = f"AUTO {_meta(db, 'count', '0')}/{_meta(db, 'max', '0')}"
        if crossread.deferred(db):  # tâche 13 : lecture seule, comme le reste de la scène
            status += f" · {crossread.READING}"
    else:
        status = f"PAUSE — {reason}" if auto == rules.PAUSE else "OFF"
    decision_by = ()
    if auto == rules.PAUSE and reason == rules.REASONS["decision"]:
        try:
            ids = [int(reply) for reply in json.loads(_meta(db, "decision_ids", "[]"))]
        except (ValueError, TypeError):
            ids = []
        ids = ids or [int(_meta(db, "decision_seen", "0") or 0)]
        marks = ",".join("?" * len(ids))
        found = {sender for (sender,) in db.execute(f"SELECT sender FROM replies WHERE id IN ({marks})", ids)}
        decision_by = tuple(agent for agent in ("claude", "codex") if agent in found)
    try:
        activity = json.loads(_meta(db, "bar_activity", "{}"))
    except ValueError:
        activity = {}
    awaited = _meta(db, "awaiting")
    latest = {"claude": _latest(db, "claude", _claude_session(db)), "codex": _latest(db, "codex", _meta(db, "codex_thread"))}
    return SceneData(names, {agent: found[1] for agent, found in latest.items()},
                     activity if isinstance(activity, dict) else {}, awaited if awaited in names else None,
                     status, decision_by, {agent: found[0] for agent, found in latest.items()})


# --- Image --------------------------------------------------------------------------------------------------------

class _Canvas:
    """Grille de travail, locale à `frame` ; l'image rendue est un tuple de tuples."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.bg = [[SKY] * width for _ in range(height)]
        self.cells = [[Cell(" ", INK, self.bg[r][c]) for c in range(width)] for r in range(height)]

    def fill(self, row: int, left: int, right: int, colour: int) -> None:
        if 0 <= row < self.height:
            for col in range(max(0, left), min(self.width, right)):
                self.bg[row][col] = colour
                self.cells[row][col] = Cell(" ", INK, colour)

    def put(self, row: int, col: int, text: str, fg: int = INK, bg: Optional[int] = None) -> None:
        if not 0 <= row < self.height:
            return
        for char, size in clusters(text):
            if size == 0:
                continue  # signe sans caractère de base : rien à afficher
            if 0 <= col and col + size <= self.width:
                back = self.bg[row][col] if bg is None else bg
                self.cells[row][col] = Cell(char, fg, back)
                if size == 2:
                    self.cells[row][col + 1] = Cell("", fg, back)
            col += size

    def image(self) -> tuple:
        return tuple(tuple(row) for row in self.cells)


def _message(width: int, height: int, text: str, bg: int = INK) -> tuple:
    canvas = _Canvas(width, height)
    for row in range(height):
        canvas.fill(row, 0, width, bg)
    if height:
        shown = text if columns(text) <= width else text[:max(0, width - 1)] + "…"
        canvas.put(height // 2, max(0, (width - columns(shown)) // 2), shown, PAPER, bg)
    return canvas.image()


def _banner(status: str) -> Optional[tuple]:
    if crossread.READING in status:
        return "LECTURE CROISÉE · ils se lisent avant ta décision", bar_colour("decision")
    if not status.startswith("PAUSE — "):
        return None
    reason = status[len("PAUSE — "):]
    colour = bar_colour(bar.family(status))
    if reason == rules.REASONS["accord"]:
        return f"✦ ACCORD ✦ · {WRITE_HINT}", colour
    if reason == rules.REASONS["decision"]:
        return "DÉCISION REQUISE · ◀ Terminaux pour répondre", colour
    hint = f" · {WRITE_HINT}, ou ^b a" if reason == rules.REASONS["limite"] else ""
    return f"PAUSE · {reason}{hint}", colour


def _draw_banner(canvas: _Canvas, banner: tuple) -> None:
    text, colour = banner
    canvas.fill(0, 0, canvas.width, colour)
    if columns(text) > canvas.width - 2:
        text = text[:max(0, canvas.width - 3)] + "…"
    canvas.put(0, max(1, (canvas.width - columns(text)) // 2), text, PAPER, colour)


def _draw_sprite(canvas: _Canvas, agent: str, centre: int, top: int) -> None:
    rows = SPRITES[agent]
    for y in range(SPRITE_HEIGHT):
        for x in range(SPRITE_WIDTH):
            upper, lower = PIXELS[rows[2 * y][x]], PIXELS[rows[2 * y + 1][x]]
            row, col = top + y, centre - SPRITE_WIDTH // 2 + x
            if (upper is None and lower is None) or not (0 <= row < canvas.height and 0 <= col < canvas.width):
                continue
            back = canvas.bg[row][col]
            canvas.cells[row][col] = Cell("▀", back if upper is None else upper, back if lower is None else lower)


def _bubble_lines(data: SceneData, agent: str, inner: int, cache=None) -> list:
    text = clean(data.texts.get(agent) or "")
    if not text:
        return [WAITING]
    if cache is None:
        return wrap(text, inner)
    return cache.lines(agent, data.ids.get(agent), text, inner, wrap)


def _fitted_lines(data: SceneData, agent: str, inner: int, room: int, cache=None) -> list:
    """Les lignes de la bulle. Une réponse qui dépasse est découpée une colonne plus court : la barre de
    défilement prend la colonne de marge, et le texte ne doit pas la toucher (capture de l'utilisateur)."""
    lines = _bubble_lines(data, agent, inner, cache)
    return _bubble_lines(data, agent, inner - 1, cache) if len(lines) > room else lines


def _geometry(width: int, height: int, banner: bool) -> tuple:
    """(largeur extérieure, largeur du texte, haut, bord inférieur) des bulles, à cette taille."""
    outer = width // 2 - 1
    bottom = height - 3 - SPRITE_HEIGHT - 2
    return outer, outer - 4, 1 if banner else 0, bottom


def bubble_limits(data: SceneData, width: int, height: int, cache=None) -> dict:
    """Position maximale de défilement de chaque bulle : lignes de la réponse moins place disponible."""
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return {"claude": 0, "codex": 0}
    _, inner, top, bottom = _geometry(width, height, _banner(data.status) is not None)
    room = max(1, bottom - top - 2)
    return {agent: max(0, len(_fitted_lines(data, agent, inner, room, cache)) - room) for agent in ("claude", "codex")}


def _draw_bubble(canvas: _Canvas, data: SceneData, agent: str, left: int, top: int, bottom: int,
                 centre: int, offset: int = 0, cache=None) -> None:
    """Bulle de `left` à `left + largeur`, collée en bas (`bottom` = son bord inférieur), haute d'au plus
    `bottom - top + 1` lignes. Une réponse plus longue défile (tâche 16) : `offset` est la première ligne vue."""
    outer, inner, _, _ = _geometry(canvas.width, canvas.height, False)
    room = max(1, bottom - top - 2)
    lines = _fitted_lines(data, agent, inner, room, cache)
    offset = max(0, min(offset, len(lines) - room))
    window = lines[offset:offset + room]
    start = bottom - len(window) - 2
    colour = bar_colour(agent)
    canvas.put(start, left, "╭" + "─" * (outer - 2) + "╮", INK, PAPER)
    canvas.put(start + 1, left, "│ " + " " * inner + " │", INK, PAPER)
    canvas.put(start + 1, left + 2, fit(data.names.get(agent, agent).upper(), inner), colour, PAPER)
    for row, line in enumerate(window):
        canvas.put(start + 2 + row, left, "│ " + " " * inner + " │", INK, PAPER)
        canvas.put(start + 2 + row, left + 2, line, INK, PAPER)
    canvas.put(bottom, left, "╰" + "─" * (outer - 2) + "╯", INK, PAPER)
    canvas.put(bottom, centre, "┬", INK, PAPER)
    if len(lines) > room:
        _draw_scrollbar(canvas, left + outer - 2, start + 2, room, offset, len(lines))
        if offset > 0:
            canvas.put(start, left + outer - 3, "▲", INK, PAPER)
        if offset < len(lines) - room:
            more = MORE if left + outer - 2 - columns(MORE) > centre else MORE_SHORT
            canvas.put(bottom, left + outer - 2 - columns(more), more, INK, PAPER)


def _draw_scrollbar(canvas: _Canvas, column: int, top: int, room: int, offset: int, total: int) -> None:
    thumb = max(1, room * room // total)
    position = offset * (room - thumb) // max(1, total - room)
    for row in range(room):
        canvas.put(top + row, column, THUMB if position <= row < position + thumb else TRACK, INK, PAPER)


def frame(data: SceneData, width: int, height: int, scroll: Optional[dict] = None, cache=None) -> tuple:
    """L'image de la scène pour un panneau de `width` × `height` caractères. `scroll` donne la première ligne vue
    de chaque bulle (tâche 16) ; `cache` garde les lignes déjà découpées."""
    width, height = max(0, width), max(0, height)
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return _message(width, height, TOO_SMALL)
    ground = height - 3
    canvas = _Canvas(width, height)
    for row in range(ground - 5, ground):
        canvas.fill(row, 0, width * 3 // 10, HILL)
    for row in range(ground - 6, ground):
        canvas.fill(row, width * 7 // 10, width, HILL)
    for col in range(width):
        canvas.cells[ground][col] = Cell("▀", GRASS, SOIL)
    for row in range(ground + 1, height):
        canvas.fill(row, 0, width, SOIL)
    banner = _banner(data.status)
    if banner:
        _draw_banner(canvas, banner)
    head = ground - SPRITE_HEIGHT
    for agent, left, centre in (("claude", 0, width // 4), ("codex", width // 2, width * 3 // 4)):
        _draw_sprite(canvas, agent, centre, head)
        _draw_bubble(canvas, data, agent, left, 1 if banner else 0, head - 2, centre,
                     (scroll or {}).get(agent, 0), cache)
        canvas.put(head - 1, centre, "╲")
        if agent in data.decision_by:
            canvas.put(head - 1, centre + 2, " ! ", PAPER, bar_colour("decision"))
        mark = "●" if data.awaited == agent else "○"
        limit = (width // 2 if agent == "claude" else width) - (centre - SPRITE_WIDTH // 2) - 1
        label = f" {mark} {fit(team.title(agent, data.names), max(1, limit - 4))} "
        canvas.put(ground + 1, centre - SPRITE_WIDTH // 2, label, PAPER, bar_colour(agent))
        start = centre - SPRITE_WIDTH // 2 + columns(label) + 1
        room = (width * 3 // 4 - SPRITE_WIDTH // 2 if agent == "claude" else width) - start - 1
        activity = str(data.activity.get(agent, ""))
        if activity and room > 0:
            canvas.put(ground + 1, start, activity if columns(activity) <= room else activity[:room - 1] + "…",
                       INK, SOIL)
    return canvas.image()


def render(image: tuple) -> str:
    """Séquences pour le terminal : 256 couleurs, chaque ligne positionnée explicitement."""
    parts = ["\x1b[H"]
    for number, row in enumerate(image, start=1):
        parts.append(f"\x1b[{number};1H")
        style = None
        for cell in row:
            if (cell.fg, cell.bg) != style:
                style = (cell.fg, cell.bg)
                parts.append(f"\x1b[38;5;{cell.fg}m\x1b[48;5;{cell.bg}m")
            parts.append(cell.char)
    parts.append("\x1b[0m")
    return "".join(parts)


# --- Boucle -------------------------------------------------------------------------------------------------------

def _temporary(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def step(folder: Path, size: tuple, shown: Optional[tuple]) -> tuple:
    """Une itération : relit la base, renvoie (sortie à écrire ou None, référence de ce qui est affiché).
    Un verrou temporaire garde l'image ; toute autre erreur s'affiche dans la scène."""
    data = load(folder)
    if data is None:
        return None, shown
    if isinstance(data, Exception):
        return _failure(size, data, shown)
    return draw(data, size, shown)


def load(folder: Path):
    """Les données de la scène, None pendant un verrou temporaire, ou l'exception à montrer."""
    try:
        with closing(read_only(folder)) as db:
            return scene_data(db)
    except sqlite3.OperationalError as error:
        return None if _temporary(error) else error
    except Exception as error:  # noqa: BLE001 — la scène montre l'erreur au lieu de fermer la fenêtre
        return error


def draw(data: SceneData, size: tuple, shown: Optional[tuple], scroll: Optional[dict] = None, cache=None) -> tuple:
    """Dessine si la taille, les données ou les positions de défilement ont changé."""
    key = (size, data, tuple(sorted((scroll or {}).items())))
    if key == shown:
        return None, shown
    try:
        return render(frame(data, *size, scroll, cache)), key
    except Exception as error:  # noqa: BLE001 — même règle que pour la lecture
        return _failure(size, error, shown)


def _failure(size: tuple, error: Exception, shown: Optional[tuple]) -> tuple:
    key = (size, "erreur", str(error))
    if key == shown:
        return None, shown
    return render(_message(*size, ERROR + (str(error) or type(error).__name__), bar_colour("risk"))), key


def bar_colour(family: str) -> int:
    return int(bar.COLOURS[family].removeprefix("colour"))


def run(folder: Path) -> None:
    """La boucle de la scène, avec la molette, vit dans un module voisin (tâche 16)."""
    from . import scene_scroll as scroll  # import tardif : ce module importe la scène
    scroll.run(folder)
