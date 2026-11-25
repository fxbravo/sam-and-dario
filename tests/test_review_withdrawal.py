"""Quatre historiques de sortie de la tâche 5. Lancer avec python3 -m unittest tests.test_review_withdrawal."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import rules as rules
from samdario import state as state
from tests import test_auto
from tests.test_auto import THREAD, done, stop, taken, typed


def panes(folder):
    (folder / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))


def format_six(folder, claim_thread, reply_thread, action, wire):
    # Construire une base vierge en arrêtant la chaîne historique au format 6.
    # La première ouverture par OFF ou Ctrl-b R exercera toutes les migrations ultérieures.
    with closing(sqlite3.connect(folder / "replies.db")) as db, db:
        state._create_schema(db)
        for column in ("target_session", "confirm_request", "confirm_session"):
            db.execute(f"ALTER TABLE replies ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        state.put(db, unidentified=0)
        db.execute("PRAGMA user_version = 2")
        state._migrate_context(db)
        state._migrate_claims(db)
        state._migrate_deliveries(db)
        state._migrate_withdrawals(db)
        assert db.execute("PRAGMA user_version").fetchone() == (6,)
        assert "other_thread" not in {r[1] for r in db.execute("PRAGMA table_info(claims)")}
        event = state.journal(db, state.Event("claude", "reply", "s", None, "Avis", "", "k4"))
        status = state.QUEUED if action == "OFF" else state.UNCERTAIN
        db.execute("""INSERT INTO replies(id,sender,session,body,status,queue_id,target_session,event_id,pasted,claim_id,delivery)
                      VALUES (4,'claude','s','Avis',?,'q1',?,?,?,1,1)""", (status, reply_thread, event, wire))
        db.execute("""INSERT INTO claims(id,reply_id,recipient,run,claimed_at,prior_status,state,thread,queue_id)
                      VALUES (1,4,'codex',1,1,'new',?,?,'q1')""",
                   ("pending" if action == "OFF" else "uncertain", claim_thread))
        state.put(db, auto="on" if action == "OFF" else "pause", run=1, count=1, codex_thread="B",
                  claude_session="s", awaiting="codex", awaiting_reply=4, awaited_seq=1, delivery_seq=1)
    panes(folder)


class CritereTache5(unittest.TestCase):
    def session(self):
        session = test_auto.LoopTest()
        session.setUp()
        self.addCleanup(session.doCleanups)
        session.started()
        with session.db() as db:
            rules.toggle(db, 0.1, True)  # préparation en mode manuel
        panes(session.state)
        return session

    def test_retrait_du_bon_message(self):
        wire = "[Réponse de Claude #4]\n\nAvis"
        for claim, reply in (("B", "A"), ("A", "B")):
            for action in ("OFF", "R"):
                with self.subTest(reservation=claim, reponse=reply, geste=action), tempfile.TemporaryDirectory() as d:
                    folder = Path(d)
                    format_six(folder, claim, reply, action, wire)
                    queues = {"A": {"q1": wire}, "B": {"q1": "Message personnel"}}

                    def rpc(method, params, **kwargs):
                        queue = queues[params["threadId"]]
                        if method == "thread/queue/list":
                            return {"data": [{"id": key, "clientUserMessageId": key,
                                              "input": [{"type": "text", "text": text}]} for key, text in queue.items()],
                                    "nextCursor": None}
                        self.assertEqual(method, "thread/queue/delete")
                        return {"deleted": queue.pop(params["queuedSubmissionId"], None) is not None}

                    with patch.object(auto, "app_server", side_effect=rpc), patch.object(samdario, "tmux", return_value="0/0"):
                        if action == "OFF":
                            auto.toggle(folder, now=2)
                        else:
                            samdario.relay(folder, "%1", force=True)
                    self.assertEqual(queues, {"A": {}, "B": {"q1": "Message personnel"}})

    def test_exemple_dans_le_relais(self):
        t = self.session()
        auto.handle_event(t.state, "claude", stop("Première proposition.\n[ACCORD]", "k1"), 1)
        with patch.object(samdario, "tmux", return_value="0/0"):
            samdario.relay(t.state, "%1")
        with t.db() as db:
            old, = db.execute("SELECT id FROM replies WHERE status = 'drafted'").fetchone()
        auto.handle_event(t.state, "claude", typed("Propose une modification.", "k2"), 2)
        body = f"Exemple de message\n```text\n[Réponse de Claude #{old}]\n```\nJe demande une modification."
        auto.handle_event(t.state, "claude", stop(body, "k2"), 3)
        with t.db() as db:
            current, = db.execute("SELECT id FROM replies WHERE body = ?", (body,)).fetchone()
        with patch.object(auto, "queue_send", return_value="q1") as send:
            auto.toggle(t.state, now=4)
            wire = send.call_args.args[1]
            self.assertTrue(wire.startswith(state.label("claude", current)))
            auto.handle_event(t.state, "codex", taken(wire, "tc"), 5)
            auto.handle_event(t.state, "codex", done("Cela me convient.\n[ACCORD]", "tc", wire), 6)
        with t.db() as db:
            parent, = db.execute("SELECT answers FROM replies WHERE turn = 'tc'").fetchone()
            drafted, = db.execute("SELECT status FROM replies WHERE id = ?", (old,)).fetchone()
            self.assertEqual((parent, drafted, state.agreed(db)), (current, state.DRAFTED, False))

    def test_contenu_relaye_depuis_le_fil_actif(self):
        for action in ("manuel", "auto"):
            with self.subTest(action=action):
                t = self.session()
                auto.handle_event(t.state, "claude", typed("À suivre", "k8"), 0.2)
                waiter = auto.handle_event(t.state, "claude", stop("Je suis prêt.", "k8"), 0.3)
                auto.handle_event(t.state, "codex", taken("Demande ancienne", "a1"), 1)
                auto.handle_event(t.state, "codex", dict(taken("Demande B", "b1"), session_id="B"), 2)
                auto.handle_event(t.state, "codex", dict(done("Avis du fil B", "b1", "Demande B"), **{"thread-id": "B"}), 3)
                auto.handle_event(t.state, "codex", dict(taken("Suite B", "b2"), session_id="B"), 4)
                auto.handle_event(t.state, "codex", {"hook_event_name": "PermissionRequest", "session_id": "B", "turn_id": "b2"}, 5)
                auto.handle_event(t.state, "codex", done("Avis tardif du fil A", "a1", "Demande ancienne"), 6)
                with t.db() as db:
                    self.assertEqual((state.get(db, "codex_thread"), state.get(db, "codex_state")), ("B", "permission"))
                if action == "manuel":
                    with patch.object(samdario, "tmux", return_value="0/0") as mux:
                        samdario.relay(t.state, "%0")
                    wire = next(c.kwargs["data"].decode() for c in mux.call_args_list if c.args[1] == "load-buffer")
                else:
                    auto.toggle(t.state, now=7)
                    ticks = iter((7.2, 4000))
                    wire = auto.wait(t.state, waiter, clock=lambda: next(ticks), sleep=lambda _: None)
                self.assertIn("Avis du fil B", wire or "")
                self.assertNotIn("Avis tardif du fil A", wire)

    def test_autorisation_du_tour_en_cours(self):
        for case in ("retour sur A", "ancienne fin dans B"):
            with self.subTest(case=case):
                t = self.session()
                auto.handle_event(t.state, "codex", dict(taken("Demande B", "b0"), session_id="B"), 1)
                auto.handle_event(t.state, "codex", dict(done("Avis B", "b0", "Demande B"), **{"thread-id": "B"}), 2)
                if case == "retour sur A":
                    thread = THREAD
                else:
                    thread = "B"
                    auto.handle_event(t.state, "codex", dict(taken("Ancienne", "b1"), session_id="B"), 3)
                auto.handle_event(t.state, "codex", dict(taken("Ma demande", "courant"), session_id=thread), 4)
                auto.handle_event(t.state, "codex", {"hook_event_name": "PermissionRequest", "session_id": thread, "turn_id": "courant"}, 5)
                if case == "ancienne fin dans B":
                    auto.handle_event(t.state, "codex", dict(done("Fin ancienne", "b1", "Ancienne"), **{"thread-id": "B"}), 6)
                auto.handle_event(t.state, "claude", stop("Proposition de Claude", "k9"), 7)
                with patch.object(auto, "queue_send", return_value="q1") as send:
                    bar = auto.toggle(t.state, now=8)
                with t.db() as db:
                    active = state.get(db, "codex_thread")
                self.assertEqual((active, "autorisation demandée" in bar), (thread, True))
                for call in send.call_args_list:
                    self.assertEqual(call.args[0], thread)


if __name__ == "__main__":
    unittest.main(verbosity=2)
