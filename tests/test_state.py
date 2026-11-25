import itertools
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from samdario import state as state


class StateTest(unittest.TestCase):
    def test_claude_events(self):
        stop = {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "C"}
        self.assertEqual(state.normalize("claude", stop), state.Event("claude", "reply", "s", None, "C"))
        self.assertIsNone(state.normalize("claude", {**stop, "agent_id": "child"}))
        submit = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "p"}
        self.assertEqual(state.normalize("claude", submit), state.Event("claude", "submitted", "s", None, "p"))
        self.assertEqual(state.normalize("claude", {**submit, "prompt_id": "k1"}),
                         state.Event("claude", "submitted", "s", None, "p", "", "k1"))
        self.assertEqual(state.normalize("claude", {**stop, "prompt_id": "k1"}),
                         state.Event("claude", "reply", "s", None, "C", "", "k1"))

    def test_codex_notification_and_hooks(self):
        done = {"type": "agent-turn-complete", "thread-id": "t", "turn-id": "1",
                "last-assistant-message": "R", "input-messages": ["avant", "dernier"]}
        self.assertEqual(state.normalize("codex", done), state.Event("codex", "reply", "t", "1", "R", "dernier", "1"))
        for messages in (None, [], "texte", [{}]):
            with self.subTest(messages=messages):
                self.assertEqual(state.normalize("codex", {**done, "input-messages": messages}).prompt, "")
        submit = {"hook_event_name": "UserPromptSubmit", "session_id": "t", "turn_id": "1", "prompt": "p"}
        self.assertEqual(state.normalize("codex", submit), state.Event("codex", "submitted", "t", "1", "p", "", "1"))
        permission = {"hook_event_name": "PermissionRequest", "session_id": "t", "turn_id": "1"}
        self.assertEqual(state.normalize("codex", permission), state.Event("codex", "permission", "t", "1", "", "", "1"))

    def test_unusable_events_are_ignored(self):
        stop = {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "C"}
        for raw in (None, [], {}, {**stop, "session_id": ""}, {**stop, "last_assistant_message": "  "},
                    {**stop, "hook_event_name": "Other"}):
            with self.subTest(raw=raw):
                self.assertIsNone(state.normalize("claude", raw))
        self.assertIsNone(state.normalize("codex", stop))

    def test_label(self):
        self.assertEqual(state.label("codex", 7), "[Réponse de Codex #7]")
        self.assertEqual(state.label(state.other("codex"), 8), "[Réponse de Claude #8]")

    def test_initial_state_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="samdario état #", dir="/tmp") as directory:
            folder = Path(directory)
            state.initialize(folder, max_relays=3, cwd="/tmp/mon projet")
            with self.assertRaises(RuntimeError), state.transaction(folder) as db:
                self.assertEqual(state.get(db, "max"), "3")
                self.assertEqual(state.get(db, "cwd"), "/tmp/mon projet")
                state.put(db, auto="on", count=1)
                raise RuntimeError("interruption")
            with state.transaction(folder) as db:
                self.assertEqual((state.get(db, "auto"), state.get(db, "count")), ("off", "0"))

    def test_version_one_session_keeps_its_draft_and_its_settings(self):
        with tempfile.TemporaryDirectory(prefix="samdario migration ", dir="/tmp") as directory:
            folder = Path(directory)
            with closing(sqlite3.connect(folder / "replies.db")) as db, db:
                db.executescript("""
                    CREATE TABLE replies (id INTEGER PRIMARY KEY, sender TEXT NOT NULL, session TEXT NOT NULL,
                        turn TEXT, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', queue_id TEXT,
                        claimed_at REAL, answers INTEGER, UNIQUE(sender, session, turn));
                    CREATE TABLE events (id INTEGER PRIMARY KEY, agent TEXT, kind TEXT, session TEXT, turn TEXT, text TEXT);
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO meta VALUES ('max', '3'), ('claude_session', ''), ('codex_thread', '');
                    INSERT INTO replies (id, sender, session, body, status) VALUES (5, 'claude', 'c', 'C0', 'drafted'),
                        (6, 'codex', 'r', 'R0', 'new');
                    PRAGMA user_version = 1;
                """)
            with state.transaction(folder) as db:
                self.assertEqual(state.get(db, "unidentified"), "0")
                state.record_submitted(db, state.Event("codex", "submitted", "r", "t", "[Réponse de Claude #5]", "", "t"))
            with state.transaction(folder) as db:
                self.assertEqual(state.get(db, "max"), "3")
                self.assertEqual(db.execute("SELECT id, status, confirm_request FROM replies ORDER BY id").fetchall(),
                                 [(5, state.SENT, "t"), (6, state.NEW, "")])

    def test_version_two_draft_does_not_acknowledge_context_it_never_carried(self):
        with tempfile.TemporaryDirectory(prefix="samdario historique ", dir="/tmp") as directory:
            folder = Path(directory)
            with closing(sqlite3.connect(folder / "replies.db")) as db, db:
                db.executescript("""
                    CREATE TABLE replies (id INTEGER PRIMARY KEY, sender TEXT, session TEXT, turn TEXT, body TEXT,
                        status TEXT DEFAULT 'new', queue_id TEXT, claimed_at REAL, answers INTEGER,
                        target_session TEXT DEFAULT '', confirm_session TEXT DEFAULT '', confirm_request TEXT DEFAULT '',
                        UNIQUE(sender, session, turn));
                    CREATE TABLE events (id INTEGER PRIMARY KEY, agent TEXT, kind TEXT, session TEXT, turn TEXT, text TEXT);
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                    INSERT INTO meta VALUES ('claude_session', 'c'), ('codex_thread', 'x'), ('count', '3'), ('awaiting', '');
                    INSERT INTO replies (id,sender,session,body,status,target_session,confirm_session)
                        VALUES (1,'claude','c','Deuxième avis','drafted','x',''), (2,'claude','c','Premier avis','sent','x','x'),
                               (3,'claude','c','Deuxième avis','new','','');
                    PRAGMA user_version = 2;
                """)
                old_sent = "[Réponse de Claude #2]\n\nPremier avis\n\nEt le délai ?"
                db.executemany("INSERT INTO events (agent, kind, session, text) VALUES (?,?,?,?)", [
                    ("claude", "submitted", "c", "Question privée avant mise à jour"),
                    ("claude", "reply", "c", "Premier avis"), ("codex", "submitted", "x", old_sent),
                    ("claude", "reply", "c", "Deuxième avis")])
            with state.transaction(folder) as db:
                self.assertEqual(state.get(db, "count"), "3")
                # Il manque l'événement du premier des deux « Deuxième avis » (ancien prototype).
                self.assertLess(db.execute("SELECT event_id FROM replies WHERE id=1").fetchone()[0],
                                db.execute("SELECT event_id FROM replies WHERE id=3").fetchone()[0])
                old_draft = "[Réponse de Claude #1]\n\nDeuxième avis"
                state.record_submitted(db, state.Event("codex", "submitted", "x", "t", old_draft, "", "t"))
                new = state.record_reply(db, state.Event("claude", "reply", "c", None, "Après mise à jour"))
                text = state.compose(db, state.Relay(new, "claude", "codex", "Après mise à jour"))
                self.assertIn("Question privée avant mise à jour", text)
                self.assertNotIn("Premier avis", text)
                self.assertEqual(text.count("Deuxième avis"), 1)
                self.assertEqual(db.execute("SELECT kind, text FROM events WHERE agent='codex' ORDER BY id").fetchall(),
                                 [("relay", old_sent), ("submitted", "Et le délai ?"), ("relay", old_draft)])


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="samdario état #", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        state.initialize(self.state)
        self.ids = itertools.count(1)
        user_thread = patch("samdario.state.is_codex_user_thread", return_value=True)
        user_thread.start()
        self.addCleanup(user_thread.stop)

    def db(self):
        return state.transaction(self.state)

    def fresh(self):
        return f"p{next(self.ids)}"

    def event(self, agent, kind, text, request, session, prompt=""):
        turn = (request or None) if agent == "codex" else None
        return state.Event(agent, kind, session or f"fil-{agent}", turn, text, prompt, request)

    def submit(self, agent, prompt, session=None, request=None):
        if request is None:
            request = self.fresh()
        with self.db() as db:
            state.record_submitted(db, self.event(agent, "submitted", prompt, request, session))
        return request

    def reply(self, sender, body, request, session=None, prompt=""):
        with self.db() as db:
            return state.record_reply(db, self.event(sender, "reply", body, request, session, prompt))

    def turn(self, agent, text, body):
        request = self.submit(agent, text)
        return self.reply(agent, body, request, prompt=text if agent == "codex" else "")

    def deliver(self, reply_id, recipient):
        with self.db() as db:
            body, = db.execute("SELECT body FROM replies WHERE id = ?", (reply_id,)).fetchone()
            pasted = state.draft(db, state.Relay(reply_id, state.other(recipient), recipient, body))
        return self.submit(recipient, pasted)

    def opening(self):
        with self.db() as db:
            number = state.share(db, {"claude": "Question", "codex": "Question"})
        prompt = state.common(number, "Question")
        return self.turn("claude", prompt, "C0"), self.turn("codex", prompt, "R0")

    def exchange(self):
        _, codex_reply = self.opening()
        return self.deliver(codex_reply, "claude")


class OrderTest(Base):
    def test_waits_for_both_initial_replies(self):
        self.turn("claude", "Question", "C1")
        with self.db() as db:
            self.assertIsNone(state.next_relay(db))

    def test_first_relay_goes_from_codex_to_claude(self):
        codex = self.turn("codex", "Question", "R1")
        self.turn("claude", "Question", "C1")
        with self.db() as db:
            self.assertEqual(state.next_relay(db), state.Relay(codex, "codex", "claude", "R1"))

    def test_one_relay_at_a_time_then_alternates(self):
        request = self.exchange()
        with self.db() as db:
            self.assertIsNone(state.next_relay(db))
        claude = self.reply("claude", "C1", request)
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "")
            self.assertEqual(state.next_relay(db), state.Relay(claude, "claude", "codex", "C1"))
            self.assertEqual(state.get(db, "count"), "0")

    def test_erased_draft_stays_due_and_edited_draft_counts_as_sent(self):
        _, codex = self.opening()
        with self.db() as db:
            state.mark(db, codex, state.DRAFTED)
        self.submit("claude", "Une question sans rapport")
        with self.db() as db:
            self.assertEqual(state.next_relay(db).reply_id, codex)
        request = self.submit("claude", f"Mon avis.\n[Réponse de Codex #{codex}]\n\nR0 retouché\n\nEt la suite ?")
        with self.db() as db:
            self.assertIsNone(state.next_relay(db))
            self.assertEqual(state.get(db, "awaiting"), "claude")
            row = db.execute("SELECT status, confirm_request, confirm_session FROM replies WHERE id = ?", (codex,))
            self.assertEqual(row.fetchone(), (state.SENT, request, "fil-claude"))

    def test_submission_marks_exactly_the_draft_it_carries(self):
        _, first = self.opening()
        second = self.turn("codex", "Précise", "R1")
        with self.db() as db:
            state.mark(db, first, state.DRAFTED)
            state.mark(db, second, state.DRAFTED)
        self.submit("claude", f"[Réponse de Codex #{first}]\n\nR0")
        with self.db() as db:
            rows = dict(db.execute("SELECT id, status FROM replies WHERE sender = 'codex'"))
            self.assertEqual(rows, {first: state.SENT, second: state.DRAFTED})
            self.assertEqual(state.get(db, "awaiting_reply"), str(first))

    def test_submitting_two_drafts_waits_for_the_newer_one(self):
        _, first = self.opening()
        second = self.turn("codex", "Précise", "R1")
        with self.db() as db:
            state.mark(db, first, state.DRAFTED)
            state.mark(db, second, state.DRAFTED)
        self.submit("claude", f"[Réponse de Codex #{second}]\n\nR1\n\n[Réponse de Codex #{first}]\n\nR0")
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting_reply"), str(second))
            self.assertEqual(db.execute("SELECT count(*) FROM replies WHERE status = 'sent'").fetchone(), (2,))

    def test_label_typed_by_hand_or_from_another_thread_changes_nothing(self):
        _, codex = self.opening()
        text = f"[Réponse de Codex #{codex}]\n\nR0"
        self.submit("claude", text)
        with self.db() as db:
            state.mark(db, codex, state.DRAFTED)
        self.submit("claude", text, session="autre-fil")
        self.submit("claude", f"[Réponse de Claude #{codex}]\n\nR0")
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "")
            self.assertEqual(state.next_relay(db).reply_id, codex)

    def test_another_thread_confirms_only_a_reply_addressed_to_it(self):
        _, codex = self.opening()
        text = f"[Réponse de Codex #{codex}]\n\nR0"
        with self.db() as db:
            state.mark(db, codex, state.QUEUED)
            db.execute("UPDATE replies SET target_session = '' WHERE id = ?", (codex,))  # destinataire inconnu
        self.submit("claude", text, session="autre-fil")  # hors du fil actif : pas un accusé
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (codex,)).fetchone(), (state.QUEUED,))
            db.execute("UPDATE replies SET target_session = 'autre-fil' WHERE id = ?", (codex,))
        self.submit("claude", text + " ", session="autre-fil")  # adressée à ce fil : accusé reçu
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (codex,)).fetchone(), (state.SENT,))

    def test_codex_notification_from_another_thread_confirms_only_a_reply_addressed_to_it(self):
        claude, _ = self.opening()
        with self.db() as db:
            state.mark(db, claude, state.DRAFTED)  # brouillon collé, jamais envoyé
            db.execute("UPDATE replies SET target_session = '' WHERE id = ?", (claude,))  # fil encore inconnu
        self.reply("codex", "RB", self.fresh(), session="fil-B")  # B devient le fil actif
        text = f"[Réponse de Claude #{claude}]\n\nC0"
        request = self.submit("codex", text, session="fil-A")  # question sur l'ancien fil A, qui cite l'étiquette
        self.reply("codex", "RA", request, session="fil-A", prompt=text)  # sa notification de fin de tour
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (claude,)).fetchone(),
                             (state.DRAFTED,))
            self.assertEqual(state.get(db, "awaiting"), "")

    def test_late_end_of_a_turn_queued_for_an_older_thread_keeps_the_active_one(self):
        for with_hook in (True, False):
            with self.subTest(with_hook=with_hook):
                self.setUp()
                claude = self.reply("claude", "C1", self.exchange())
                with self.db() as db:  # copie automatique en file pour le fil A, celui de Codex
                    state.mark(db, claude, state.QUEUED, "q1")
                    db.execute("UPDATE replies SET claim_id = 1 WHERE id = ?", (claude,))
                    state.put(db, awaiting="codex", awaiting_reply=claude)
                self.reply("codex", "RB", self.submit("codex", "Ailleurs", session="fil-B"), session="fil-B",
                           prompt="Ailleurs")  # l'utilisateur passe sur le fil B
                with self.db() as db:
                    state.record_permission(db, self.event("codex", "permission", "", self.fresh(), "fil-B"))
                text = f"[Réponse de Claude #{claude}]\n\nC1"
                request = self.submit("codex", text) if with_hook else self.fresh()  # A prend la copie
                codex = self.reply("codex", "R1", request, prompt=text)  # fin tardive du tour de A
                with self.db() as db:
                    self.assertEqual((state.get(db, "codex_thread"), state.get(db, "codex_state")),
                                     ("fil-B", "permission"))
                    self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (codex,)).fetchone(),
                                     (claude,))

    def test_turn_typed_on_an_older_thread_still_moves_the_active_one(self):
        for case in ("étiquette déjà remise", "brouillon manuel"):
            with self.subTest(case):
                self.setUp()
                claude = self.reply("claude", "C1", self.exchange())
                text = f"[Réponse de Claude #{claude}]\n\nC1"
                if case == "étiquette déjà remise":
                    with self.db() as db:  # copie automatique en file pour A
                        state.mark(db, claude, state.QUEUED, "q1")
                        db.execute("UPDATE replies SET claim_id = 1 WHERE id = ?", (claude,))
                    self.reply("codex", "R1", self.submit("codex", text), prompt=text)  # remise à A, répondue
                else:
                    with self.db() as db:
                        state.draft(db, state.Relay(claude, "claude", "codex", "C1"))  # collé dans A
                self.reply("codex", "RB", self.submit("codex", "Ailleurs", session="fil-B"), session="fil-B",
                           prompt="Ailleurs")  # B devient actif
                request = self.submit("codex", text + "\n\nEt maintenant ?")  # l'utilisateur revient sur A
                self.reply("codex", "R2", request, prompt=text + "\n\nEt maintenant ?")
                with self.db() as db:
                    self.assertEqual(state.get(db, "codex_thread"), "fil-codex")

    def test_late_end_of_a_turn_typed_before_the_last_one_of_the_active_thread_keeps_it(self):
        self.opening()  # le fil A (fil-codex) est actif
        question = self.submit("codex", "Question sur A")
        self.reply("codex", "RB", self.submit("codex", "Ailleurs", session="fil-B"), session="fil-B",
                   prompt="Ailleurs")  # saisie plus récente sur B : B devient actif
        self.submit("codex", "Encore", session="fil-B")
        with self.db() as db:
            state.record_permission(db, self.event("codex", "permission", "", self.fresh(), "fil-B"))
        self.reply("codex", "RA", question, prompt="Question sur A")  # fin tardive du tour de A
        with self.db() as db:
            self.assertEqual((state.get(db, "codex_thread"), state.get(db, "codex_state")), ("fil-B", "permission"))

    def test_label_cited_in_a_sentence_does_not_send_the_draft(self):
        for with_hook in (True, False):
            with self.subTest(with_hook=with_hook):
                self.setUp()
                claude, _ = self.opening()
                with self.db() as db:
                    state.draft(db, state.Relay(claude, "claude", "codex", "C0"))  # Ctrl-b r chez Codex
                question = f"Que signifie le numéro dans [Réponse de Claude #{claude}] ?"  # brouillon remplacé
                request = self.submit("codex", question) if with_hook else self.fresh()
                codex = self.reply("codex", "R1", request, prompt=question)
                with self.db() as db:
                    self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (claude,)).fetchone(),
                                     (state.DRAFTED,))
                    self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (codex,)).fetchone(),
                                     (None,))

    def test_text_typed_on_the_label_line_leaves_the_draft_pending(self):
        # Limite connue : l'étiquette ne finit plus seule sa ligne. Le brouillon reste en attente (pause
        # « brouillon en attente »), jamais un faux envoi.
        claude, _ = self.opening()
        with self.db() as db:
            state.draft(db, state.Relay(claude, "claude", "codex", "C0"))
        self.submit("codex", f"Mon avis : [Réponse de Claude #{claude}]\n\nC0")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (claude,)).fetchone(),
                             (state.DRAFTED,))

    def test_codex_turn_citing_the_awaited_label_in_a_sentence_is_not_its_answer(self):
        claude = self.reply("claude", "C1", self.exchange())
        with self.db() as db:
            state.draft(db, state.Relay(claude, "claude", "codex", "C1"))
        self.submit("codex", f"[Réponse de Claude #{claude}]\n\nC1", request="")  # accusé sans numéro de tour
        question = f"Que signifie le numéro dans [Réponse de Claude #{claude}] ?"
        codex = self.reply("codex", "R1", "", prompt=question)  # autre tour, sans numéro non plus
        with self.db() as db:
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (codex,)).fetchone(), (None,))
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "awaiting_reply")), ("codex", str(claude)))

    def test_label_quoted_inside_the_relayed_reply_is_not_a_header(self):
        for with_hook in (True, False):
            with self.subTest(with_hook=with_hook):
                self.setUp()
                old, _ = self.opening()
                with self.db() as db:
                    state.draft(db, state.Relay(old, "claude", "codex", "C0"))  # jamais envoyé
                body = f"Exemple :\n```text\n[Réponse de Claude #{old}]\n```\nJe demande une modification."
                current = self.reply("claude", body, self.fresh())
                with self.db() as db:  # copie automatique en file
                    state.mark(db, current, state.QUEUED, "q1")
                    db.execute("UPDATE replies SET claim_id = 1 WHERE id = ?", (current,))
                wire = f"[Réponse de Claude #{current}]\n\n{body}"
                request = self.submit("codex", wire) if with_hook else self.fresh()
                codex = self.reply("codex", "Cela me convient.", request, prompt=wire)
                with self.db() as db:
                    self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (old,)).fetchone(),
                                     (state.DRAFTED,))
                    self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (codex,)).fetchone(),
                                     (current,))

    def test_turn_typed_on_another_thread_moves_the_active_one_at_once(self):
        self.opening()  # A (fil-codex) actif
        self.reply("codex", "RB", self.submit("codex", "Ailleurs", session="fil-B"), session="fil-B",
                   prompt="Ailleurs")  # B devient actif
        request = self.submit("codex", "Ma demande")  # l'utilisateur revient sur A
        with self.db() as db:
            self.assertEqual((state.get(db, "codex_thread"), state.get(db, "codex_state")), ("fil-codex", "busy"))
            state.record_permission(db, self.event("codex", "permission", "", request, None))
            self.assertEqual(state.get(db, "codex_state"), "permission")

    def test_late_end_of_an_earlier_turn_keeps_the_permission_of_the_next_one(self):
        self.opening()
        earlier = self.submit("codex", "Ancienne")
        current = self.submit("codex", "Ma demande")
        with self.db() as db:
            state.record_permission(db, self.event("codex", "permission", "", current, None))
        self.reply("codex", "Fin ancienne", earlier, prompt="Ancienne")
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_state"), "permission")
        self.reply("codex", "Fin", current, prompt="Ma demande")
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_state"), "idle")

    def test_first_thread_is_followed_even_when_its_first_end_is_late(self):
        earlier = self.submit("codex", "Première")
        self.submit("codex", "Seconde")
        self.reply("codex", "Fin de la première", earlier, prompt="Première")
        with self.db() as db:
            self.assertEqual((state.get(db, "codex_thread"), state.get(db, "codex_state")), ("fil-codex", "busy"))

    def test_label_with_an_oversized_number_is_ignored(self):
        claude, _ = self.opening()
        with self.db() as db:
            state.draft(db, state.Relay(claude, "claude", "codex", "C0"))
        self.submit("codex", "[Réponse de Claude #9999999999999999999]\n\nC0")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (claude,)).fetchone(),
                             (state.DRAFTED,))

    def test_relay_to_claude_takes_the_reply_of_the_active_thread(self):
        self.opening()
        late = self.submit("codex", "Demande ancienne")  # tour de A, encore en cours
        reply_b = self.reply("codex", "Avis B", self.submit("codex", "Demande B", session="fil-B"), session="fil-B",
                             prompt="Demande B")
        self.submit("codex", "Suite B", session="fil-B")
        self.reply("codex", "Avis tardif de A", late, prompt="Demande ancienne")
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_thread"), "fil-B")
            self.assertEqual(state.next_relay(db).reply_id, reply_b)
            self.assertEqual(state.latest_reply(db, "codex")[0], reply_b)

    def test_wake_up_text_from_claude_code_confirms_the_delivery(self):
        _, codex = self.opening()
        with self.db() as db:
            state.mark(db, codex, state.QUEUED)
        self.submit("claude", '<system-reminder>\nStop hook blocking error from command "Stop": '
                    f"[Réponse de Codex #{codex}]\n\nR0\n</system-reminder>")
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "claude")

    def test_pasted_draft_wrapped_by_claude_code_counts_as_sent(self):
        _, codex = self.opening()
        with self.db() as db:
            state.mark(db, codex, state.DRAFTED)
        self.submit("claude", f'\n\n<pasted_content id="a6d6">\n[Réponse de Codex #{codex}]\n\nR0\n\n</pasted_content>\n')
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "claude")

    def test_older_reply_is_never_relayed(self):
        self.opening()
        latest = self.turn("codex", "Précise", "R1")
        with self.db() as db:
            self.assertEqual(state.next_relay(db).reply_id, latest)
            state.mark(db, latest, state.SENT)
            self.assertIsNone(state.next_relay(db))

    def test_reply_received_while_relay_is_queued_does_not_answer_it(self):
        claude = self.reply("claude", "C1", self.exchange())
        with self.db() as db:
            state.mark(db, claude, state.QUEUED, "q1")
            state.put(db, awaiting="codex", awaiting_reply=claude)
        self.turn("codex", "Une autre demande", "Réponse au tour en cours")
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "codex")
        codex = self.reply("codex", "R1", self.fresh(), prompt=f"[Réponse de Claude #{claude}]\n\nC1")
        with self.db() as db:
            self.assertEqual(state.next_relay(db), state.Relay(codex, "codex", "claude", "R1"))
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (codex,)).fetchone(), (claude,))

    def test_claude_reply_is_tied_to_the_request_it_answers(self):
        _, codex = self.opening()
        older_request = self.submit("claude", "Une demande plus ancienne")
        request = self.deliver(codex, "claude")
        self.reply("claude", "Fin de l'ancien tour", older_request)
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "claude")
            self.assertEqual(state.get(db, "unidentified"), "0")
        claude = self.reply("claude", "Réponse au relais", request)
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "")
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (claude,)).fetchone(), (codex,))

    def test_unidentifiable_reply_is_kept_and_flagged_until_a_manual_relay(self):
        self.turn("claude", "Question", "C0")
        codex = self.turn("codex", "Question", "R0\n[ACCORD]")
        with self.db() as db:
            state.mark(db, codex, state.DRAFTED)
        self.submit("claude", f"[Réponse de Codex #{codex}]\n\nR0\n[ACCORD]", request="")
        claude = self.reply("claude", "Fin d'un tour antérieur\n[ACCORD]", self.fresh())
        with self.db() as db:
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "unidentified")), ("claude", str(codex)))
            self.assertIsNone(state.next_relay(db))
            self.assertFalse(state.agreed(db))
        self.deliver(claude, "codex")
        with self.db() as db:
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "awaiting_reply"),
                              state.get(db, "unidentified")), ("codex", str(claude), "0"))

    def test_new_codex_thread_is_followed(self):
        self.reply("codex", "R1", self.fresh(), session="fil-a")
        self.reply("codex", "R2", self.fresh(), session="fil-b")
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_thread"), "fil-b")

    def test_codex_replay_and_new_thread_cannot_acknowledge_an_old_draft(self):
        claude, _ = self.opening()
        original = self.submit("codex", "Autre question")
        self.reply("codex", "Réponse initiale", original, prompt="Autre question")
        with self.db() as db:
            state.mark(db, claude, state.DRAFTED)
        text = f"[Réponse de Claude #{claude}]\n\nC0"
        self.assertIsNone(self.reply("codex", "Rejeu", original, prompt=text))
        self.reply("codex", "Autre fil", self.fresh(), session="fil-neuf")
        self.reply("codex", "Même étiquette", self.fresh(), session="fil-neuf", prompt=text)
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "")
            self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (claude,)).fetchone(), (state.DRAFTED,))

    def test_two_initial_accords_are_not_an_agreement(self):
        codex = self.turn("codex", "Question", "R1\n[ACCORD]")
        self.turn("claude", "Question", "C1\n[ACCORD]")
        request = self.deliver(codex, "claude")
        with self.db() as db:
            self.assertFalse(state.agreed(db))
        self.reply("claude", "C2\n  [ACCORD]  \n", request)
        with self.db() as db:
            self.assertTrue(state.agreed(db))
        self.reply("claude", "Autre fin du même tour\n[ACCORD]", request)
        with self.db() as db:
            self.assertFalse(state.agreed(db))

    def test_accord_must_be_the_exact_last_line(self):
        claude = self.reply("claude", "C1\n[ACCORD]", self.exchange())
        self.reply("codex", "Je cite [ACCORD] mais je continue.", self.deliver(claude, "codex"))
        with self.db() as db:
            self.assertFalse(state.agreed(db))


class ContextTest(Base):
    def text(self, reply_id):
        with self.db() as db:
            relay = state.next_relay(db)
            self.assertEqual(relay.reply_id, reply_id)
            return state.compose(db, relay)

    def paste(self):
        with self.db() as db:
            return state.draft(db, state.next_relay(db))

    def settled(self):
        self.reply("claude", "Bien reçu.", self.exchange())

    def test_reply_alone_when_nothing_is_new(self):
        self.assertEqual(state.common(1, "Question"), "[Demande commune #1]\n\nQuestion")
        _, codex = self.opening()
        self.assertEqual(self.text(codex), f"[Réponse de Codex #{codex}]\n\nR0")

    def test_request_typed_to_one_assistant_travels_with_its_answer(self):
        self.settled()
        claude = self.turn("claude", "Lis le plan et termine les mesures.", "Mesures faites.")
        self.assertEqual(self.text(claude),
                         f"[Réponse de Claude #{claude}]\n\nAvant cette réponse, depuis le dernier relais :\n\n"
                         "— Réponse de Claude :\nC0\n\n"
                         "— Réponse de Claude :\nBien reçu.\n\n"
                         "— Demande de l'utilisateur à Claude :\nLis le plan et termine les mesures.\n\n"
                         "— Réponse la plus récente de Claude :\nMesures faites.")

    def test_addition_to_a_draft_travels_once_without_the_quote(self):
        self.opening()
        pasted = self.paste()
        wrapped = f'Mon avis d’abord.\n<pasted_content id="a6d6">\n{pasted}\n</pasted_content id="a6d6">\nPropose une charte.'
        claude = self.turn("claude", wrapped, "C1")
        text = self.text(claude)
        self.assertIn("— Demande de l'utilisateur à Claude :\nMon avis d’abord.\nPropose une charte.", text)
        self.assertNotIn("pasted_content", text)
        self.assertNotIn("R0", text)
        codex = self.turn("codex", self.paste(), "R1")
        following = self.reply("claude", "C2", self.deliver(codex, "claude"))
        self.assertNotIn("Propose une charte", self.text(following))

    def test_addition_on_the_codex_side_is_known_from_the_notification(self):
        self.settled()
        pasted = self.paste()
        codex = self.reply("codex", "R1", self.fresh(), prompt=pasted + "\n\nEt vérifie les tests.")
        text = self.text(codex)
        self.assertIn("— Demande de l'utilisateur à Codex :\nEt vérifie les tests.", text)
        self.assertNotIn("Bien reçu.", text)

    def test_same_words_sent_separately_are_two_real_requests(self):
        self.settled()
        self.submit("codex", "Continue")
        claude = self.turn("claude", "Continue", "C1")
        self.assertIn("— Demande de l'utilisateur à Claude :\nContinue", self.text(claude))

    def test_shared_request_is_left_out_exactly_once(self):
        self.settled()
        claude = self.turn("claude", "Question", "C1")
        self.assertEqual(self.text(claude).count("Question"), 1)

    def test_an_abandoned_question_travels_as_context_if_only_codex_received_it(self):
        self.settled()
        question = "Quel risque voyez-vous chacun ?"
        with self.db() as db:
            number = state.share(db, dict.fromkeys(state.NAMES, question))
        reply = self.turn("codex", state.common(number, question), "Risque B")
        with self.db() as db:
            state.close_round(db, number)
        message = self.text(reply)
        self.assertIn("— Demande de l'utilisateur à Codex :\n" + question, message)
        self.assertNotIn("[Demande commune", message)

    def test_only_kept_replies_of_the_active_thread_are_quoted(self):
        self.turn("claude", "Question", "C1")
        with patch("samdario.state.is_codex_user_thread", return_value=False):
            self.reply("codex", '{"title":"Titre interne"}', self.fresh(), session="fil-titre")
        self.reply("codex", "Ancien fil", self.fresh(), session="fil-ancien")
        request = self.submit("codex", "Question")
        self.reply("codex", "Premier avis", request, prompt="Question")
        self.reply("codex", "Doublon à ignorer", request, prompt="Question")
        latest = self.turn("codex", "Précise", "Deuxième avis")
        text = self.text(latest)
        self.assertIn("— Réponse de Codex :\nPremier avis", text)
        for excluded in ("Titre interne", "Ancien fil", "Doublon"):
            self.assertNotIn(excluded, text)

    def test_read_marker_moves_only_on_confirmation_and_per_thread(self):
        self.settled()
        first = self.turn("claude", "Demande A", "C1")
        pasted = self.paste()
        with self.db() as db:
            state.mark(db, first, state.SENT)
        codex = self.turn("codex", "Autre sujet", "R1")
        second = self.reply("claude", "C2", self.deliver(codex, "claude"))
        self.assertIn("Demande A", self.text(second))
        self.submit("codex", pasted)
        self.assertNotIn("Demande A", self.text(second))
        self.reply("codex", "Nouveau fil", self.fresh(), session="fil-neuf")
        with self.db() as db:
            self.assertIn("Demande A", state.compose(db, state.Relay(second, "claude", "codex", "C2")))

    def test_full_size_reply_arrives_whole_and_the_omission_is_visible(self):
        self.settled()
        body = "é" * state.MAX_BODY
        claude = self.turn("claude", "Question à omettre", body)
        text = self.text(claude)
        self.assertTrue(text.startswith(f"[Réponse de Claude #{claude}]\n"))
        self.assertTrue(text.endswith(body))
        self.assertIn("(contexte plus ancien omis)", text)
        self.assertNotIn("Question à omettre", text)

    def test_quota_counts_only_bodies_and_keeps_the_newest_context(self):
        self.settled()
        claude = self.turn("claude", "Récent", "東京")
        with patch.object(state, "MAX_BODY", len("Récent東京")):
            text = self.text(claude)
        self.assertIn("(contexte plus ancien omis)", text)
        self.assertNotIn("Bien reçu.", text)
        self.assertIn("— Demande de l'utilisateur à Claude :\nRécent", text)
        self.assertTrue(text.endswith("東京"))

    def test_hook_and_notification_count_each_codex_demand_once(self):
        self.turn("claude", "Question", "C0")
        first = self.submit("codex", "Continue")
        second = self.submit("codex", "Continue")
        self.reply("codex", "R1", first, prompt="Continue")
        latest = self.reply("codex", "R2", second, prompt="Continue")
        self.assertEqual(self.text(latest).count("— Demande de l'utilisateur à Codex :\nContinue"), 2)

    def test_sending_two_drafts_records_only_the_user_addition(self):
        self.opening()
        older = self.paste()
        self.turn("codex", "Et le coût ?", "Trois euros.")
        newer = self.paste()
        claude = self.turn("claude", newer + "\n\n" + older + "\n\nCalcule la TVA.", "TVA calculée.")
        text = self.text(claude)
        self.assertEqual(text.count("Calcule la TVA."), 1)
        for quoted in ("R0", "Trois euros.", "Et le coût ?"):
            self.assertNotIn(quoted, text)

    def test_inline_addition_and_markdown_paragraphs_survive_paste(self):
        self.opening()
        pasted = self.paste()
        comment = "Vérifie.\n\nExplique le délai."
        claude = self.turn("claude", pasted + " " + comment, "C1")
        text = self.text(claude)
        self.assertIn("— Demande de l'utilisateur à Claude :\nVérifie.\n\nExplique le délai.", text)
        self.assertNotIn("R0", text)

    def test_replayed_submission_does_not_turn_the_common_launch_into_a_private_request(self):
        with self.db() as db:
            number = state.share(db, {"claude": "Question", "codex": "Question"})
        prompt = state.common(number, "Question")
        request = self.submit("claude", prompt)
        self.submit("claude", prompt, request=request)
        self.reply("claude", "C0", request)
        codex = self.turn("codex", prompt, "R0")
        claude = self.reply("claude", "C1", self.deliver(codex, "claude"))
        self.assertNotIn("Question", self.text(claude))

    def test_late_older_draft_never_moves_the_read_marker_back(self):
        self.opening()
        older = self.paste()
        second = self.turn("codex", "Question B", "Réponse B")
        self.submit("claude", self.paste())
        self.submit("claude", older)
        with self.db() as db:
            last_seen, = db.execute("SELECT upto FROM seen WHERE session = 'fil-claude'").fetchone()
            self.assertEqual(db.execute("SELECT event_id FROM replies WHERE id=?", (second,)).fetchone(), (last_seen,))

    def test_control_characters_are_counted_as_displayed_in_a_manual_draft(self):
        self.settled()
        self.turn("claude", "\x1b", "A")
        with patch.object(state, "MAX_BODY", 4):
            text = self.paste()
        self.assertIn("(contexte plus ancien omis)", text)
        self.assertNotIn(r"\x1b", text)
        self.assertTrue(text.endswith("— Réponse la plus récente de Claude :\nA"))

    def test_each_assistant_has_its_own_read_marker_even_with_the_same_session_name(self):
        session = "même-identifiant"
        request = self.submit("codex", "Un risque ?", session=session)
        self.reply("codex", "Premier risque", request, session=session, prompt="Un risque ?")
        claude = self.reply("claude", "Avis de Claude", self.fresh(), session=session)
        with self.db() as db:
            sent = state.draft(db, state.Relay(claude, "claude", "codex", "Avis de Claude"))
        request = self.submit("codex", sent, session=session)
        codex = self.reply("codex", "Autre risque", request, session=session, prompt=sent)
        text = self.text(codex)
        self.assertIn("— Demande de l'utilisateur à Codex :\nUn risque ?", text)
        self.assertIn("— Réponse de Codex :\nPremier risque", text)

    def test_another_thread_cannot_consume_the_common_request(self):
        self.opening()
        with self.db() as db:
            number = state.share(db, {"claude": "Question B", "codex": "Question B"})
        prompt = state.common(number, "Question B")
        self.submit("codex", prompt, session="ancien-fil")
        self.turn("codex", prompt, "Réponse B")
        with self.db() as db:
            row = db.execute("""SELECT e.session FROM shared AS s JOIN events AS e ON e.id = s.event_id
                                WHERE s.agent = 'codex' AND s.round = ?""", (number,)).fetchone()
        self.assertEqual(row, ("fil-codex",))


if __name__ == "__main__":
    unittest.main()
