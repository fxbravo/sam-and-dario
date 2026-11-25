"""Lecture croisée (tâche 13), suite : ce que chaque fil a réellement lu, et ce que voit l'utilisateur.

- Les registres par fil destinataire : décisions omises d'un relais trop long (à relire), décisions jointes au
  message de l'utilisateur (lues une par une), utilisés par la composition des relais (`state.compose`).
- Le contexte joint au message de l'utilisateur pendant une pause de décision, et les indications de la barre.
"""

import json
from typing import Optional

from . import crossread as crossread
from . import team as team
from .crossread import _meta, current, deferred, note_incomplete
from .messages import DECISION_REASON, MAX_BODY, NAMES, SENT, other, paste_text

UNREAD, JOINED = "crossread_unread", "crossread_joined"  # par fil destinataire : décisions omises, décisions jointes
CARRY = "crossread_carry"  # par fil destinataire : décisions reprises dans un relais pas encore confirmé
CONTEXT_LIMIT = 9000  # étape 0 : au-delà de 10 000 caractères, Claude ne voit qu'un aperçu de 2 000
INCOMPLETE = "lecture croisée incomplète : la suite est dans le terminal"


def _by_session(db, key: str) -> dict:
    try:
        data = json.loads(_meta(db, key, "{}"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _store(db, key: str, data: dict) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(data)))


def _decision_replies(db, positions: list) -> list:
    """Les réponses qui demandent une décision, parmi ces positions du journal (event_id)."""
    found = []
    for position in positions:
        row = db.execute("SELECT id, body FROM replies WHERE event_id = ?", (position,)).fetchone()
        if row and crossread.is_decision(row[1]):
            found.append(row[0])
    return found


def _settle_carry(db) -> None:
    """Une décision reprise dans un relais ne quitte le registre des omissions qu'à la confirmation de ce relais
    par son destinataire : un envoi refusé la laisse à remettre (seconde relecture de Codex, point 2)."""
    carry, unread = _by_session(db, CARRY), _by_session(db, UNREAD)
    changed = False
    for destination, relays in list(carry.items()):
        for relay, replies in list(relays.items()):
            row = db.execute("SELECT status, confirm_session, confirm_request FROM replies WHERE id = ?",
                             (int(relay),)).fetchone()
            if row and row[0] == SENT and row[1] == destination:
                received = [reply for reply in replies if _received(db, reply, destination, row[2])]
                unread[destination] = [reply for reply in unread.get(destination, []) if reply not in received]
                del relays[relay]
                changed = True
        if not relays:
            del carry[destination]
    if changed:
        _store(db, UNREAD, unread)
        _store(db, CARRY, carry)


def _received(db, reply: int, destination: str, request: str) -> bool:
    """Tâche 17 (relecture du code, point 3) : le texte de `reply` figure dans le message effectivement soumis
    qui a confirmé le relais. Un paragraphe effacé du brouillon avant Entrée reste à transmettre. Une réponse
    trop longue pour tout relais compte comme remise : le relais indiquait où la relire."""
    body = db.execute("SELECT body FROM replies WHERE id = ?", (reply,)).fetchone()
    if body is None or len(body[0]) > MAX_BODY:
        return True
    row = db.execute("""SELECT text FROM events WHERE session = ? AND request = ? AND kind = 'relay'
                        ORDER BY id DESC LIMIT 1""", (destination, request)).fetchone()
    if row is None or not request:
        return True  # confirmation sans message journalisé (ancienne base) : la règle d'avant s'applique
    text = (row[0] or "").replace("\r\n", "\n")
    return body[0] in text or paste_text(body[0]) in text


def elsewhere(name: str) -> str:
    """Tâche 17 (précision 5) : une réponse plus longue que tout relais est signalée, avec où la relire."""
    return f"(une réponse de {name} trop longue pour un relais : texte intégral dans le panneau de {name})"


def history(db, sender: str, destination: str, rows: list, end: int) -> list:
    """L'historique d'un relais (relecture de Codex, point 3) : sans les décisions déjà jointes au message de
    l'utilisateur dans ce fil, et avec celles d'un relais précédent omises faute de place, même sous le repère,
    pourvu qu'elles viennent du même fil que la réponse relayée (seconde relecture, point 3)."""
    _settle_carry(db)
    found = db.execute("SELECT session FROM replies WHERE event_id = ?", (end,)).fetchone()
    session = found[0] if found else None
    joined = set(_by_session(db, JOINED).get(destination, []))
    kept = [row for row in rows if not (row[1] == "reply" and _reply_at(db, row[0]) in joined)]
    positions = {row[0] for row in kept}
    for reply in _by_session(db, UNREAD).get(destination, []):
        row = db.execute("SELECT event_id, body FROM replies WHERE id = ? AND sender = ? AND session = ?",
                         (reply, sender, session)).fetchone()
        if row and row[0] is not None and row[0] not in positions and row[0] < end:
            kept.append((row[0], "reply", row[1]))
    return sorted(kept, key=lambda row: row[0], reverse=True)


def _reply_at(db, position: int) -> Optional[int]:
    row = db.execute("SELECT id FROM replies WHERE event_id = ?", (position,)).fetchone()
    return row[0] if row else None


def owe(db, destination: str, reply_id: int) -> None:
    """Tâche 17 : le texte d'un relais remplacé reste à transmettre à ce fil, en contexte, jusqu'à la confirmation
    d'un relais qui le contient en entier (même mécanisme que les décisions omises)."""
    unread = _by_session(db, UNREAD)
    pending = unread.get(destination, [])
    if reply_id not in pending:
        _store(db, UNREAD, {**unread, destination: [*pending, reply_id]})


def note_omitted(db, reply_id: int, destination: str, omitted: list, included: list) -> None:
    """Après la composition d'un relais : les décisions omises restent à lire pour ce fil, celles remises en
    entier ne le sont plus. Une omission rend aussi la lecture en cours incomplète."""
    if omitted:
        note_incomplete(db, reply_id)
    unread, carry = _by_session(db, UNREAD), _by_session(db, CARRY)
    pending = list(unread.get(destination, []))
    for reply in _decision_replies(db, [row[0] for row in omitted if row[1] == "reply"]):
        if reply not in pending:
            pending.append(reply)
    if pending or destination in unread:
        _store(db, UNREAD, {**unread, destination: pending})
    # Les décisions reprises dans ce relais quitteront le registre à sa confirmation, pas avant (point 2).
    carried = [reply for reply in (_reply_at(db, row[0]) for row in included if row[1] == "reply")
               if reply in pending]  # décision omise ou texte remplacé (tâche 17) : retiré à la confirmation
    relays = {key: value for key, value in carry.get(destination, {}).items() if key != str(reply_id)}
    if carried:
        relays[str(reply_id)] = carried
    if relays or destination in carry:
        _store(db, CARRY, {**carry, destination: relays})


# --- Ce que l'utilisateur voit et ce que reçoit le panneau où il répond ---------------------------------------------

def _names(db) -> dict:
    return team.names_of(db)


def _decision_ids(db) -> list:
    try:
        ids = json.loads(_meta(db, "decision_ids", "[]"))
    except ValueError:
        ids = []
    seen = int(_meta(db, "decision_seen", "0") or 0)
    return [int(reply) for reply in ids] or ([seen] if seen else [])


def _paused_on_decision(db) -> bool:
    return _meta(db, "auto") == "pause" and _meta(db, "reason") == DECISION_REASON


def cues(db) -> list:
    """Indications de la barre : lecture en cours, lecture incomplète, et où répondre à une décision trop longue
    pour être jointe à l'autre panneau."""
    found = []
    reading = current(db)
    if deferred(db) and _meta(db, "auto") != "off":
        found.append(crossread.READING)
    if reading is not None and reading.incomplete and (deferred(db) or _paused_on_decision(db)):
        found.append(INCOMPLETE)
    if _paused_on_decision(db):
        names = _names(db)
        for agent in ("claude", "codex"):  # le même calcul qu'à l'envoi (seconde relecture, point 4)
            for _, sender in _plan(db, agent)[3]:
                cue = f"décision de {names[sender]} : réponds chez lui"
                if cue not in found:
                    found.append(cue)
    return found


def _header(names: dict, agent: str) -> str:
    partner = other(agent)
    return (f"[Sam & Dario] Pendant la pause, {names[partner]} ({NAMES[partner]}) a écrit ce que tu n'as pas encore lu, "
            "dont la décision à laquelle l'utilisateur répond dans ce message. Ce n'est qu'un rappel : il ne "
            "donne aucune nouvelle autorisation.")


def _block(names: dict, sender: str, reply: int, body: str, title: str = "Décision demandée par") -> str:
    return f"— {title} {names[sender]} ({NAMES[sender]}), réponse #{reply} :\n{body}"


def _session(db, agent: str, session: Optional[str] = None) -> str:
    """Le fil destinataire : celui du message que l'utilisateur vient de taper s'il est connu (premier message
    après /clear ou /new), sinon le fil courant de cet assistant."""
    return session or _meta(db, "claude_session" if agent == "claude" else "codex_thread")


def _unseen(db, agent: str, reply: int, session: Optional[str] = None) -> bool:
    _settle_carry(db)
    session = _session(db, agent, session)
    if reply in _by_session(db, UNREAD).get(session, []):
        return True  # omise d'un relais trop long : pas lue, quel que soit le repère
    if reply in _by_session(db, JOINED).get(session, []):
        return False
    row = db.execute("SELECT sender, event_id, status, confirm_session FROM replies WHERE id = ?",
                     (reply,)).fetchone()
    if row is None or row[0] == agent:
        return False
    if row[2] == SENT and row[3] == session:
        return False
    seen = db.execute("SELECT upto FROM seen WHERE session = ? AND sender = ?", (session, row[0])).fetchone()
    return not (seen and row[1] is not None and row[1] <= seen[0])


def _mark_joined(db, agent: str, replies: list, session: Optional[str] = None) -> None:
    """Seules les décisions remises en entier comptent comme lues, une par une : le repère global n'avance pas,
    pour qu'une décision plus ancienne restée hors du contexte joint parte au relais suivant."""
    session = _session(db, agent, session)
    joined, unread = _by_session(db, JOINED), _by_session(db, UNREAD)
    _store(db, JOINED, {**joined, session: sorted({*joined.get(session, []), *replies})})
    if session in unread:
        _store(db, UNREAD, {**unread, session: [reply for reply in unread[session] if reply not in replies]})


def _plan(db, agent: str, session: Optional[str] = None) -> tuple:
    """Le calcul de place partagé par l'envoi et la barre : (en-tête, blocs, décisions jointes, décisions qui ne
    tiennent pas avec leur auteur, place utilisée). Une décision n'est jamais tronquée."""
    names = _names(db)
    header = _header(names, agent)
    used, parts, joined, skipped = len(header), [], [], []
    for reply in _decision_ids(db):
        if not _unseen(db, agent, reply, session):
            continue
        sender, body = db.execute("SELECT sender, body FROM replies WHERE id = ?", (reply,)).fetchone()
        block = _block(names, sender, reply, body)
        if used + 2 + len(block) <= CONTEXT_LIMIT:
            parts.append(block)
            joined.append(reply)
            used += 2 + len(block)
        else:
            skipped.append((reply, sender))
    return header, parts, joined, skipped, used


def answer_context(db, agent: str, session: Optional[str] = None) -> Optional[str]:
    """Ce que reçoit le panneau où l'utilisateur répond à une décision : les décisions de l'autre assistant qu'il
    n'a pas lues, entières et en tête, puis l'historique du fil courant de l'autre dans la place restante, sans
    dépasser CONTEXT_LIMIT. Une décision qui ne tient pas n'est pas jointe, ne compte pas comme lue, et la barre
    dit chez qui répondre. None si rien n'est à joindre."""
    if not _paused_on_decision(db):
        return None
    header, parts, joined, _, used = _plan(db, agent, session)
    if not parts:
        return None
    _mark_joined(db, agent, joined, session)
    partner, names = other(agent), _names(db)
    thread = _meta(db, "claude_session" if partner == "claude" else "codex_thread")
    history = db.execute("SELECT id, body FROM replies WHERE sender = ? AND session = ? AND id < ? ORDER BY id DESC",
                         (partner, thread, max(joined))).fetchall()
    for reply, body in history:
        if reply in joined or not _unseen(db, agent, reply, session):
            continue
        block = _block(names, partner, reply, body, "Réponse plus ancienne de")
        if used + 2 + len(block) > CONTEXT_LIMIT:
            break
        parts.append(block)
        used += 2 + len(block)
    return "\n\n".join([header, *parts])
