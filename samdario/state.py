"""État privé d'une session Sam & Dario et événements reçus des deux assistants."""

import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import crossread as crossread
from . import priority as priority
from . import crossread_notes as notes
from . import threads as threads
from .messages import (
    DECISION_REASON, DRAFTED, MAX_BODY, NAMES, NEW, PASTED, PASTING, QUEUED, REPLACED, SENT, SESSION_KEYS, UNCERTAIN,
    Event, ReferencedReply, Relay, common, common_number, decision_reply, journal, journal_input, label,
    other, paste_text, referenced_replies, relay_headers,
)
__all_statuses__ = (REPLACED,)  # tâche 17 : statut exporté avec les autres (state.REPLACED)


def get(db, key: str) -> str:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return row[0]


def put(db, **values: object) -> None:
    db.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                   ((key, str(value)) for key, value in values.items()))


def _create_schema(db: sqlite3.Connection) -> None:
    # Une session du prototype peut encore appeler le script après sa mise à jour.
    legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'replies'").fetchone()
    if legacy:
        db.execute("ALTER TABLE replies RENAME TO prototype_replies")
    db.execute("""
        CREATE TABLE replies (
            id INTEGER PRIMARY KEY, sender TEXT NOT NULL, session TEXT NOT NULL, turn TEXT,
            body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', queue_id TEXT,
            claimed_at REAL, answers INTEGER, UNIQUE (sender, session, turn)
        )
    """)
    if legacy:
        db.execute("""
            INSERT INTO replies (id, sender, session, turn, body, status)
            SELECT id, sender, session, turn, body, CASE WHEN pasted THEN ? ELSE ? END
            FROM prototype_replies
        """, (DRAFTED, NEW))
        db.execute("DROP TABLE prototype_replies")
    db.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, agent TEXT, kind TEXT, session TEXT, turn TEXT, text TEXT
        )
    """)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    put(db, auto="off", reason="", count=0, max=8, waiter=0, waiter_seq=0, deadline=0,
        awaiting="", awaiting_reply=0, accord_seen=0, claude_session="", codex_thread="",
        codex_state="idle", cwd="")
    db.execute("PRAGMA user_version = 1")


@contextmanager
def transaction(state: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(state / "replies.db", timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version, = db.execute("PRAGMA user_version").fetchone()
        if version == 0:
            _create_schema(db)
        if version < 2:
            for column in ("target_session", "confirm_request", "confirm_session"):
                db.execute(f"ALTER TABLE replies ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            put(db, unidentified=0)
            for agent, key in SESSION_KEYS.items():
                # La version 1 captait les réponses mais ne suivait pas encore le fil actif.
                db.execute("""UPDATE meta SET value = COALESCE(
                                (SELECT session FROM replies WHERE sender = ? ORDER BY id DESC LIMIT 1), '')
                              WHERE key = ? AND value = ''""", (agent, key))
                db.execute("""UPDATE replies SET target_session = (SELECT value FROM meta WHERE key = ?)
                              WHERE sender = ? AND status IN (?, ?)""", (key, other(agent), DRAFTED, QUEUED))
            db.execute("PRAGMA user_version = 2")
        if version < 3:
            _migrate_context(db)
        if version < 4:
            _migrate_claims(db)
        if version < 5:
            _migrate_deliveries(db)
        if version < 6:
            _migrate_withdrawals(db)
        if version < 7:
            _migrate_previous_plan(db)
        if version < 8:
            _migrate_candidates(db)
        if version < 9:
            _migrate_rounds(db)
        if version < 10:
            threads.migrate(db, get(db, "codex_thread"))
        if version < 11:
            _migrate_field(db)
        if version < 12:
            db.execute("""CREATE TABLE IF NOT EXISTS agreements
                          (reply_id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, snapshot TEXT)""")
            db.execute("""INSERT OR IGNORE INTO meta SELECT 'accord_pause_seen', value FROM meta
                          WHERE key = 'accord_seen'""")
            previous = int(get(db, "accord_seen"))
            if previous:
                confirm_agreement(db, previous)
            db.execute("PRAGMA user_version = 12")
        yield db


def _migrate_context(db) -> None:
    db.execute("ALTER TABLE replies ADD COLUMN event_id INTEGER")
    db.execute("ALTER TABLE replies ADD COLUMN pasted TEXT")
    db.execute("""CREATE TABLE shared (
        id INTEGER PRIMARY KEY, round INTEGER NOT NULL, agent TEXT NOT NULL, text TEXT NOT NULL,
        event_id INTEGER UNIQUE, UNIQUE(round, agent)
    )""")
    db.execute("""CREATE TABLE seen (
        session TEXT NOT NULL, sender TEXT NOT NULL, upto INTEGER NOT NULL, PRIMARY KEY(session, sender)
    )""")
    db.execute("CREATE INDEX replies_context ON replies(sender, session, event_id)")
    last_reply, = db.execute("SELECT COALESCE(MAX(id), 0) FROM replies").fetchone()
    put(db, round_seq=0, legacy_last_reply=last_reply)

    # Les anciens collages ne contenaient que la réponse. Reconstituer la chronologie
    # du journal sans confondre un titre ou une notification rejouée avec une réponse retenue.
    db.execute("ALTER TABLE events RENAME TO previous_events")
    db.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY, agent TEXT, kind TEXT, session TEXT, turn TEXT, text TEXT,
        request TEXT NOT NULL DEFAULT ''
    )""")
    if last_reply:
        db.execute("CREATE INDEX previous_replies ON previous_events(agent, session, kind, turn, id)")
    matches = {}
    unlogged = []
    before, = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM previous_events").fetchone()
    old_replies = db.execute("SELECT id, sender, session, turn, body FROM replies ORDER BY id DESC")
    for reply_id, agent, session, turn, body in old_replies:
        # Sans identifiant de tour, le même texte peut être une nouvelle réponse légitime.
        # Avec un identifiant, seule la première notification du tour a produit la réponse.
        order = "ASC" if turn is not None else "DESC"
        row = db.execute(f"""SELECT id FROM previous_events WHERE agent = ? AND session = ? AND kind = 'reply'
                             AND turn IS ? AND text = ? AND id < ? ORDER BY id {order} LIMIT 1""",
                         (agent, session, turn, body, before)).fetchone()
        if row:
            before, = row
            matches[before] = reply_id
        else:
            unlogged.append((reply_id, Event(agent, "reply", session, turn, body, request=turn or "")))
    # Les réponses conservées par le premier prototype précèdent le journal.
    for reply_id, event in reversed(unlogged):
        event_id = journal(db, event)
        db.execute("UPDATE replies SET event_id = ? WHERE id = ?", (event_id, reply_id))
    for old_id, agent, kind, session, turn, text in db.execute(
            "SELECT id, agent, kind, session, turn, text FROM previous_events ORDER BY id"):
        event = Event(agent, kind, session, turn, text, request=turn or "")
        if kind == "submitted":
            journal_input(db, event, referenced_replies(db, event, candidates=False), legacy=True)
        else:
            event_id = journal(db, event)
            if old_id in matches:
                db.execute("UPDATE replies SET event_id = ? WHERE id = ?", (event_id, matches[old_id]))
    db.execute("DROP TABLE previous_events")
    db.execute("CREATE INDEX events_context ON events(agent, session, kind, id)")
    db.execute("CREATE INDEX events_request ON events(agent, session, request, kind)")
    db.execute("PRAGMA user_version = 3")


def initialize(state: Path, max_relays: int = 8, cwd: str = "") -> None:
    with transaction(state) as db:
        put(db, max=max_relays, cwd=cwd)


def normalize(agent: str, raw: object) -> Optional[Event]:
    if agent not in NAMES or not isinstance(raw, dict):
        return None
    hook = raw.get("hook_event_name")
    session = raw.get("session_id")
    turn = raw.get("turn_id") if agent == "codex" else None
    prompt = ""
    if agent == "codex" and raw.get("type") == "agent-turn-complete":
        kind, text = "reply", raw.get("last-assistant-message")
        session, turn = raw.get("thread-id"), raw.get("turn-id")
        inputs = raw.get("input-messages")
        if isinstance(inputs, list) and inputs and isinstance(inputs[-1], str):
            prompt = inputs[-1]
    elif agent == "claude" and hook == "Stop" and not raw.get("agent_id"):
        kind, text = "reply", raw.get("last_assistant_message")
    elif agent == "claude" and hook == "StopFailure" and not raw.get("agent_id"):
        # Remplace Stop quand l'API échoue : aucune réponse n'est enregistrée, seul le type d'erreur est gardé.
        error = raw.get("error")
        kind, text = "failure", error if isinstance(error, str) else ""
    elif hook == "UserPromptSubmit":
        kind, text = "submitted", raw.get("prompt")
    elif agent == "codex" and hook == "PermissionRequest":
        kind, text = "permission", ""
    else:
        return None
    if not isinstance(session, str) or not session.strip() or not isinstance(text, str):
        return None
    if (kind == "reply" and not text.strip()) or (turn is not None and not isinstance(turn, str)):
        return None
    if turn is not None and not turn.strip():
        turn = None
    request = raw.get("prompt_id") if agent == "claude" else turn
    if not isinstance(request, str) or not request.strip():
        request = ""
    return Event(agent, kind, session, turn, text, prompt, request)


def share(db, texts: dict[str, str]) -> int:
    number = int(get(db, "round_seq")) + 1
    thread = get(db, "codex_thread")
    rows = [(number, agent, texts[agent], thread if agent == "codex" else "") for agent in NAMES]
    db.executemany("INSERT INTO shared (round, agent, text, target_session) VALUES (?, ?, ?, ?)", rows)
    put(db, round_seq=number)
    crossread.start(db, crossread.Question("commune", number, "", ""), "codex")  # tâche 13
    return number


def round_open(db) -> Optional[int]:
    number = int(get(db, "round_seq"))
    if number > int(get(db, "round_closed")) and db.execute(
            "SELECT 1 FROM shared WHERE round = ? AND reply_id IS NULL LIMIT 1", (number,)).fetchone():
        return number
    return None


def close_round(db, round_id: int) -> None:
    put(db, round_closed=max(round_id, int(get(db, "round_closed"))))
    hold(db, round_id, "")


def hold(db, round_id: int, value: Optional[str] = None) -> str:
    if value is not None:
        db.execute("UPDATE shared SET hold = ? WHERE round = ? AND agent = 'claude'", (value, round_id))
        return value
    return db.execute("SELECT hold FROM shared WHERE round = ? AND agent = 'claude'", (round_id,)).fetchone()[0]


def common_text(db, round_id: int) -> str:
    text, = db.execute("SELECT text FROM shared WHERE round = ? AND agent = 'claude'", (round_id,)).fetchone()
    return common(round_id, text)


def codex_received(db, round_id: int) -> bool:
    return db.execute("SELECT 1 FROM shared WHERE round = ? AND agent = 'codex' AND event_id IS NOT NULL",
                      (round_id,)).fetchone() is not None


def take_broadcast(db, generation: int) -> Optional[str]:
    if not generation or int(get(db, "waiter")) != generation:
        return None
    number = round_open(db)
    if number is None or hold(db, number) != "ready":
        return None
    hold(db, number, "")
    put(db, waiter=0)
    return common_text(db, number)


def is_codex_user_thread(thread_id: str) -> bool:
    try:
        thread = str(uuid.UUID(thread_id))
    except ValueError:
        return False
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    for rollout in (codex_home / "sessions").glob(f"????/??/??/rollout-*-{thread}.jsonl"):
        try:
            with rollout.open(encoding="utf-8") as stream:
                record = json.loads(stream.readline())
        except (FileNotFoundError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            continue
        meta = record.get("payload")
        if (isinstance(meta, dict) and meta.get("id") == thread
                and meta.get("source") in ("cli", "vscode", "exec", "appServer")
                and meta.get("thread_source") in (None, "user")):
            return True
    return False


def record_reply(db, event: Event) -> Optional[int]:
    if event.kind != "reply":
        return None
    if event.agent == "codex":
        known = db.execute("SELECT 1 FROM replies WHERE sender = 'codex' AND session = ? LIMIT 1",
                           (event.session,)).fetchone()
        if not known and not is_codex_user_thread(event.session):
            journal(db, event)
            return None
    cursor = db.execute("""
        INSERT INTO replies (sender, session, turn, body) VALUES (?, ?, ?, ?)
        ON CONFLICT (sender, session, turn) DO NOTHING
    """, (event.agent, event.session, event.turn, event.text))
    if not cursor.rowcount:
        journal(db, event)
        return None
    reply_id = cursor.lastrowid
    if event.agent == "codex":
        active = get(db, "codex_thread")
        same_thread = not active or active == event.session
        overtaken = _overtaken(db, event, active or event.session)  # avant de journaliser le message reçu
        if event.prompt:
            submitted = event._replace(kind="submitted", text=event.prompt)
            recorded = _recorded_input_kind(db, submitted, notification=True)
            if recorded is None:
                _receive_input(db, submitted, same_thread)
            elif recorded == "relay":
                # Le hook a déjà traité ce message : sur le fil actif d'alors, l'accusé est fait. Le retour de
                # l'utilisateur sur ce fil depuis ne transforme pas après coup un message d'un autre fil en accusé.
                replies = _addressed(referenced_replies(db, submitted), submitted.session, False)
                if replies:
                    _confirm_labels(db, submitted, replies)
        # Une fin de tour plus ancienne que le dernier message reçu par le fil actif ne touche ni au fil ni à
        # son état : le tour suivant est peut-être en cours, ou attend une autorisation.
        if not overtaken and (same_thread or not _queued_turn(db, event)):
            threads.activate(db, active, event.session)
            put(db, codex_thread=event.session, codex_state="idle")
        elif not active:
            threads.activate(db, "", event.session)
            put(db, codex_thread=event.session)  # premier fil connu, même par une fin de tour tardive
    else:
        put(db, claude_session=event.session)
    event_id = journal(db, event)
    db.execute("UPDATE replies SET event_id = ? WHERE id = ?", (event_id, reply_id))
    _answer_round(db, event, reply_id)
    _attribute_reply(db, event, reply_id)
    return reply_id


def _recorded_input_kind(db, event: Event, notification: bool = False) -> Optional[str]:
    # Deux tours peuvent porter le même texte. Le numéro de tour reste prioritaire même
    # si la soumission suivante précède la notification du tour précédent.
    if event.request:
        row = db.execute("""SELECT kind FROM events WHERE agent = ? AND session = ?
                            AND request = ? AND kind IN ('submitted', 'relay') AND text = ? LIMIT 1""",
                         (event.agent, event.session, event.request, event.text)).fetchone()
        if row:
            return row[0]
    if notification:
        row = db.execute("""
            SELECT kind FROM events WHERE agent = 'codex' AND session = :session AND text = :text
              AND kind IN ('submitted', 'relay') AND (:turn IS NULL OR turn IS NULL)
              AND id > (SELECT COALESCE(MAX(event_id), 0) FROM replies WHERE sender = 'codex' AND session = :session)
            ORDER BY id DESC LIMIT 1
        """, {"session": event.session, "text": event.text, "turn": event.turn}).fetchone()
        if row:
            return row[0]
    return None


def record_submitted(db, event: Event) -> None:
    if _recorded_input_kind(db, event) is not None:
        return
    active = get(db, SESSION_KEYS[event.agent])
    same_thread = not active or active == event.session
    if event.agent == "codex":
        if (not active and is_codex_user_thread(event.session)) or (not same_thread and _typed_on_thread(db, event)):
            # L'utilisateur écrit dans ce fil : il y est revenu. Une copie automatique prise par la file
            # d'un ancien fil ne le dit pas.
            threads.activate(db, active, event.session)
            put(db, codex_thread=event.session)
        if same_thread or get(db, "codex_thread") == event.session:
            put(db, codex_state="busy")
    else:
        put(db, waiter=0)
    _receive_input(db, event, same_thread)


def _typed_on_thread(db, event: Event) -> bool:
    """Vrai si ce message de Codex a été écrit ou collé par l'utilisateur dans un fil à lui, et non pris par la
    file de ce fil comme copie automatique d'un relais."""
    known = db.execute("SELECT 1 FROM replies WHERE sender = 'codex' AND session = ? LIMIT 1",
                       (event.session,)).fetchone()
    if (not known and not is_codex_user_thread(event.session)) or common_number(event.text):
        return False  # la demande commune vient de Sam & Dario, pas d'un retour de l'utilisateur sur ce fil
    replies = referenced_replies(db, event)
    return not replies or any(reply.status == DRAFTED for reply in replies)


def record_permission(db, event: Event) -> None:
    journal(db, event)
    if event.agent == "codex" and get(db, "codex_thread") in ("", event.session):
        put(db, codex_state="permission")


def mark(db, reply_id: int, status: str, queue_id: Optional[str] = None) -> None:
    if status in (DRAFTED, QUEUED):
        row = db.execute("SELECT sender FROM replies WHERE id = ?", (reply_id,)).fetchone()
        if row is None:
            return
        recipient = other(row[0])
        db.execute("UPDATE replies SET target_session = ? WHERE id = ?",
                   (get(db, SESSION_KEYS[recipient]), reply_id))
    db.execute("UPDATE replies SET status = ?, queue_id = ? WHERE id = ?", (status, queue_id, reply_id))


def _receive_input(db, event: Event, confirm: bool = True) -> None:
    number = common_number(event.text)
    if number:
        row = db.execute("""SELECT id FROM shared WHERE round = ? AND agent = ? AND event_id IS NULL
                            AND hold IN ('', 'draft')
                            AND (target_session = ? OR (target_session = '' AND ?))""",
                         (number, event.agent, event.session, confirm)).fetchone()
        if row:
            db.execute("UPDATE shared SET event_id = ?, request = ?, hold = '' WHERE id = ?",
                       (journal(db, event), event.request, row[0]))
            if event.agent == "codex":
                threads.received(db, event.session, event.text)
                db.execute("UPDATE shared SET hold = 'ready' WHERE round = ? AND agent = 'claude' AND hold = 'uncertain'",
                           (number,))
            return
    replies = referenced_replies(db, event)
    journal_input(db, event, replies)
    replies = _addressed(replies, event.session, confirm)
    if replies:
        if event.agent == "codex":
            threads.received(db, event.session, event.text)
        _confirm_labels(db, event, replies)


def _answer_round(db, event: Event, reply_id: int) -> None:
    number = common_number(event.prompt) if event.agent == "codex" else None
    if number is None and not event.request:
        return
    # La réception fixe le fil. Le numéro nommé par Codex prime ; sinon il faut la demande native exacte.
    # Une seule ligne doit correspondre. Sans HAVING ni GROUP BY : SQLite avant 3.39 refuse HAVING seul.
    row = db.execute("""SELECT id FROM (SELECT MIN(s.id) AS id, count(*) AS n FROM shared s
                                         JOIN events e ON e.id = s.event_id
                                        WHERE s.agent = :agent AND e.session = :session
                                          AND (s.round = :number OR (:number IS NULL AND s.request = :request)))
                        WHERE n = 1""",
                     dict(agent=event.agent, session=event.session, number=number, request=event.request)).fetchone()
    if row:
        db.execute("UPDATE shared SET reply_id = ? WHERE id = ? AND reply_id IS NULL", (reply_id, row[0]))


def _addressed(replies: list[ReferencedReply], session: str, same_thread: bool) -> list[ReferencedReply]:
    """Les réponses dont cette soumission vaut accusé. Hors du fil actif, seule une réponse adressée à ce fil
    précis compte : un autre fil a pu devenir actif pendant que la copie attendait dans la file de son
    destinataire. La règle vaut pour le hook comme pour la notification de fin de tour."""
    if same_thread:
        return replies
    return [reply for reply in replies if reply.target == session]


def _overtaken(db, event: Event, active: str) -> bool:
    """Vrai si ce tour de Codex a été soumis avant le dernier message reçu par le fil actif : sa fin tardive ne
    dit pas que l'utilisateur est revenu sur son fil. Sans soumission journalisée pour ce tour (hook absent),
    rien ne permet de dater le tour : sa fin vaut retour sur ce fil, comme avant."""
    if not event.request:
        return False
    submitted, = db.execute("""SELECT MIN(id) FROM events WHERE agent = 'codex' AND session = ? AND request = ?
                               AND kind IN ('submitted', 'relay')""", (event.session, event.request)).fetchone()
    return submitted is not None and db.execute(
        """SELECT 1 FROM events WHERE agent = 'codex' AND session = ? AND kind IN ('submitted', 'relay')
           AND id > ? AND COALESCE(request, '') != ? LIMIT 1""", (active, submitted, event.request)).fetchone() is not None


def _queued_turn(db, event: Event) -> bool:
    """Vrai si ce tour de Codex a reçu une copie automatique adressée à son fil : la fin d'un tour mis en file
    pour un fil qui n'est plus le fil actif ne doit pas détourner les envois suivants ni effacer l'état du fil
    actif. Un tour tapé par l'utilisateur, même s'il cite une ancienne étiquette ou porte un brouillon manuel,
    reste le signe qu'il est passé sur ce fil."""
    if event.request:
        if db.execute("""SELECT 1 FROM shared s JOIN events e ON e.id = s.event_id
                         WHERE s.agent = 'codex' AND s.target_session = ? AND e.request = ?""",
                      (event.session, event.request)).fetchone():
            return True
        # Ce tour précis a confirmé la réception de la copie.
        return db.execute("""SELECT 1 FROM replies WHERE sender = 'claude' AND claim_id IS NOT NULL
                             AND target_session = ? AND confirm_session = ? AND confirm_request = ? LIMIT 1""",
                          (event.session, event.session, event.request)).fetchone() is not None
    probe = event._replace(kind="submitted", text=event.prompt)  # sans identifiant de tour : l'étiquette citée
    numbers = [reply.reply_id for reply in referenced_replies(db, probe) if reply.target == event.session]
    return any(db.execute("SELECT 1 FROM replies WHERE id = ? AND claim_id IS NOT NULL", (number,)).fetchone()
               for number in numbers)


def saw(db, session: str, reply_id: int) -> None:
    # Un ancien brouillon remis tel quel prouve seulement la lecture de sa réponse.
    # Son confirm_session permet à compose de l'écarter individuellement.
    db.execute("""
        INSERT INTO seen (session, sender, upto)
        SELECT ?, sender, event_id FROM replies WHERE id = ? AND event_id IS NOT NULL
          AND (pasted IS NOT NULL OR id > CAST((SELECT value FROM meta WHERE key = 'legacy_last_reply') AS INTEGER))
        ON CONFLICT(session, sender) DO UPDATE SET upto = MAX(seen.upto, excluded.upto)
    """, (session, reply_id))


def next_delivery(db) -> int:
    """Rang d'une remise, manuelle ou automatique. L'attente suit l'ordre des remises, pas celui des réponses."""
    rank = int(get(db, "delivery_seq")) + 1
    put(db, delivery_seq=rank)
    return rank


def _delivery_rank(db, reply: ReferencedReply) -> int:
    rank, = db.execute("SELECT delivery FROM replies WHERE id = ?", (reply.reply_id,)).fetchone()
    if reply.status == DRAFTED or rank is None:
        # Un brouillon part quand l'utilisateur appuie sur Entrée : c'est cette soumission, et non le
        # collage, qui fait l'ordre des remises manuelles. Une copie automatique garde le rang de sa réservation.
        rank = next_delivery(db)
        db.execute("UPDATE replies SET delivery = ? WHERE id = ?", (rank, reply.reply_id))
    return rank


def _confirm_labels(db, event: Event, replies: list[ReferencedReply]) -> None:
    latest = None
    # Deux brouillons sans rang envoyés dans le même message : le plus récent des deux l'emporte.
    for reply in sorted(replies, key=lambda found: found.reply_id):
        if reply.status in (DRAFTED, QUEUED, UNCERTAIN):
            db.execute("UPDATE replies SET status = ?, confirm_request = ?, confirm_session = ? WHERE id = ?",
                       (SENT, event.request, event.session, reply.reply_id))
            if reply.status == DRAFTED:
                # Un brouillon recollé puis envoyé se détache de la copie automatique : son accusé ne dit
                # rien de cette copie, dont le refus ou le retrait reste à appliquer.
                db.execute("UPDATE replies SET claim_id = NULL WHERE id = ?", (reply.reply_id,))
            elif event.agent == "codex":
                _establish(db, reply.reply_id, event.session)
            latest = max(latest or (0, 0), (_delivery_rank(db, reply), reply.reply_id))
        else:
            db.execute("""UPDATE replies SET confirm_request = ?, confirm_session = ?
                          WHERE id = ? AND confirm_session = ''""", (event.request, event.session, reply.reply_id))
        saw(db, event.session, reply.reply_id)
    if latest and latest[0] > int(get(db, "awaited_seq")):
        # Seule une remise plus récente que la dernière attendue déplace l'attente. La confirmation
        # tardive d'une ancienne remise met son historique à jour, sans rouvrir ni remplacer une attente.
        put(db, awaiting=event.agent, awaiting_reply=latest[1], awaited_seq=latest[0], unidentified=0)


def _establish(db, reply_id: int, session: str) -> None:
    """L'accusé de Codex établit le fil d'une copie pour laquelle une migration avait gardé deux candidats."""
    changed = db.execute("""UPDATE claims SET thread = ?, other_thread = '' WHERE other_thread != ''
                              AND ? IN (thread, other_thread) AND id = (SELECT claim_id FROM replies WHERE id = ?)""",
                         (session, session, reply_id)).rowcount
    if changed:
        db.execute("UPDATE replies SET target_session = ? WHERE id = ?", (session, reply_id))


def _attribute_reply(db, event: Event, reply_id: int) -> None:
    if get(db, "awaiting") != event.agent:
        return
    awaited = int(get(db, "awaiting_reply"))
    sender = other(event.agent)
    row = db.execute("""
        SELECT confirm_request, confirm_session, target_session, claim_id FROM replies
        WHERE id = ? AND sender = ? AND status = ?
    """, (awaited, sender, SENT)).fetchone()
    if row is None:
        return
    request, session, target, claim_id = row
    expected_session = session or target
    if expected_session and expected_session != event.session:
        return
    named_prompt = event.prompt if event.agent == "codex" else ""
    same_request = request and request == event.request and session == event.session
    other_request = bool(request and event.request and request != event.request)
    # Un renvoi manuel et la copie automatique d'une même réponse portent la même étiquette : quand les
    # deux identifiants de tour sont connus et différents, l'étiquette ne suffit pas à conclure. Seule une
    # étiquette en tête de relais compte, pas une étiquette citée dans une question.
    if same_request or (not other_request and (NAMES[sender], str(awaited)) in relay_headers(named_prompt, db)):
        db.execute("UPDATE replies SET answers = ? WHERE id = ?", (awaited, reply_id))
        put(db, awaiting="", awaiting_reply=0, unidentified=0)
        if (claim_id is None and get(db, "auto") != ON and event.text.rstrip().endswith("[ACCORD]")
                and not crossread.superseded(db, reply_id, replaced=False)
                and get(db, "reason") != DECISION_REASON and agreement_reply(db) == reply_id
                and decision_reply(db) is None):
            confirm_agreement(db, reply_id)
        return
    if named_prompt or other_request:
        return  # Une réponse à une autre demande laisse l'attente intacte.
    put(db, unidentified=awaited)


def latest_reply(db, sender: str):
    """La dernière réponse de cet assistant ; pour Codex, celle du fil actif : la fin tardive d'un tour d'un
    autre fil ne remplace pas ce que l'utilisateur voit à l'écran."""
    thread = get(db, "codex_thread") if sender == "codex" else ""
    return db.execute("""SELECT id, body, status FROM replies WHERE sender = ? AND (? = '' OR session = ?)
                         ORDER BY id DESC LIMIT 1""", (sender, thread, thread)).fetchone()


def next_relay(db) -> Optional[Relay]:
    if get(db, "awaiting") or round_open(db) is not None:
        return None
    due = crossread.due(db)  # tâche 13 : pendant une lecture armée, seul le relais dû à la question part
    if due is crossread.FREE:
        due = priority.due(db)  # tâche 17 : la réponse à la question qui a remplacé un relais part d'abord
    if due is not crossread.FREE and due is not priority.FREE:
        return due
    latest = {agent: latest_reply(db, agent) for agent in NAMES}
    if None in latest.values():
        return None
    sender = "codex"
    current = int(get(db, "round_seq"))
    round_replies = dict(db.execute("SELECT agent, reply_id FROM shared WHERE round = ?", (current,)))
    fresh_round = current > int(get(db, "round_closed")) and all(
        round_replies.get(agent) == row[0] and row[2] in (NEW, DRAFTED) for agent, row in latest.items())
    if not fresh_round and db.execute("SELECT 1 FROM replies WHERE status IN (?, ?) LIMIT 1", (SENT, QUEUED)).fetchone():
        sender = max(latest, key=lambda agent: latest[agent][0])
    reply_id, body, status = latest[sender]
    if status not in (NEW, DRAFTED):
        return None
    return Relay(reply_id, sender, other(sender), body)


def compose(db, relay: Relay, *, for_paste: bool = False) -> str:
    header = label(relay.sender, relay.reply_id)
    body = paste_text(relay.body) if for_paste else relay.body
    session, end = db.execute("SELECT session, event_id FROM replies WHERE id = ? AND sender = ?",
                              (relay.reply_id, relay.sender)).fetchone()
    active = get(db, SESSION_KEYS[relay.sender])
    if active and session != active:
        return f"{header}\n\n{body}"
    destination = get(db, SESSION_KEYS[relay.recipient])
    marker = db.execute("SELECT upto FROM seen WHERE session = ? AND sender = ?",
                        (destination, relay.sender)).fetchone()
    history = notes.history(db, relay.sender, destination, db.execute("""
        SELECT event_id AS position, 'reply', body FROM replies
        WHERE sender = :agent AND session = :session AND event_id > :start AND event_id < :end
          AND (confirm_session = '' OR confirm_session != :destination)
        UNION ALL
        SELECT e.id AS position, e.kind, COALESCE(source.text, e.text) FROM events e
        LEFT JOIN shared source ON source.event_id = e.id
        WHERE e.agent = :agent AND e.session = :session AND e.id > :start AND e.id < :end AND e.kind = 'submitted'
          AND NOT EXISTS (SELECT 1 FROM shared peer JOIN events receipt ON receipt.id = peer.event_id
                          WHERE peer.round = source.round AND peer.agent = :recipient AND receipt.session = :destination)
        ORDER BY position DESC
    """, {"agent": relay.sender, "session": session, "recipient": relay.recipient, "destination": destination,
          "start": marker[0] if marker else 0, "end": end}).fetchall(), end)
    name = NAMES[relay.sender]
    sections = []
    remaining = MAX_BODY - len(body)
    for index, (_, kind, text) in enumerate(history):
        if for_paste:
            text = paste_text(text)
        if kind == "reply" and len(text) > MAX_BODY:
            sections.append(notes.elsewhere(name))  # tâche 17 : jamais remise en entier, signalée
            continue
        if len(text) > remaining:
            sections.append(crossread.OMITTED)
            notes.note_omitted(db, relay.reply_id, destination, history[index:], history[:index])
            break
        remaining -= len(text)
        title = f"Réponse de {name}" if kind == "reply" else f"Demande de l'utilisateur à {name}"
        sections.append(f"— {title} :\n{text}")
    else:
        notes.note_omitted(db, relay.reply_id, destination, [], history)  # tout est remis
    message = f"{header}\n\n{body}"
    if sections:
        message = "\n\n".join([header, "Avant cette réponse, depuis le dernier relais :", *reversed(sections),
                               f"— Réponse la plus récente de {name} :\n{body}"])
    if relay.recipient == "codex":
        return threads.with_context(db, destination, message, remaining)
    return message


def draft(db, relay: Relay) -> str:
    text = compose(db, relay, for_paste=True)
    mark(db, relay.reply_id, DRAFTED)
    db.execute("UPDATE replies SET pasted = ?, delivery = NULL WHERE id = ?", (text, relay.reply_id))
    return text


def agreement_reply(db) -> Optional[int]:
    """Un accord porte sur le dernier échange des deux fils actifs, y compris si une fin arrive hors ordre."""
    pair = db.execute("""
        SELECT last.id, last.body, parent.body FROM replies AS last
        LEFT JOIN replies AS parent ON last.answers = parent.id
        WHERE ((last.sender = 'codex' AND last.session = :codex) OR (last.sender = 'claude' AND last.session = :claude))
          AND (parent.id IS NULL OR (parent.sender != last.sender AND
               ((parent.sender = 'codex' AND parent.session = :codex) OR
                (parent.sender = 'claude' AND parent.session = :claude))))
        ORDER BY last.id DESC LIMIT 1
    """, {agent: get(db, key) for agent, key in SESSION_KEYS.items()}).fetchone()
    if not pair or pair[2] is None:
        return None
    for body in pair[1:]:
        lines = body.rstrip().splitlines()
        if not lines or lines[-1].strip() != "[ACCORD]":
            return None
    return pair[0]


def agreed(db) -> bool:
    return agreement_reply(db) is not None


def confirm_agreement(db, reply_id: int) -> None:
    db.execute("INSERT INTO agreements (reply_id, token) VALUES (?, ?) ON CONFLICT(reply_id) DO NOTHING",
               (reply_id, str(uuid.uuid4())))
    put(db, accord_seen=reply_id)


# --- Format de la base pour le mode auto (les règles sont dans rules.py) ---

OFF, ON, PAUSE = "off", "on", "pause"


def _migrate_claims(db) -> None:
    # Une réservation garde ce qu'il faut pour être défaite sans rien perdre : l'état et le texte
    # d'avant, et l'activation qui l'a comptée.
    db.execute("""CREATE TABLE claims (
        id INTEGER PRIMARY KEY, reply_id INTEGER NOT NULL, recipient TEXT NOT NULL, run INTEGER NOT NULL,
        claimed_at REAL NOT NULL, prior_status TEXT NOT NULL, prior_pasted TEXT,
        cancel INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending'
    )""")
    db.execute("ALTER TABLE replies ADD COLUMN claim_id INTEGER")
    put(db, run=0)
    # Les bases des premières versions n'ont pas toutes les clés que lit le mode auto.
    defaults = {"auto": OFF, "reason": "", "count": 0, "max": 8, "waiter": 0, "waiter_seq": 0, "deadline": 0,
                "awaiting": "", "awaiting_reply": 0, "accord_seen": 0, "unidentified": 0, "codex_state": "idle"}
    db.executemany("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                   ((key, str(value)) for key, value in defaults.items()))
    db.execute("PRAGMA user_version = 4")


def _migrate_deliveries(db) -> None:
    db.execute("ALTER TABLE claims ADD COLUMN since REAL")  # heure du dernier changement d'état
    db.execute("UPDATE claims SET since = claimed_at")
    db.execute("ALTER TABLE replies ADD COLUMN delivery INTEGER")  # rang de la dernière remise de cette réponse
    # Une réponse en file sans réservation associée n'a aucun suivi (bases des versions précédentes) :
    # la rendre visible et recollable plutôt que de la laisser bloquer l'auto.
    db.execute("UPDATE replies SET status = ? WHERE status = ? AND claim_id IS NULL", (UNCERTAIN, QUEUED))
    put(db, delivery_seq=0, awaited_seq=0)
    db.execute("PRAGMA user_version = 5")


def _migrate_withdrawals(db) -> None:
    # Un retrait vise le fil où la copie a été mise en file, même si un autre fil Codex s'ouvre ensuite.
    db.execute("ALTER TABLE claims ADD COLUMN thread TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE claims ADD COLUMN queue_id TEXT")
    # Le fil visé est celui enregistré pour la réponse à la réservation ; le fil actif a pu changer depuis.
    db.execute("""UPDATE claims SET queue_id = (SELECT queue_id FROM replies WHERE replies.claim_id = claims.id),
                  thread = COALESCE(NULLIF((SELECT target_session FROM replies WHERE replies.id = claims.reply_id), ''),
                                    (SELECT value FROM meta WHERE key = 'codex_thread'))
                  WHERE recipient = 'codex'""")
    put(db, claude_failed=0)  # le dernier tour de Claude a échoué : rien ne viendra avant un nouveau tour
    db.execute("PRAGMA user_version = 6")


def _migrate_previous_plan(db) -> None:
    """Une base déjà au format 6 a pu être migrée par le plan 17, qui gardait l'échec de Claude comme simple
    drapeau. Les bases du plan 18 n'en sont pas modifiées. Le fil des copies est traité par le format 8."""
    # L'échec doit désigner son entrée au journal. Un drapeau du plan 17 prend le dernier échec journalisé, à
    # défaut la dernière entrée : seul un tour soumis après lui peut le lever.
    db.execute("""UPDATE meta SET value = CAST(COALESCE(
                      (SELECT MAX(id) FROM events WHERE agent = 'claude' AND kind = 'failure'),
                      (SELECT MAX(id) FROM events), 0) AS TEXT)
                  WHERE key = 'claude_failed' AND value != '0' AND NOT EXISTS (
                      SELECT 1 FROM events WHERE id = CAST(meta.value AS INTEGER) AND agent = 'claude'
                        AND kind = 'failure')""")
    db.execute("INSERT OR IGNORE INTO meta (key, value) SELECT 'count_before', value FROM meta WHERE key = 'count'")
    db.execute("PRAGMA user_version = 7")


def _migrate_candidates(db) -> None:
    """Sous le plan 17, la réservation et la réponse d'une copie suivie ont pu désigner deux fils différents,
    l'un ou l'autre étant le bon. Les deux restent candidats jusqu'à ce qu'un retrait ou l'accusé de Codex
    établisse la destination. Sous le plan 18, les deux sont toujours le même fil : rien ne change."""
    if "other_thread" not in {row[1] for row in db.execute("PRAGMA table_info(claims)")}:
        db.execute("ALTER TABLE claims ADD COLUMN other_thread TEXT NOT NULL DEFAULT ''")
    db.execute("""UPDATE claims SET other_thread = thread,
                         thread = (SELECT target_session FROM replies WHERE replies.claim_id = claims.id)
                  WHERE recipient = 'codex' AND state IN ('pending', 'withdrawing', 'uncertain') AND thread != ''
                    AND EXISTS (SELECT 1 FROM replies WHERE replies.claim_id = claims.id AND target_session != ''
                                AND target_session != claims.thread AND status IN (?, ?))""", (QUEUED, UNCERTAIN))
    db.execute("PRAGMA user_version = 8")


def _migrate_rounds(db) -> None:
    # Les anciens lanceurs n'ont pas la touche pour abandonner un tour incomplet.
    # Garder leur historique et n'appliquer la nouvelle barrière qu'aux prochains tours.
    columns = {row[1] for row in db.execute("PRAGMA table_info(shared)")}
    for name, definition in (("request", "TEXT"), ("reply_id", "INTEGER"),
                             ("hold", "TEXT NOT NULL DEFAULT ''"), ("target_session", "TEXT NOT NULL DEFAULT ''")):
        if name not in columns:
            db.execute(f"ALTER TABLE shared ADD COLUMN {name} {definition}")
    db.execute("INSERT OR IGNORE INTO meta (key, value) SELECT 'round_closed', value FROM meta WHERE key = 'round_seq'")
    db.execute("PRAGMA user_version = 9")


def _migrate_field(db) -> None:
    """Suivi du champ de Claude (tâche 8). Une session ouverte avant lui garde son dernier relais collé par
    l'auto et encore attendu : il occupe le champ jusqu'à la prochaine soumission chez Claude."""
    if db.execute("SELECT 1 FROM meta WHERE key = 'claude_field'").fetchone() is None:
        legacy = db.execute("""SELECT replies.id FROM replies JOIN claims ON claims.id = replies.claim_id
                               WHERE replies.status = ? AND claims.state IN (?, ?)
                               ORDER BY claims.id DESC LIMIT 1""", (QUEUED, PASTED, PASTING)).fetchone()
        last, = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        put(db, claude_field=f"relay:{legacy[0]}" if legacy else "", claude_field_at=last, claude_field_seq=0)
    db.execute("PRAGMA user_version = 11")
