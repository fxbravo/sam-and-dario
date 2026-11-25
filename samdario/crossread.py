"""Lecture croisée avant une décision (tâche 13).

À chaque question de l'utilisateur (tour de table, ou demande tapée dans un panneau), les deux assistants se
lisent avant de lui revenir : deux relais confirmés, puis la réponse au second. Tant qu'une décision a été
demandée sur cette question et que la lecture n'est pas finie (lecture « armée »), aucune pause de décision ni
d'accord ne tombe, et seul le relais dû à cette question peut partir.

Sans décision, rien ne change : la lecture n'est qu'enregistrée. Une base d'avant la tâche 13, sans la clé
`crossread`, garde son comportement. Conception : docs/superpowers/specs/2026-11-21-lecture-croisee-design.md ;
plan : docs/superpowers/plans/2026-11-21-tache-13-lecture-croisee.md.

Ce module n'importe pas state (qui l'appelle) : il lit la base directement.
"""

import json
from typing import NamedTuple, Optional

from .messages import (
    DRAFTED, NEW, QUEUED, SENT, Event, Relay, common_number, other, referenced_replies,
)

KEY = "crossread"
PAST, PAST_KEPT = "crossread_past", 20  # questions remplacées, gardées pour reconnaître leurs réponses tardives
FREE = object()  # aucune lecture armée en cours : le choix habituel du prochain relais s'applique
READING = "lecture croisée avant ta décision"
OMITTED = "(contexte plus ancien omis)"
WAKE = "Stop hook blocking error from command"


class Question(NamedTuple):
    origin: str  # "commune" (tour de table) | "saisie" (demande tapée dans un panneau)
    number: int  # n° du tour de table, ou id de l'événement de soumission
    agent: str  # pour une saisie : l'assistant qui l'a reçue
    session: str  # pour une saisie : son fil


class Reading(NamedTuple):
    question: Question
    first: str  # qui part dans le premier relais
    relays: tuple  # réponses de cette question relayées et confirmées, dans l'ordre de remise
    decisions: tuple  # réponses de cette question qui finissent par [DÉCISION]
    incomplete: bool


# --- État -------------------------------------------------------------------------------------------------------

def _meta(db, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _load(db) -> list:
    try:
        readings = json.loads(_meta(db, KEY, "[]"))
    except ValueError:
        return []
    return readings if isinstance(readings, list) else []


def _save(db, readings: list) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (KEY, json.dumps(readings)))


def _question(item: dict) -> Question:
    return Question(item["origin"], int(item["number"]), item.get("agent", ""), item.get("session", ""))


def start(db, question: Question, first: str) -> None:
    """Ouvre la lecture d'une question. Si la lecture en cours est armée et a encore un relais dû, la nouvelle
    question attend son tour (précision 4) ; sinon elle la remplace."""
    item = {**question._asdict(), "first": first, "incomplete": False}
    readings = _load(db)
    current = _current_item(db, readings)
    if current is not None and _armed(db, current) and not _finished(db, current, readings):
        current["queued_behind"] = True  # son accord tardif reste écarté, même après une autre question (point 1)
        kept = readings[readings.index(current):]
        dropped, readings = readings[:readings.index(current)], [*kept, item]
    else:
        dropped, readings = readings, [item]
    _save(db, readings)
    known = [*json.loads(_meta(db, PAST, "[]")), *dropped]
    # Une question à l'accord écarté n'est jamais oubliée (troisième relecture) ; les autres, 20 au plus.
    plain = [entry for entry in known if not entry.get("queued_behind")]
    past = [entry for entry in known if entry.get("queued_behind") or entry in plain[-PAST_KEPT:]]
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (PAST, json.dumps(past)))


def note_submission(db, event: Event) -> bool:
    """Une demande tapée par l'utilisateur dans un panneau ouvre sa lecture. Ni une question commune, ni un
    réveil de Claude, ni un relais collé n'en sont. Renvoie vrai si c'est une demande de l'utilisateur."""
    if event.kind != "submitted" or common_number(event.text) or WAKE in event.text:
        return False
    if _meta(db, KEY, None) is None or referenced_replies(db, event):
        return False  # base d'avant la tâche 13, ou relais remis avec un ajout
    row = db.execute("""SELECT id, kind FROM events WHERE agent = ? AND session = ? AND request = ?
                        AND kind IN ('submitted', 'relay') ORDER BY id DESC LIMIT 1""",
                     (event.agent, event.session, event.request)).fetchone()
    if row is None or row[1] != "submitted" or not event.request:
        return False
    # Même avec l'auto coupé, la question est notée : une décision d'une question précédente ne s'impose plus
    # à la reprise. Aucun relais ne part tant que l'auto est coupé.
    start(db, Question("saisie", row[0], event.agent, event.session), event.agent)
    return True


# --- Appartenance à la question ---------------------------------------------------------------------------------

def _roots(db, question: Question) -> list:
    if question.origin == "commune":
        return [reply for (reply,) in db.execute(
            "SELECT reply_id FROM shared WHERE round = ? AND reply_id IS NOT NULL", (question.number,))]
    row = db.execute("SELECT request FROM events WHERE id = ?", (question.number,)).fetchone()
    if not row or not row[0]:
        return []
    return [reply for (reply,) in db.execute(
        """SELECT r.id FROM replies r JOIN events e ON e.id = r.event_id
           WHERE r.sender = ? AND r.session = ? AND e.request = ? AND e.id > ?""",
        (question.agent, question.session, row[0], question.number))]


def _members(db, question: Question) -> list:
    """Les réponses de cette question : ses premières réponses, puis celles qui répondent à l'une d'elles."""
    members = set(_roots(db, question))
    if members:
        for reply, answers in db.execute("SELECT id, answers FROM replies WHERE id > ? ORDER BY id",
                                         (min(members),)):
            if answers in members:
                members.add(reply)
    return sorted(members)


def _relays(db, members: list) -> list:
    if not members:
        return []
    marks = ",".join("?" * len(members))
    return [reply for (reply,) in db.execute(
        f"""SELECT id FROM replies WHERE id IN ({marks}) AND status = ? AND delivery IS NOT NULL
            ORDER BY delivery""", (*members, SENT))]


def is_decision(body: str) -> bool:
    """La règle unique d'une demande de décision : dernière ligne `[DÉCISION]`, espaces autour ignorés."""
    lines = body.rstrip().splitlines()
    return bool(lines) and lines[-1].strip() == "[DÉCISION]"


def _decisions(db, members: list) -> list:
    found = []
    for reply in members:
        body, = db.execute("SELECT body FROM replies WHERE id = ?", (reply,)).fetchone()
        if is_decision(body):
            found.append(reply)
    return found


def _armed(db, item: dict) -> bool:
    return bool(_decisions(db, _members(db, _question(item))))


def _finished(db, item: dict, readings: list) -> bool:
    """Deux relais confirmés, puis la réponse au second. Une question en file derrière elle se contente du
    second relais (précision 4) : sa réponse appartiendra à la lecture suivante."""
    if item.get("closed"):
        return True  # tâche 17 : son relais dû a été remplacé par une nouvelle question
    members = _members(db, _question(item))
    relays = _relays(db, members)
    if len(relays) < 2:
        return False
    if readings.index(item) < len(readings) - 1:
        return True
    return db.execute(f"SELECT 1 FROM replies WHERE answers = ? AND id IN ({','.join('?' * len(members))})",
                      (relays[1], *members)).fetchone() is not None


def _current_item(db, readings: list) -> Optional[dict]:
    """La lecture en cours : la première qui n'est pas finie, ou la dernière."""
    for item in readings:
        if not _finished(db, item, readings):
            return item
    return readings[-1] if readings else None


def current(db) -> Optional[Reading]:
    readings = _load(db)
    item = _current_item(db, readings)
    if item is None:
        return None
    members = _members(db, _question(item))
    return Reading(_question(item), item["first"], tuple(_relays(db, members)), tuple(_decisions(db, members)),
                   bool(item.get("incomplete")))


def belongs(db, reply_id: int) -> bool:
    reading = current(db)
    return reading is not None and reply_id in _members(db, reading.question)


def superseded(db, reply_id: int, replaced: bool = True) -> bool:
    """Vrai si cette réponse appartient de façon certaine à une question précédente, et non à la question en
    cours. Une réponse sans question connue n'est jamais écartée : une décision n'est jamais perdue.
    - Une décision d'une question remplacée ou mise en file ne s'impose plus (relecture de Codex, point 2).
    - Un accord n'est écarté que pour une question mise en file derrière une nouvelle (point 1) : l'accord d'une
      discussion qui se poursuit pendant une autre demande reste présenté (`replaced=False`)."""
    if belongs(db, reply_id):
        return False
    readings = _load(db)
    item = _current_item(db, readings)
    past = [entry for entry in json.loads(_meta(db, PAST, "[]")) if replaced or entry.get("queued_behind")]
    others = [entry for entry in [*past, *readings] if entry is not item]
    return any(reply_id in _members(db, _question(entry)) for entry in others)


def deferred(db) -> bool:
    """Vrai tant qu'une lecture armée n'est pas finie : aucune pause de décision ni d'accord."""
    readings = _load(db)
    item = _current_item(db, readings)
    return item is not None and _armed(db, item) and not _finished(db, item, readings)


# --- Relais et décisions ----------------------------------------------------------------------------------------

def due(db, ready=(NEW, DRAFTED)):
    """Le relais dû à la lecture armée en cours, None s'il n'est pas encore là, FREE hors lecture armée. Une
    réponse d'une autre question (ancien tour arrivé en retard) ne passe jamais devant (précision 1)."""
    readings = _load(db)
    item = _current_item(db, readings)
    if item is None or _finished(db, item, readings):
        return FREE
    if not _armed(db, item) and readings.index(item) == 0:
        return FREE  # une question sortie de la file garde ses deux relais, même sans décision (précision 4)
    reading = current(db)
    members = _members(db, reading.question)
    if not reading.relays:
        sender = reading.first
        candidates = [reply for reply in _roots(db, reading.question)
                      if _sender(db, reply) == sender]
    else:
        sender = other(reading.first)
        candidates = [reply for reply in members if _sender(db, reply) == sender
                      and _answers(db, reply) == reading.relays[0]]
        if len(reading.relays) >= 2:
            return None  # la réponse au second relais clôt la lecture : rien d'autre ne part avant
    for reply in reversed(candidates):
        body, status = db.execute("SELECT body, status FROM replies WHERE id = ?", (reply,)).fetchone()
        if status in ready:
            return Relay(reply, sender, other(sender), body)
    return None


def _sender(db, reply: int) -> str:
    return db.execute("SELECT sender FROM replies WHERE id = ?", (reply,)).fetchone()[0]


def _answers(db, reply: int) -> Optional[int]:
    return db.execute("SELECT answers FROM replies WHERE id = ?", (reply,)).fetchone()[0]


def decisions(db) -> list:
    """Les décisions de la lecture finie, pas encore présentées à l'utilisateur, dans l'ordre."""
    if deferred(db):
        return []
    reading = current(db)
    if reading is None or not reading.decisions:
        return []
    seen = int(_meta(db, "decision_seen", "0") or 0)
    return [reply for reply in reading.decisions if reply > seen]


def close_due(db, reply_id: int, recipient: str) -> None:
    """Tâche 17 : si cette réponse, remplacée par une question tapée chez `recipient`, était le relais dû à la
    lecture en cours (numéro et destinataire, collée ou non), cette lecture est close : la nouvelle question suit
    son propre parcours au lieu d'attendre un relais qui ne partira plus. Un autre brouillon effacé n'y touche pas."""
    expected = due(db, (NEW, DRAFTED, QUEUED))
    if not isinstance(expected, Relay) or (expected.reply_id, expected.recipient) != (reply_id, recipient):
        return
    readings = _load(db)
    item = _current_item(db, readings)
    if item is not None:
        item["closed"] = True
        _save(db, readings)


def restart_allowed(db, reply_id: int) -> bool:
    """La réponse à une question mise en file autorise la reprise, même si le relais à envoyer d'abord est celui
    dû à la question précédente (précision 7)."""
    readings = _load(db)
    return any(reply_id in _roots(db, _question(item)) for item in readings[1:])


def note_incomplete(db, reply_id: int) -> None:
    """Le relais de cette réponse a omis du contexte : la lecture en cours est incomplète."""
    readings = _load(db)
    item = _current_item(db, readings)
    if item is not None and reply_id in _members(db, _question(item)):
        item["incomplete"] = True
        _save(db, readings)
