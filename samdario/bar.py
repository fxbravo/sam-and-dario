"""La barre « tableau de bord » (tâche 11, option B des maquettes) : mise en forme pour tmux.

Le texte de `rules.status_line` reste la seule source de vérité : ce module ne fait que le mettre en forme avec
les styles `#[…]` de tmux, et y ajoute le compteur en points, le dernier relais et l'assistant attendu. tmux
interprète aussi la sortie d'une commande `#(…)` : chaque `#` d'un texte est doublé. Vérifié sur tmux 3.6a.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple, Optional

from . import auto as auto
from . import rules as rules
from . import state as state
from . import team as team
from .tmux import tmux

COLOURS = {
    "claude": "colour166", "codex": "colour25",
    "run": "colour28", "accord": "colour22", "user": "colour94", "decision": "colour55", "risk": "colour124",
    "off": "colour242", "ground": "colour254", "text": "colour236", "empty": "colour248",
}
MARKER = "tableau-de-bord"  # posé par le lanceur qui installe la barre : une session plus ancienne garde la sienne
LAST_LINE = "barre-derniere-ligne"  # dernière ligne affichée, reprise tant que la base est verrouillée
BUSY = "Sam & Dario — base occupée, un instant…"
STATUS_STYLE = f"bg={COLOURS['ground']},fg={COLOURS['text']}"
MAX_DOTS = 12
SEPARATOR = " │ "
COUNTER = re.compile(r"^AUTO (\d+)/(\d+)$")
RISKS = ("incertain", "suivi", "long", "echec", "inconnue")
HINTS = {  # ce que l'utilisateur peut faire, pour les pauses qu'il lève lui-même
    rules.REASONS["accord"]: "tape ta prochaine demande pour relancer",
    rules.REASONS["limite"]: "tape ta demande ou ^b a",
    rules.REASONS["decision"]: "réponds dans l’un des panneaux",
}
# Bouton cliquable (tâche 12) : il bascule entre les terminaux et la scène ; ^b v fait de même.
BUTTON_COLOUR = "colour61"  # le violet du bouton Scène (tâche 12)


def _button(name: str, label: str, colour: str = BUTTON_COLOUR, escaped: bool = False) -> str:
    """Un bouton cliquable (tâche 15). Dans une condition tmux, les virgules du style s'écrivent `#,`."""
    comma = "#," if escaped else ","
    return f"#[range=user|{name}]#[bg={colour}{comma}fg=colour255{comma}bold]{label}#[norange]#[default] "


def _shown_if(option: str, button: str) -> str:
    return f"#{{?#{{{option}}},{button},}}"


# Seconde ligne de la barre : les boutons, du plus utile au moins utile, Fermer seul à droite. Coller, Recoller et
# Exporter n'apparaissent que lorsqu'ils servent, et jamais dans la scène. Les options @samdario-… sont tenues par
# `dashboard` (`button_flags`). Les raccourcis clavier restent tous liés.
BUTTONS = (_button("vue", "#{?#{==:#{window_name},scene}, ◀ Terminaux , ▶ Scène }")
           + _button("auto", "#{?#{==:#{@samdario-auto},on}, ⏸ Pause , ▶ Auto }")
           + _button("deux", " ✉ Pour les deux ")
           + "#{?#{==:#{window_name},scene},,"
           + _shown_if("@samdario-coller", _button("coller", " ⎘ Coller ", escaped=True))
           + _shown_if("@samdario-recoller", _button("recoller", " ↻ Recoller ", escaped=True))
           + _shown_if("@samdario-exportable", _button("exporter", " Exporter l’accord ", escaped=True))
           + "}#[align=right]" + _button("fermer", " ✕ Fermer ", COLOURS["risk"]))


class Progress(NamedTuple):
    count: int
    limit: int
    last: Optional[tuple[str, str, int]]  # (prénom de l'émetteur, prénom du destinataire, numéro du relais)
    waiting: Optional[str]  # prénom de l'assistant attendu


def _escape(text: str) -> str:
    return text.replace("#", "##")


def _chip(colour: str, text: str) -> str:
    return f"#[bg={colour},fg=colour255,bold] {_escape(text)} #[default]"


def family(status: str) -> str:
    """La famille de couleur d'un état : run, accord, user, decision, risk ou off."""
    main = status.split(" · ")[0]
    if main == "OFF":
        return "off"
    if main.startswith("AUTO"):
        return "run"
    reason = main[len("PAUSE — "):] if main.startswith("PAUSE — ") else main
    for key, text in rules.REASONS.items():
        if reason.startswith(text.split("{")[0].rstrip()):
            return {"accord": "accord", "decision": "decision"}.get(key, "risk" if key in RISKS else "user")
    return "user"  # raison inconnue (session plus ancienne) : ce qui attend l'utilisateur, jamais une erreur


def _counter(count: int, limit: int) -> str:
    if limit > MAX_DOTS:
        return f"{count}/{limit}"
    filled = min(count, limit)
    return (f"#[fg={COLOURS['run']}]{'●' * filled}#[fg={COLOURS['empty']}]{'○' * (limit - filled)}"
            f"#[default] {count}/{limit}")


def status_markup(status: str, progress: Progress) -> str:
    """Ligne 1 de la barre : bloc d'état, compteur ou raison, dernier relais, attente, puis les indications."""
    main, *cues = status.split(" · ")
    kind = family(status)
    if kind == "off":
        head, body = "OFF", "auto coupé"
    elif main.startswith("AUTO"):
        head, body = "AUTO", main[len("AUTO — "):] if main.startswith("AUTO — ") else ""
    else:
        head, body = "PAUSE", main[len("PAUSE — "):] if main.startswith("PAUSE — ") else main
    parts = [_chip(COLOURS[kind], head)]
    counter = COUNTER.match(main)
    details = []
    if counter:
        details.append(_counter(int(counter.group(1)), int(counter.group(2))))
    elif body:
        details.append(_escape(body))
        if body in HINTS:
            details.append(_escape(HINTS[body]))
    if counter and progress.last:
        sender, recipient, number = progress.last
        details.append(f"dernier relais : {_escape(sender)} → {_escape(recipient)} (##{number})")
    if counter and progress.waiting:
        details.append(f"en attente : {_escape(progress.waiting)}")
    if details:
        parts.append(" " + SEPARATOR.join(details))
    parts.extend(" " + _chip(COLOURS["user"], cue) for cue in cues)
    return "".join(parts)


def progress(db, names: dict[str, str]) -> Progress:
    """Compteur, dernier relais reçu et assistant attendu, lus dans la base de la session. Un relais compte une fois
    que son destinataire l'a confirmé (Entrée chez Claude, prise en charge chez Codex) : avant, il n'est qu'en
    route, et la barre le dit par ses indications (« Entrée chez Claude »…)."""
    row = db.execute("""SELECT id, sender FROM replies WHERE delivery IS NOT NULL AND status = ?
                        ORDER BY delivery DESC LIMIT 1""", (state.SENT,)).fetchone()
    last = (names[row[1]], names[state.other(row[1])], row[0]) if row else None
    awaiting = state.get(db, "awaiting")
    return Progress(int(state.get(db, "count")), int(state.get(db, "max")), last,
                    names[awaiting] if awaiting in names else None)


def activity(db, now: float) -> dict[str, str]:
    """Ce que fait chaque assistant, pour le titre de son panneau."""
    if rules.field_reply(db) is not None or _common_draft(db):
        claude = "Entrée attendue"
    elif not rules.claude_idle(db):
        claude = "rédige sa réponse"
    elif rules.waiter_valid(db, now):
        claude = "attend le relais"
    else:
        claude = "au repos"
    codex = {"busy": "rédige sa réponse", "permission": "autorisation demandée"}.get(
        state.get(db, "codex_state"), "attend le relais")
    return {"claude": claude, "codex": codex}


def _common_draft(db) -> bool:
    """Une question « aux deux » collée chez Claude attend son Entrée (même condition que la barre)."""
    number = rules.round_open(db)
    return number is not None and rules.hold(db, number) == "draft"


def button_flags(db, status: str) -> dict:
    """Libellé du bouton d'auto et visibilité des boutons (tâche 15, relecture de Codex du 23 novembre).
    - Pendant l'attente du démarrage automatique, l'auto compte comme actif : le bouton propose « ⏸ Pause ».
    - Recoller suit ce que la commande sait réellement recoller, jamais les mots de la barre : une réponse en
      brouillon ou à la remise incertaine (chez Claude comme chez Codex), le relais collé chez Claude qui attend
      son Entrée, ou la question « pour les deux » collée ou à coller chez Claude."""
    mode = state.get(db, "auto")
    if mode == state.OFF and rules.autostart_pending(db):
        mode = state.ON
    latest = [state.latest_reply(db, agent) for agent in state.NAMES]
    number = state.round_open(db)
    recoller = (any(row and row[2] in (state.DRAFTED, state.UNCERTAIN) for row in latest)
                or rules.field_reply(db) is not None
                or (number is not None and state.hold(db, number) in ("draft", "ready")))
    row = db.execute("SELECT value FROM meta WHERE key = 'accord_seen'").fetchone()
    return {"@samdario-auto": mode, "@samdario-coller": "1" if mode == state.OFF else "",
            "@samdario-recoller": "1" if recoller else "",
            "@samdario-exportable": "1" if row and int(row[0] or 0) > 0 else ""}


def border_format() -> str:
    """Titre de chaque panneau ; les styles vivent ici, les valeurs (prénom, couleur, activité) dans les options
    de panneau, que tmux affiche telles quelles."""
    # `default` = couleurs du terminal : `#[default]` reviendrait au style de la bordure (orange si actif).
    return ("#[fg=colour255,bg=#{@samdario_colour},bold] #{?pane_active,●,○} #{@samdario} "
            "#[fg=default,bg=default,nobold] #{@samdario_activity} ")


def dashboard(folder: Path, now: Optional[float] = None) -> str:
    """Ce qu'affiche la ligne 1 de la barre, chaque seconde. Met à jour l'activité des panneaux seulement quand elle
    change, et ne la note comme affichée qu'une fois tmux mis à jour. Une erreur de lecture ou de tmux laisse la
    barre en texte brut plutôt que vide."""
    try:
        status = auto.status(folder, now)
    except (OSError, sqlite3.Error):  # base verrouillée un instant : garder la dernière ligne affichée
        return _last_line(folder)
    try:
        with state.transaction(folder) as db:
            names = team.names_of(db)
            markup = status_markup(status, progress(db, names))
            current = activity(db, auto._clock(now))
            row = db.execute("SELECT value FROM meta WHERE key = 'bar_activity'").fetchone()
        if row is None or json.loads(row[0]) != current:
            panes = json.loads((folder / "panes.json").read_text(encoding="utf-8"))
            for pane, agent in panes.items():
                tmux(folder, "set-option", "-p", "-t", pane, "@samdario_activity", f"— {current[agent]}")
            with state.transaction(folder) as db:
                state.put(db, bar_activity=json.dumps(current, ensure_ascii=False))
        refresh_buttons(folder, status)
        if _last_line(folder) != markup:
            (folder / LAST_LINE).write_text(markup, encoding="utf-8")
        return markup
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return _escape(status)


def refresh_buttons(folder: Path, status: str = "") -> bool:
    """Règle les options des boutons si elles ont changé, et le dit. Appelée par chaque rafraîchissement de la barre,
    et aussitôt après un clic sur le bouton d'auto : son libellé suit le clic sans attendre la barre."""
    with state.transaction(folder) as db:
        flags = button_flags(db, status)
        shown = db.execute("SELECT value FROM meta WHERE key = 'bar_buttons'").fetchone()
    if shown is not None and json.loads(shown[0]) == flags:
        return False
    for key, value in flags.items():
        tmux(folder, "set-option", key, value)
    with state.transaction(folder) as db:
        state.put(db, bar_buttons=json.dumps(flags))
    return True


def _last_line(folder: Path) -> str:
    try:
        return (folder / LAST_LINE).read_text(encoding="utf-8")
    except OSError:
        return BUSY


def installed(folder: Path) -> bool:
    """Vrai si le lanceur de cette session a installé la barre « tableau de bord »."""
    return (folder / MARKER).is_file()
