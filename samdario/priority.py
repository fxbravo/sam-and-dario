"""La question qui a remplacé un relais garde la main (tâche 17, relecture du code par Codex, point 1).

Après le remplacement, la réponse à cette question est la prochaine à partir. Tant qu'elle n'est pas arrivée, aucun
autre relais ne part : une fin tardive d'un ancien tour ne passe pas devant. Cette priorité s'arrête dès que la
réponse est relayée, qu'une réponse plus récente de son auteur la dépasse, ou que son tour échoue. C'est aussi
elle, et elle seule, qui peut lever la pause « brouillon en attente ».
"""

import json
from typing import Optional

from .messages import DRAFTED, NEW, Event, Relay, other

KEY = "replacing"
FREE = object()  # aucune priorité en cours : le choix habituel du prochain relais s'applique


def note(db, event: Event, replaced: int, position: int) -> None:
    """La saisie `event`, journalisée en `position`, vient de remplacer le relais `replaced`."""
    entry = {"agent": event.agent, "session": event.session, "request": event.request, "replaced": replaced,
             "position": position}
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (KEY, json.dumps(entry)))


def _entry(db) -> Optional[dict]:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (KEY,)).fetchone()
    try:
        entry = json.loads(row[0]) if row else None
    except ValueError:
        return None
    return entry if isinstance(entry, dict) and entry.get("request") else None


def due(db):
    """La réponse à la question qui a remplacé un relais, si elle est prête ; None si elle reste à venir ; FREE
    hors priorité."""
    entry = _entry(db)
    if entry is None:
        return FREE
    agent, session, request = entry["agent"], entry["session"], entry["request"]
    row = db.execute("""SELECT r.id, r.body, r.status FROM replies r JOIN events e ON e.id = r.event_id
                        WHERE r.sender = ? AND r.session = ? AND e.request = ? ORDER BY r.id DESC LIMIT 1""",
                     (agent, session, request)).fetchone()
    newer = db.execute("SELECT 1 FROM replies WHERE sender = ? AND id > ? AND event_id > ? LIMIT 1",
                       (agent, row[0] if row else 0, entry["position"])).fetchone()
    if newer:
        return FREE  # l'auteur est passé à autre chose : sa dernière réponse suit le choix habituel
    if row is None:
        failed = db.execute("""SELECT 1 FROM events WHERE agent = ? AND session = ? AND request = ?
                               AND kind = 'failure' LIMIT 1""", (agent, session, request)).fetchone()
        return FREE if failed else None
    if row[2] not in (NEW, DRAFTED):
        return FREE  # déjà relayée (ou remplacée à son tour)
    return Relay(row[0], agent, other(agent), row[1])


def active(db) -> bool:
    return due(db) is not FREE
