"""Règles du mode auto : bascule, pauses, réservation d'un relais, attente unique côté Claude.

Aucun effet extérieur ici : ni tmux ni sous-processus. Les canaux d'envoi sont dans auto.py.
"""

import json
from typing import NamedTuple, Optional

from . import crossread as crossread
from . import priority as priority
from . import crossread_notes as notes
from .messages import PASTED, PASTING  # relais collé chez Claude : il attend son Entrée sans délai
from .state import (
    DECISION_REASON, DRAFTED, MAX_BODY, NAMES, OFF, ON, PAUSE, QUEUED, SENT, UNCERTAIN, Relay,
    agreement_reply, codex_received, compose, confirm_agreement, decision_reply, get, hold, mark, next_delivery,
    next_relay, other, put, round_open,
)


WAIT_SECONDS = 3600
CONFIRM_SECONDS = 30  # délai laissé à Claude pour signaler la réception d'un relais
SEND_SECONDS = 120  # délai laissé à `codex queue`, pour un envoi comme pour un retrait
PASTE_SECONDS = 10  # au-delà, un collage resté « en cours » a été interrompu : sa remise devient incertaine
REASONS = {
    "accord": "accord trouvé",
    "decision": DECISION_REASON,
    "limite": "limite de relais atteinte",
    "manuel": "relais manuel requis vers Claude",
    "suivi": "suivi perdu",
    "hooks": "hooks Codex à valider dans /hooks",
    "long": "réponse trop longue pour l'auto",
    "incertain": "remise incertaine",
    "brouillon": "brouillon en attente chez {nom} : Entrée, ou Ctrl-b R pour le recoller",
    "inconnue": "réponse au relais #{n} non identifiée",
    "echec": "Claude n'a pas pu répondre : un nouveau tour de Claude est nécessaire",
}
# Identifiant de file d'une copie : celui de la réservation, ou celui qu'un hook de la version 5, encore en
# cours après la mise à jour, a écrit sur la réponse. À lire avec la jointure replies.claim_id = claims.id.
QUEUE_ID = "COALESCE(claims.queue_id, replies.queue_id)"
# Une réservation suit la copie automatique d'un relais, indépendamment d'un brouillon manuel de la même
# réponse. États : en attente, retrait confié à une opération, perdue de vue (UNCERTAIN), puis une issue.
PENDING, WITHDRAWING = "pending", "withdrawing"
REVERTED, WITHDRAWN, LEFT = "reverted", "withdrawn", "left"
UNPASTED = "unpasted"  # tâche 17 : OFF avant l'effet tmux du collage, rien n'est à l'écran


def pause(db, reason: str, **values: object) -> None:
    # La position du journal au moment de la pause : seule une demande tapée ensuite peut relancer l'échange.
    last, = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    put(db, auto=PAUSE, reason=REASONS[reason].format(**values), paused_at=last)


def pause_run(db, reason: str, run: int) -> None:
    """Met en pause l'activation `run` si elle n'a été ni coupée ni remplacée. Un constat commencé avant un
    appui (lecture des panneaux, retour d'une commande) n'écrase ni OFF ni une nouvelle activation ; il
    remplace en revanche la raison d'une pause de la même activation, plus ancienne que lui."""
    if get(db, "auto") != OFF and int(get(db, "run")) == run:
        pause(db, reason)


def refused(db, relay: Relay) -> None:
    """Le refus d'un envoi de l'activation en cours, déjà rendu par `revert`. Il met en pause, sauf après OFF :
    la réponse est libre, rien n'est à signaler. Si c'était le premier relais de cette activation, la reprise n'a jamais été effective :
    le compteur reprend sa valeur d'avant l'appui qui l'a ouverte, que l'envoi ait été réservé par
    l'appui ou par la barre d'état."""
    pause_run(db, "suivi", int(get(db, "run")))
    first, = db.execute("SELECT MIN(id) FROM claims WHERE run = ?", (int(get(db, "run")),)).fetchone()
    if first == relay.claim:
        put(db, count=get(db, "count_before"))


def new_waiter(db, now: float) -> int:
    generation = int(get(db, "waiter_seq")) + 1
    put(db, waiter_seq=generation, waiter=generation, deadline=now + WAIT_SECONDS)
    return generation


def waiter_valid(db, now: float) -> bool:
    return int(get(db, "waiter")) != 0 and now <= float(get(db, "deadline"))


class _Awaited(NamedTuple):
    status: str
    claim_id: Optional[int]
    state: Optional[str]
    claimed_at: Optional[float]
    since: Optional[float]
    queue_id: Optional[str]


def _awaited_delivery(db) -> Optional[_Awaited]:
    """La remise encore attendue, avec la réservation de sa copie automatique s'il y en a une."""
    recipient = get(db, "awaiting")
    if not recipient:
        return None
    row = db.execute(f"""SELECT replies.status, claims.id, claims.state, claims.claimed_at, claims.since, {QUEUE_ID}
                        FROM replies LEFT JOIN claims ON claims.id = replies.claim_id
                        WHERE replies.id = ? AND replies.sender = ?""",
                     (get(db, "awaiting_reply"), other(recipient))).fetchone()
    return _Awaited(*row) if row else None


def _is_overdue(db, awaited: _Awaited, now: float) -> bool:
    if awaited.status != QUEUED:
        return False
    if awaited.state == WITHDRAWING:
        # Un retrait confié puis interrompu ne doit pas bloquer la reprise.
        return now - (awaited.since or awaited.claimed_at) > SEND_SECONDS
    if awaited.state == PASTING:
        return now - awaited.claimed_at > PASTE_SECONDS
    if awaited.state != PENDING:
        return False
    if get(db, "awaiting") == "claude":
        return now - awaited.claimed_at > CONFIRM_SECONDS
    # Dans la file de Codex, un message peut attendre longtemps la fin d'un tour : seul l'envoi a un délai.
    return awaited.queue_id is None and now - awaited.claimed_at > SEND_SECONDS


def _stop_reason(db, now: float) -> Optional[tuple[str, dict[str, object]]]:
    """Raison pour laquelle la boucle ne peut pas continuer, ou None."""
    unidentified = int(get(db, "unidentified"))
    if unidentified:
        return "inconnue", {"n": unidentified}
    if int(get(db, "claude_failed")):
        return "echec", {}  # jusqu'à un nouveau tour de Claude
    # Seule la remise encore attendue peut bloquer : un relais manuel confirmé la remplace.
    awaited = _awaited_delivery(db)
    if awaited:
        if awaited.status == UNCERTAIN or _is_overdue(db, awaited, now):
            return "incertain", {}
        if awaited.status == DRAFTED:
            # Recollée après une remise incertaine : la pause tient jusqu'à l'envoi confirmé.
            return "brouillon", {"nom": NAMES[get(db, "awaiting")]}
    if not crossread.deferred(db):  # tâche 13 : ni décision ni accord avant la fin d'une lecture armée
        # Une décision ou un accord d'une question précédente ne s'impose plus (relecture de Codex, points 1 et 2).
        found = crossread.decisions(db)
        decision = found[-1] if found else decision_reply(db)
        if decision is not None and not found and crossread.superseded(db, decision):
            decision = None
        if decision is not None:
            return "decision", {"reply_id": decision, "all": found or [decision]}
        agreement = agreement_reply(db)
        if agreement is not None and crossread.superseded(db, agreement, replaced=False):
            agreement = None
        if agreement is not None and agreement > int(get(db, "accord_pause_seen")):
            return "accord", {"reply_id": agreement}
    relay = next_relay(db)
    if relay is None:
        return None
    if int(get(db, "count")) >= int(get(db, "max")):
        return "limite", {}
    if len(relay.body) > MAX_BODY:
        return "long", {}
    if db.execute("SELECT status FROM replies WHERE id = ?", (relay.reply_id,)).fetchone()[0] == DRAFTED:
        # Le brouillon est peut-être encore à l'écran : l'envoyer aussi par l'auto le ferait partir deux fois.
        # La protection porte sur cette réponse seulement ; une réponse plus récente partirait normalement.
        return "brouillon", {"nom": NAMES[relay.recipient]}
    # Vers Claude sans attente valide : son prochain réveil, ou un collage s'il est au repos (auto).
    return None


def settle(db, now: float) -> None:
    if get(db, "auto") != ON:
        return
    stopped = _stop_reason(db, now)
    if stopped is None:
        return
    reason, values = stopped
    if reason == "accord":
        # Noter la réponse permet à un nouvel appui de relancer la boucle au lieu de retomber en pause.
        confirm_agreement(db, values["reply_id"])
        put(db, accord_pause_seen=values["reply_id"])
    elif reason == "decision":
        put(db, decision_seen=values["reply_id"], decision_ids=json.dumps(values["all"]))  # Ctrl-b a : on poursuit
        dropped = agreement_reply(db)
        if dropped is not None and dropped > int(get(db, "accord_pause_seen")):
            # Précision 8 : un accord formé avant la décision est écarté dès cette pause ; l'export garde accord_seen.
            put(db, accord_dropped=dropped, accord_pause_seen=dropped)
    elif reason == "incertain":
        # Sam & Dario perd de vue cette copie. La trace reste, et l'auto ne la retentera pas seul :
        # Ctrl-b R, la confirmation tardive du destinataire, ou le retour tardif de la commande.
        awaited = _awaited_delivery(db)
        if awaited.status == QUEUED:
            db.execute("UPDATE claims SET state = ?, since = ? WHERE id = ?", (UNCERTAIN, now, awaited.claim_id))
            db.execute("UPDATE replies SET status = ? WHERE id = ?", (UNCERTAIN, get(db, "awaiting_reply")))
    pause(db, reason, **values)


def claim(db, recipient: str, now: float, generation: Optional[int] = None) -> Optional[Relay]:
    settle(db, now)
    if get(db, "auto") != ON:
        return None
    relay = next_relay(db)
    if relay is None or relay.recipient != recipient:
        return None
    if recipient == "claude" and generation != int(get(db, "waiter")):
        return None
    # Réserver : la confirmation viendra du destinataire, par l'étiquette du relais.
    claim_id = _reserve(db, relay, compose(db, relay), now, PENDING)
    if recipient == "claude":
        put(db, waiter=0)
    return relay._replace(claim=claim_id)


def _reserve(db, relay: Relay, text: str, now: float, kind: str) -> int:
    """Réserve la remise de `relay` : compteur, attente, rang de remise. Renvoie l'identifiant de réservation."""
    prior_status, prior_pasted = db.execute("SELECT status, pasted FROM replies WHERE id = ?",
                                            (relay.reply_id,)).fetchone()
    thread = get(db, "codex_thread") if relay.recipient == "codex" else ""  # le fil visé par l'envoi et ses retraits
    claim_id = db.execute("""INSERT INTO claims (reply_id, recipient, run, claimed_at, since, prior_status, prior_pasted,
                                                thread, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (relay.reply_id, relay.recipient, int(get(db, "run")), now, now, prior_status,
                           prior_pasted, thread, kind)).lastrowid
    rank = next_delivery(db)
    mark(db, relay.reply_id, QUEUED)
    db.execute("UPDATE replies SET pasted = ?, claim_id = ?, delivery = ? WHERE id = ?",
               (text, claim_id, rank, relay.reply_id))
    put(db, count=int(get(db, "count")) + 1, awaiting=relay.recipient, awaiting_reply=relay.reply_id,
        awaited_seq=rank)
    return claim_id


def paste_due(db, now: float) -> Optional[Relay]:
    """Le relais à coller en brouillon chez Claude : auto actif, Claude au repos et sans attente valide, champ
    de saisie libre de tout collage de Sam & Dario."""
    settle(db, now)
    if get(db, "auto") != ON or waiter_valid(db, now) or not claude_idle(db) or field_occupied(db):
        return None
    relay = next_relay(db)
    return relay if relay is not None and relay.recipient == "claude" else None


def reserve_paste(db, relay: Relay, text: str, now: float) -> tuple[int, int]:
    """Réservation enregistrée avant le collage chez Claude : une interruption ne peut pas le faire recommencer.
    Renvoie l'identifiant de réservation et le numéro du collage dans le champ de Claude."""
    claim_id = _reserve(db, relay, text, now, PASTING)
    return claim_id, occupy_field(db, f"relay:{relay.reply_id}")


def paste_allowed(db, relay: Relay, seq: int) -> bool:
    """Dernier contrôle, dans la transaction du collage : réservation encore en cours, auto toujours actif,
    champ inchangé depuis la réservation et Claude toujours au repos."""
    found = _claim_of(db, relay)
    return (found is not None and found.state == PASTING and get(db, "auto") == ON
            and _field_seq(db) == seq and field_occupied(db) and claude_idle(db))


def pasted(db, claim_id: int) -> None:
    """Le collage a eu lieu : l'Entrée de l'utilisateur confirmera la remise, sans délai."""
    db.execute("UPDATE claims SET state = ? WHERE id = ? AND state = ?", (PASTED, claim_id, PASTING))


def paste_failed(db, relay: Relay) -> None:
    """Le collage a échoué de façon certaine : la réponse redevient libre, sans compter de relais."""
    found = _claim_of(db, relay)
    if found is None or found.state != PASTING:
        return  # coupé entre-temps : OFF en a fait un brouillon
    _close(db, relay, found, REVERTED, delivered=False)
    put(db, claude_field="")
    pause_run(db, "manuel", found.run)


def paste_skipped(db, relay: Relay, seq: int) -> None:
    """Le collage n'a pas lieu : OFF, pause, Claude au travail ou champ repris entre la réservation et l'effet
    tmux. Rien n'a été collé par cette réservation : elle est annulée (réponse, attente, compteur), y compris
    si son délai l'a déjà rendue incertaine ou si OFF en a fait un brouillon. Seule exception : l'utilisateur
    a lui-même recollé ce relais entre-temps, et son brouillon fait foi."""
    found = _claim_of(db, relay)
    if found is None:
        return
    content, _ = _field(db)
    ours = _field_seq(db) == seq
    if not ours and content in (f"relay:{relay.reply_id}", f"manual:{relay.reply_id}"):
        if found.state == PASTING:
            pasted(db, relay.claim)  # Ctrl-b R l'a déjà collé : ne pas le coller une seconde fois
        return
    if found.state in (PASTING, UNCERTAIN):
        _close(db, relay, found, REVERTED, delivered=False)
    elif found.state in (LEFT, UNPASTED) and found.reply_status == DRAFTED and not found.owns_reply:
        # OFF l'avait changée en brouillon, mais elle n'a jamais été collée.
        db.execute("UPDATE replies SET status = ?, pasted = ? WHERE id = ? AND status = ? AND claim_id IS NULL",
                   (found.prior_status, found.prior_pasted, relay.reply_id, DRAFTED))
        if found.run == int(get(db, "run")):
            put(db, count=max(0, int(get(db, "count")) - 1))
        if int(get(db, "awaiting_reply")) == relay.reply_id:
            put(db, awaiting="", awaiting_reply=0)
    if ours:
        put(db, claude_field="")


def occupy_field(db, content: str) -> int:
    """Sam & Dario vient de coller `content` (`relay:<n>`, `manual:<n>` ou `common:<n>`) dans le champ de Claude.
    Renvoie le numéro de ce collage."""
    _field(db)  # une base d'avant le suivi du champ reçoit d'abord son état
    last, = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    seq = _field_seq(db) + 1
    put(db, claude_field=content, claude_field_at=last, claude_field_seq=seq)
    return seq


def _field_seq(db) -> int:
    row = db.execute("SELECT value FROM meta WHERE key = 'claude_field_seq'").fetchone()
    return int(row[0]) if row else 0


def _field(db) -> tuple[str, int]:
    """Contenu et repère du champ de Claude. Une session ouverte avant ce suivi le reçoit ici : son dernier
    relais collé par l'auto, encore attendu, occupe le champ jusqu'à la prochaine soumission chez Claude."""
    rows = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('claude_field', 'claude_field_at')"))
    if "claude_field" not in rows:
        legacy = db.execute("""SELECT replies.id FROM replies JOIN claims ON claims.id = replies.claim_id
                               WHERE replies.status = ? AND claims.state IN (?, ?)
                               ORDER BY claims.id DESC LIMIT 1""", (QUEUED, PASTED, PASTING)).fetchone()
        last, = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        rows = {"claude_field": f"relay:{legacy[0]}" if legacy else "", "claude_field_at": last}
        put(db, **rows)
    return rows["claude_field"], int(rows.get("claude_field_at", 0))


def field_kind(db) -> str:
    """Nature du dernier collage de Sam & Dario chez Claude : `relay`, `manual`, `common`, ou vide."""
    return _field(db)[0].partition(":")[0]


def field_occupied(db) -> bool:
    """Vrai tant qu'un collage de Sam & Dario peut encore se trouver dans le champ de Claude : aucune soumission
    tapée chez Claude ne l'a vidé depuis. Un réveil par le hook ne passe pas par le champ."""
    content, since = _field(db)
    return bool(content) and db.execute("""SELECT 1 FROM events WHERE agent = 'claude' AND kind IN ('submitted', 'relay')
                                           AND id > ? AND text NOT LIKE '%Stop hook blocking error from command%'
                                           LIMIT 1""", (since,)).fetchone() is None


def field_reply(db) -> Optional[int]:
    """Le relais collé chez Claude (par l'auto ou par Ctrl-b r) sur lequel portent Ctrl-b r et Ctrl-b R : tant
    que l'auto suit sa remise, ou, en brouillon, tant que le champ n'a pas été vidé par une soumission."""
    content, _ = _field(db)
    kind, _, number = content.partition(":")
    if kind not in ("relay", "manual"):
        return None  # une question « aux deux » se recolle par son propre chemin
    reply_id = int(number)
    row = db.execute("SELECT status, claim_id FROM replies WHERE id = ?", (reply_id,)).fetchone()
    if row is None:
        return None
    status, claim_id = row
    if status in (QUEUED, UNCERTAIN) and claim_id is not None:
        return reply_id
    return reply_id if status == DRAFTED and field_occupied(db) else None


def claude_idle(db) -> bool:
    """Vrai si le dernier message reçu par Claude a eu sa fin de tour ou son échec. L'issue tardive d'un ancien
    tour ne compte pas : seul le tour courant, repéré par son `prompt_id`, dit si Claude est au repos."""
    row = db.execute("""SELECT id, request FROM events WHERE agent = 'claude' AND kind IN ('submitted', 'relay')
                        ORDER BY id DESC LIMIT 1""").fetchone()
    if row is None:
        return True
    received, request = row
    return db.execute("""SELECT 1 FROM events WHERE agent = 'claude' AND kind IN ('reply', 'failure') AND id > ?
                           AND (? = '' OR request = ?) LIMIT 1""", (received, request, request)).fetchone() is not None


def autostart_value(db) -> str:
    """`1` : démarrage prévu ; `0` : aucun, annulé ou consommé. Le `go` de la version précédente (démarrage
    engagé) vaut `1` : la barre le reprend."""
    row = db.execute("SELECT value FROM meta WHERE key = 'autostart'").fetchone()
    value = row[0] if row else "0"
    return "1" if value == "go" else value


def autostart_pending(db) -> bool:
    """Démarrage du lancement prévu : ni Ctrl-b a ni une activation ne l'a encore consommé."""
    return autostart_value(db) == "1"


def launch_ready(db) -> bool:
    """Le démarrage du lancement peut avoir lieu : prévu, jamais activé, tour initial clos, et aucun échec de
    Claude en attente d'un nouveau tour. Revérifié dans la transaction qui active."""
    return (autostart_pending(db) and get(db, "auto") == OFF and int(get(db, "run")) == 0
            and int(get(db, "round_seq")) >= 1 and round_open(db) is None and not int(get(db, "claude_failed")))


LAUNCH_RETRY_SECONDS = 30  # une tentative de démarrage en cours (vérification des hooks) n'est pas doublée


def claim_launch(db, now: float) -> bool:
    """Vrai si ce processus doit tenter le démarrage du lancement : prêt, et aucune autre tentative récente."""
    row = db.execute("SELECT value FROM meta WHERE key = 'launch_try'").fetchone()
    if not launch_ready(db) or (row is not None and now - float(row[0]) < LAUNCH_RETRY_SECONDS):
        return False
    put(db, launch_try=now)
    return True


def restart_for(db, reply_id: int) -> bool:
    """La relance par une demande tapée vaut encore : pause relançable, et la réponse de cette demande est
    toujours celle à relayer (le fil actif n'a pas changé entre-temps)."""
    relay = next_relay(db)
    return restartable(db) and relay is not None and (relay.reply_id == reply_id
                                                       or crossread.restart_allowed(db, reply_id))


def launch_blocked(db) -> bool:
    """Claude a reçu la demande commune ouverte, puis a fini un tour sans y répondre (échec ou autre demande) :
    le tour de table ne se clôt pas seul, le démarrage attend son abandon."""
    number = round_open(db)
    if number is None:
        return False
    row = db.execute("""SELECT s.event_id FROM shared s WHERE s.round = ? AND s.agent = 'claude'
                        AND s.event_id IS NOT NULL AND s.reply_id IS NULL""", (number,)).fetchone()
    return row is not None and db.execute("""SELECT 1 FROM events WHERE agent = 'claude'
                                             AND kind IN ('reply', 'failure') AND id > ? LIMIT 1""",
                                          (row[0],)).fetchone() is not None


def restartable(db) -> bool:
    """Pause qu'une demande tapée par l'utilisateur peut lever : accord trouvé ou limite atteinte."""
    if get(db, "auto") == PAUSE and get(db, "reason").startswith("brouillon en attente"):
        # Tâche 17 : seule la question qui a remplacé le brouillon peut relancer, par sa réponse (restart_for).
        return _awaited_delivery(db) is None and priority.active(db)
    return get(db, "auto") == PAUSE and get(db, "reason") in (REASONS["accord"], REASONS["limite"],
                                                              REASONS["decision"])


def answers_typed_request(db, event) -> bool:
    """Vrai si cette fin de tour répond à une demande tapée par l'utilisateur pendant la pause : ni un relais,
    ni une demande commune, ni le réveil de Claude. Une phrase ajoutée à un brouillon compte. Une demande tapée
    avant la pause appartient à l'activation d'alors (conception, point 5)."""
    if event.kind != "reply" or not event.request:
        return False
    row = db.execute("SELECT value FROM meta WHERE key = 'paused_at'").fetchone()
    return db.execute("""SELECT 1 FROM events e WHERE e.agent = ? AND e.session = ? AND e.request = ?
                           AND e.kind = 'submitted' AND NOT EXISTS (SELECT 1 FROM shared s WHERE s.event_id = e.id)
                           AND e.id > (SELECT COALESCE(MAX(id), 0) FROM events WHERE kind = 'relay') AND e.id > ?
                         LIMIT 1""", (event.agent, event.session, event.request,
                                      int(row[0]) if row else 0)).fetchone() is not None


class _Claim(NamedTuple):
    state: str
    run: int
    cancel: int
    prior_status: str
    prior_pasted: Optional[str]
    reply_status: str
    owns_reply: bool  # la réponse est encore rattachée à cette copie automatique

    @property
    def governs(self) -> bool:
        """Vrai tant que le sort de la réponse dépend de cette copie : ni brouillon manuel recollé,
        ni réception confirmée par le destinataire."""
        return self.owns_reply and self.reply_status in (QUEUED, UNCERTAIN)

    @property
    def acknowledged(self) -> bool:
        """Le destinataire a confirmé la réception de la réponse encore rattachée à cette copie."""
        return self.owns_reply and self.reply_status == SENT


def _claim_of(db, relay: Relay) -> Optional[_Claim]:
    row = db.execute("""SELECT claims.state, claims.run, claims.cancel, claims.prior_status, claims.prior_pasted,
                               replies.status, COALESCE(replies.claim_id = claims.id, 0)
                        FROM claims JOIN replies ON replies.id = claims.reply_id
                        WHERE claims.id = ? AND claims.reply_id = ?""", (relay.claim, relay.reply_id)).fetchone()
    return _Claim(*row[:6], bool(row[6])) if row else None


def of_current_run(db, relay: Relay) -> bool:
    """Vrai si aucune nouvelle activation n'a eu lieu depuis cette réservation. Une coupure seule ne change
    pas d'activation ; après une reprise, l'issue d'un ancien envoi ne doit ni mettre en pause ni décompter."""
    row = db.execute("SELECT run FROM claims WHERE id = ? AND reply_id = ?", (relay.claim, relay.reply_id)).fetchone()
    return row is not None and row[0] == int(get(db, "run"))


def _close(db, relay: Relay, found: _Claim, outcome: str, delivered: bool, now: Optional[float] = None) -> None:
    """Applique l'issue de la copie automatique. La réponse n'est touchée que si son sort en dépend encore."""
    db.execute("UPDATE claims SET state = ?, since = COALESCE(?, since) WHERE id = ?", (outcome, now, relay.claim))
    run = int(get(db, "run"))
    if not delivered and found.run == run:
        # La copie automatique n'est jamais partie : elle n'est plus comptée, même si un renvoi manuel
        # a pris le relais. Le décompte appartient à l'activation qui l'avait comptée.
        put(db, count=max(0, int(get(db, "count")) - 1))
    elif not delivered and found.run == run - 1:
        # Comptée par l'activation précédente, elle l'était aussi dans la valeur d'avant l'appui, que
        # `refused` rend : son refus établi l'en retire.
        put(db, count_before=max(0, int(get(db, "count_before")) - 1))
    if not found.governs:
        return
    if delivered:
        db.execute("UPDATE replies SET status = ?, queue_id = NULL WHERE id = ?", (SENT, relay.reply_id))
        return
    db.execute("UPDATE replies SET status = ?, pasted = ?, queue_id = NULL, claim_id = NULL WHERE id = ?",
               (found.prior_status, found.prior_pasted, relay.reply_id))
    if int(get(db, "awaiting_reply")) == relay.reply_id:
        put(db, awaiting="", awaiting_reply=0)


def revert(db, relay: Relay) -> bool:
    """L'envoi a échoué de façon certaine : la copie n'est jamais partie. Faux si ce retour ne s'applique plus."""
    found = _claim_of(db, relay)
    if found is None or found.state not in (PENDING, UNCERTAIN):
        return False
    if found.state == PENDING and not found.governs:
        return False  # Le destinataire a confirmé la réception : elle fait foi.
    if found.acknowledged:
        return False  # Même après le délai d'envoi, l'accusé de cette copie fait foi.
    _close(db, relay, found, REVERTED, delivered=False)
    return True


def _lose(db, relay: Relay, found: _Claim, now: Optional[float]) -> None:
    """Sam & Dario perd de vue la copie : l'identifiant reste, pour un retrait ou une confirmation ultérieurs."""
    db.execute("UPDATE claims SET state = ?, since = COALESCE(?, since) WHERE id = ?", (UNCERTAIN, now, relay.claim))
    if found.governs:
        db.execute("UPDATE replies SET status = ? WHERE id = ?", (UNCERTAIN, relay.reply_id))


def unsure(db, relay: Relay, now: float) -> bool:
    """`codex queue` n'a pas dit si la copie est partie. Faux si ce retour ne s'applique plus, notamment
    si Codex a déjà confirmé la réception : elle fait foi."""
    found = _claim_of(db, relay)
    if found is None or found.state != PENDING or found.acknowledged:
        return False
    _lose(db, relay, found, now)
    return True


def release(db, relay: Relay, cancelled: bool, now: Optional[float] = None) -> bool:
    """Issue du retrait de la copie dans la file de Codex : retirée (vrai) ou non prouvée (faux). « Plus dans
    la file » ne prouve pas la réception : seul le hook de Codex la prouve. Faux si ce retour ne s'applique plus."""
    found = _claim_of(db, relay)
    if found is None or found.state not in (WITHDRAWING, UNCERTAIN):
        return False
    if cancelled:
        _close(db, relay, found, WITHDRAWN, delivered=False)
        return True
    if found.acknowledged:
        _close(db, relay, found, LEFT, delivered=True)  # la réception confirmée fait foi
        return False
    _lose(db, relay, found, now)
    return True


def turn_failed(db, session: str, request: str, failure_id: int) -> None:
    """Claude n'a pas pu finir son tour (StopFailure) : la réponse attendue de ce tour ne viendra pas.
    L'auto ne repart pas avant la fin d'un tour de Claude commencé après cet échec (`failure_id`, son entrée
    au journal). Sous OFF, l'auto reste OFF ; le prochain appui le signale. L'échec d'un autre tour que
    celui du relais attendu est seulement journalisé. Un nouveau signalement d'un tour déjà signalé est
    seulement journalisé : il ne doit ni déplacer le repère, ni remplacer celui d'un échec plus récent."""
    active = get(db, "claude_session")
    if active and active != session:
        return
    if request and db.execute("""SELECT 1 FROM events WHERE agent = 'claude' AND kind = 'failure' AND session = ?
                                 AND request = ? AND id < ? LIMIT 1""", (session, request, failure_id)).fetchone():
        return
    submitted = _first_submission(db, session, request)
    if submitted is not None and db.execute("""
            SELECT 1 FROM events AS reply WHERE reply.agent = 'claude' AND reply.session = ? AND reply.kind = 'reply'
              AND (SELECT MIN(id) FROM events WHERE agent = 'claude' AND session = ? AND request = reply.request
                   AND request != '' AND kind IN ('submitted', 'relay')) > ? LIMIT 1""",
            (session, session, submitted)).fetchone():
        return  # échec tardif d'un tour plus ancien qu'un tour déjà terminé : seulement journalisé
    if get(db, "awaiting") == "claude":
        row = db.execute("SELECT confirm_request FROM replies WHERE id = ?", (get(db, "awaiting_reply"),)).fetchone()
        expected = row[0] if row else ""
        # Sans identifiant d'un côté ou de l'autre, l'échec est celui du tour en cours.
        if expected and request and expected != request:
            return
        put(db, awaiting="", awaiting_reply=0)
    put(db, claude_failed=failure_id)
    if get(db, "auto") == ON:
        pause(db, "echec")


def turn_answered(db, session: str, request: str) -> None:
    """Fin d'un tour de Claude. Elle lève l'échec seulement si ce tour a été soumis pour la première fois
    après lui : ni la fin tardive d'un ancien tour, ni un doublon de la soumission échouée."""
    failed = int(get(db, "claude_failed"))
    first = _first_submission(db, session, request)
    if failed and first is not None and first > failed:
        put(db, claude_failed=0)


def _first_submission(db, session: str, request: str) -> Optional[int]:
    """Entrée au journal de la première soumission de cette demande à Claude, ou None."""
    if not request:
        return None
    return db.execute("""SELECT MIN(id) FROM events WHERE agent = 'claude' AND session = ? AND request = ?
                         AND kind IN ('submitted', 'relay')""", (session, request)).fetchone()[0]


def enqueued(db, relay: Relay, queue_id: str, now: float) -> bool:
    """Au retour de `codex queue`. Vrai : la copie reste en file. Faux : elle est à retirer, et l'appelant
    est la seule opération chargée de ce retrait ; il en donnera le résultat à `release`."""
    found = _claim_of(db, relay)
    if found is None:
        return False
    # L'identifiant reste sur la réservation, même tardif : chaque retrait ultérieur en a besoin.
    db.execute("UPDATE claims SET queue_id = ? WHERE id = ?", (queue_id, relay.claim))
    if found.state in (PENDING, UNCERTAIN) and found.acknowledged:
        # Codex a confirmé la réception pendant l'appel, même après son délai : la remise enregistrée fait foi.
        return True
    if found.state == PENDING and found.governs and not found.cancel:
        _hold(db, relay, queue_id)
        return True
    if found.state not in (PENDING, UNCERTAIN):
        return False  # Une issue a déjà été appliquée à cette copie.
    # Coupure pendant l'envoi, ou copie perdue de vue : son sort se règle par un retrait, quoi qu'ait
    # fait l'utilisateur entre-temps avec un brouillon manuel de la même réponse.
    if found.state == PENDING and found.governs:
        _hold(db, relay, queue_id)
    db.execute("UPDATE claims SET state = ?, since = ? WHERE id = ?", (WITHDRAWING, now, relay.claim))
    return False


def _hold(db, relay: Relay, queue_id: str) -> None:
    """La réponse est en file chez Codex. Son destinataire reste le fil fixé à la réservation : un autre fil
    devenu actif pendant l'envoi ne doit pas détourner l'accusé de réception."""
    db.execute("UPDATE replies SET status = ?, queue_id = ? WHERE id = ?", (QUEUED, queue_id, relay.reply_id))


class Queued(NamedTuple):
    """Une copie en file chez Codex, confiée pour retrait, avec le fil où elle a été mise en file."""
    relay: Relay
    thread: str
    queue_id: str


def toggle(db, now: float, hooks_ok: bool) -> list[Queued]:
    if get(db, "auto") == ON:
        pending = db.execute(f"""SELECT replies.id, replies.body, claims.id, claims.thread, {QUEUE_ID}
                                FROM claims JOIN replies ON replies.id = claims.reply_id AND replies.claim_id = claims.id
                                WHERE claims.recipient = 'codex' AND claims.state = ? AND replies.status = ?
                                ORDER BY claims.id""", (PENDING, QUEUED)).fetchall()
        # La coupure reste acquise à chaque envoi en cours, même si l'auto est réactivé avant son retour.
        db.executemany("UPDATE claims SET cancel = 1 WHERE id = ?", ((row[2],) for row in pending))
        # Un message déjà en file est confié une seule fois pour retrait, même après OFF, ON, OFF.
        queued = [Queued(Relay(reply_id, "claude", "codex", body, claim_id), thread, queue_id)
                  for reply_id, body, claim_id, thread, queue_id in pending if queue_id is not None]
        db.executemany("UPDATE claims SET state = ?, since = ? WHERE id = ?",
                       ((WITHDRAWING, now, item.relay.claim) for item in queued))
        # Un relais collé chez Claude reste à l'écran : il devient un brouillon ordinaire, protégé comme tel.
        on_screen = db.execute("""SELECT claims.id, claims.reply_id, claims.state FROM claims
                                  JOIN replies ON replies.claim_id = claims.id
                                  WHERE claims.state IN (?, ?) AND replies.status = ?""",
                               (PASTED, PASTING, QUEUED)).fetchall()
        # Tâche 17 : un collage encore en préparation n'a rien mis à l'écran ; une saisie ne peut pas le remplacer.
        db.executemany("UPDATE claims SET state = ? WHERE id = ?",
                       ((LEFT if state == PASTED else UNPASTED, claim_id) for claim_id, _, state in on_screen))
        db.executemany("UPDATE replies SET status = ?, claim_id = NULL WHERE id = ?",
                       ((DRAFTED, reply_id) for _, reply_id, _ in on_screen))
        put(db, auto=OFF, reason="", waiter=0)
        return queued
    put(db, reason="", autostart=0)  # toute activation consomme le démarrage du lancement
    if not hooks_ok:
        pause(db, "hooks")
        return []
    before = get(db, "count")
    put(db, auto=ON, count=0)
    settle(db, now)
    if get(db, "auto") == ON:
        put(db, run=int(get(db, "run")) + 1, count_before=before)
    else:
        # La remise à zéro appartient à la reprise effective.
        put(db, count=before)
    return []


def status_line(db, now: float) -> str:
    settle(db, now)
    auto = get(db, "auto")
    if auto == ON:
        text = f"AUTO {get(db, 'count')}/{get(db, 'max')}"
    elif auto == PAUSE:
        text = f"PAUSE — {get(db, 'reason')}"
    elif autostart_pending(db) and launch_blocked(db):
        text = ("AUTO — démarrage suspendu : Claude n'a pas répondu à la demande commune "
                "(Ctrl-b b puis ligne vide pour l'abandonner)")
    elif autostart_pending(db) and int(get(db, "claude_failed")):
        text = "AUTO — démarre après une réponse de Claude : son dernier tour a échoué"
    elif autostart_pending(db):
        text = "AUTO — démarre après les deux réponses"
    else:
        text = "OFF"
    awaited = _awaited_delivery(db)
    if auto == ON and awaited and awaited.state == PASTED and awaited.status == QUEUED:
        text += " · Entrée chez Claude"
    elif auto == ON and not waiter_valid(db, now) and not claude_idle(db):
        relay = next_relay(db)
        if relay is not None and relay.recipient == "claude":
            # Claude rédige : le relais partira à la fin de son tour. Un tour interrompu n'en a pas (guide).
            text += " · relais à Claude après son tour"
    for cue in notes.cues(db):  # tâche 13
        text += f" · {cue}"
    if get(db, "codex_state") == "permission":
        text += " · Codex : autorisation demandée"
    number = round_open(db)
    if number is not None:
        delivery = hold(db, number)
        progress = {"uncertain": "envoi à Codex incertain", "draft": "Entrée chez Claude"}.get(delivery)
        if delivery == "ready" and not waiter_valid(db, now):
            progress = "collage en attente chez Claude"
        if progress is None:
            progress = "en cours" if codex_received(db, number) else "en file chez Codex"
        text += f" · tour de table : {progress}"
    return text
