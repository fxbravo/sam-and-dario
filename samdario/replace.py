"""Un relais remplacé par une question de l'utilisateur (tâche 17).

Quand l'utilisateur envoie, dans le panneau d'un assistant, une question tapée à la place du relais collé dans ce
panneau, le relais est « remplacé » : il ne part plus seul, sa réservation est close comme une copie jamais
partie (le crédit revient à l'activation qui l'avait compté), l'attente de sa remise est levée, et son texte est
inscrit pour partir en contexte au premier relais suivant vers ce fil qui a la place. Un message qui contient le
bloc collé entier reste une remise de ce relais. Un collage en préparation et une copie incertaine ne sont jamais
remplacés. Plan : docs/superpowers/plans/2026-11-23-tache-17-relais-remplace.md.
"""

from typing import NamedTuple, Optional

from . import crossread as crossread
from . import crossread_notes as notes
from . import priority as priority
from . import rules as rules
from . import state as state
from . import threads as threads
from .messages import (
    DRAFTED, QUEUED, REPLACED, UNCERTAIN, Event, ReferencedReply, Relay, _manual_addition, journal, label, other,
    paste_text,
)


class Candidate(NamedTuple):
    reply_id: int
    status: str
    pasted: Optional[str]
    body: str
    claim_id: Optional[int]


def candidate(db, event: Event) -> Optional[Candidate]:
    """Le relais attendu par ce panneau et ce fil, relevé avant l'enregistrement de la saisie : en brouillon, ou
    collé par l'auto (réservation `PASTED`). Jamais pendant `PASTING`, ni pour une copie incertaine."""
    if event.kind != "submitted":
        return None
    row = db.execute("""SELECT r.id, r.status, r.pasted, r.body, r.claim_id FROM replies r
                        LEFT JOIN claims c ON c.id = r.claim_id
                        WHERE r.sender = ? AND r.target_session = ?
                          AND (r.status = ? OR (r.status = ? AND c.state = ?))
                        ORDER BY r.id DESC LIMIT 1""",
                     (other(event.agent), event.session, DRAFTED, QUEUED, rules.PASTED)).fetchone()
    if row is None:
        return None
    if db.execute("SELECT 1 FROM claims WHERE reply_id = ? AND state = ? LIMIT 1", (row[0], UNCERTAIN)).fetchone():
        return None  # une copie est peut-être encore dans la file : la pause de sécurité garde sa règle
    latest = db.execute("SELECT state FROM claims WHERE reply_id = ? ORDER BY id DESC LIMIT 1", (row[0],)).fetchone()
    if row[1] == DRAFTED and latest and latest[0] == rules.UNPASTED:
        return None  # OFF avant le collage : rien à l'écran, la réponse redevient libre (point 4)
    return Candidate(*row)


def last_event(db) -> int:
    return db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def fresh(db, event: Event, before: int) -> bool:
    """Vrai si cet événement vient d'ajouter une saisie : un hook rejoué, déjà journalisé, n'en ajoute pas."""
    return db.execute("""SELECT 1 FROM events WHERE id > ? AND agent = ? AND session = ? AND kind IN ('submitted', 'relay')
                         LIMIT 1""", (before, event.agent, event.session)).fetchone() is not None


def confirm_embedded(db, event: Event, found: Optional[Candidate], before: int) -> bool:
    """Un message qui contient le bloc collé entier, même précédé d'un commentaire sur la même ligne, est une
    remise de ce relais : elle est confirmée, et l'ajout de l'utilisateur est journalisé une seule fois."""
    if found is None or db.execute("""SELECT 1 FROM events WHERE id > ? AND agent = ? AND session = ?
                                          AND kind = 'relay' LIMIT 1""",
                                       (before, event.agent, event.session)).fetchone():
        return False  # déjà reconnu comme un relais : les règles habituelles s'en sont chargées
    snapshots = {text for text in (found.pasted, paste_text(found.pasted or ""),
                                   paste_text(f"{label(other(event.agent), found.reply_id)}\n\n{found.body}")) if text}
    if not any(snapshot in event.text for snapshot in snapshots):
        return False
    reference = ReferencedReply(found.reply_id, found.status, found.pasted, found.body, event.session)
    db.execute("""UPDATE events SET kind = 'relay' WHERE id > ? AND agent = ? AND session = ? AND kind = 'submitted'""",
               (before, event.agent, event.session))
    addition = _manual_addition(event, [reference])
    if addition:
        journal(db, event._replace(text=addition), "submitted")
    if event.agent == "codex":
        threads.received(db, event.session, event.text)
    state._confirm_labels(db, event, [reference])
    return True


def replace(db, event: Event, found: Candidate, position: int) -> int:
    """Marque le relais remplacé et en tire les conséquences. `position` : la saisie qui le remplace, journalisée.
    Renvoie son numéro."""
    crossread.close_due(db, found.reply_id, event.agent)  # avant le changement de statut (point 2)
    if found.claim_id is not None:
        relay = Relay(found.reply_id, other(event.agent), event.agent, found.body, found.claim_id)
        claim = rules._claim_of(db, relay)
        if claim is not None and claim.state in (rules.PENDING, rules.PASTED):
            rules._close(db, relay, claim, rules.REVERTED, delivered=False)  # crédit rendu une seule fois
    run = int(state.get(db, "run"))
    for claim_id, claim_run in db.execute("SELECT id, run FROM claims WHERE reply_id = ? AND state = ?",
                                          (found.reply_id, rules.LEFT)).fetchall():
        # Copie convertie en brouillon par une coupure : décomptée de même, dans l'activation qui l'avait comptée.
        db.execute("UPDATE claims SET state = ? WHERE id = ?", (rules.REVERTED, claim_id))
        if claim_run == run:
            state.put(db, count=max(0, int(state.get(db, "count")) - 1))
        elif claim_run == run - 1:
            state.put(db, count_before=max(0, int(state.get(db, "count_before")) - 1))
    db.execute("UPDATE replies SET status = ?, claim_id = NULL, delivery = NULL, queue_id = NULL WHERE id = ?",
               (REPLACED, found.reply_id))
    if int(state.get(db, "awaiting_reply")) == found.reply_id:
        state.put(db, awaiting="", awaiting_reply=0)
    notes.owe(db, event.session, found.reply_id)  # le texte remplacé part en contexte au relais suivant
    priority.note(db, event, found.reply_id, position)  # sa réponse part d'abord (point 1)
    return found.reply_id
