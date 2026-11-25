"""Tâche 17 : un relais remplacé par une question de l'utilisateur (plan : docs/superpowers/plans/2026-11-23-tache-17-relais-remplace.md)."""

import json
import unittest
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import bar as bar
from samdario import crossread as crossread
from samdario import messages as messages
from samdario import rules as rules
from samdario import state as state
from tests.test_auto import LoopBase, clock, done, stop, taken, typed

LATE = 5000.0  # l'attente de Claude a expiré : l'auto colle le relais dans son champ
ASK = "Deux formats possibles :\n- PDF\n- CSV\nQue choisis-tu ?\n[DÉCISION]"


class Replace(LoopBase):
    def setUp(self):
        super().setUp()
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        self.send = self.patch("samdario.auto.queue_send", return_value="q1")
        self.paste = self.patch("samdario.auto.paste_draft")

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def meta(self, key):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def row_of(self, reply_id):
        with self.db() as db:
            return db.execute("SELECT status, claim_id FROM replies WHERE id = ?", (reply_id,)).fetchone()

    def pasted_at_claude(self, claude="C1", codex="R1"):
        """Ouverture, puis l'auto colle la réponse de Codex (n° 2) dans le champ de Claude. Renvoie son numéro."""
        with self.db() as db:
            sent = state.common(state.share(db, {"claude": "Question", "codex": "Question"}), "Question")
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", stop(claude, "k1"), now=1.0)
        auto.handle_event(self.state, "codex", done(codex, "t1", sent), now=2.0)
        auto.toggle(self.state, now=LATE)  # reprise tardive : collage chez Claude
        self.assertIn("Entrée chez Claude", auto.status(self.state, now=LATE + 1))
        return 2

    def renard(self, request="k9", now=LATE + 2):
        auto.handle_event(self.state, "claude", typed("Et pour un renard ?", request), now=now)


class ReplaceTest(Replace):
    def test_typed_question_over_a_pasted_relay_replaces_it(self):
        relay = self.pasted_at_claude()
        self.renard()
        self.assertEqual(self.row_of(relay), (state.REPLACED, None))
        self.assertEqual(self.meta("awaiting"), "")
        waiter = auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        sent = self.send.call_args.args[1]
        self.assertTrue(sent.startswith("[Réponse de Claude #3]"), sent)  # numéro et texte réellement envoyés
        self.assertIn("Roussin.", sent)
        self.assertIn("Et pour un renard ?", sent)
        auto.handle_event(self.state, "codex", taken(sent, "t9"), now=LATE + 4)
        auto.handle_event(self.state, "codex", done("Goupil.", "t9", sent), now=LATE + 5)
        woke = auto.wait(self.state, waiter, clock=clock(LATE + 6, 1.0), sleep=lambda seconds: None)
        self.assertIsNotNone(woke)
        self.assertIn("R1", woke)  # le texte remplacé arrive en contexte au relais suivant vers Claude
        self.assertIn("Goupil.", woke)

    def test_first_relay_replaced_lets_the_answer_go(self):
        self.pasted_at_claude()
        self.renard()
        auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        self.send.assert_called()
        self.assertIn("Roussin.", self.send.call_args.args[1])

    def test_max_one_gives_back_the_credit(self):
        with self.db() as db:
            state.put(db, max=1)
        self.pasted_at_claude()
        self.assertEqual(self.meta("count"), "1")
        self.renard()
        self.assertEqual(self.meta("count"), "0")  # la copie écartée n'est plus comptée
        auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        self.assertIn("Roussin.", self.send.call_args.args[1])
        self.assertNotIn(rules.REASONS["limite"], auto.status(self.state, now=LATE + 4))

    def test_replaced_relay_lifts_the_draft_pause(self):
        self.pasted_at_claude()
        auto.toggle(self.state, now=LATE + 1)  # OFF : le relais collé devient un brouillon
        auto.toggle(self.state, now=LATE + 1.5)  # reprise : pause « brouillon » (cas réel du 23 novembre)
        self.assertIn("brouillon", auto.status(self.state, now=LATE + 1.6))
        self.renard()
        auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        self.assertTrue(auto.status(self.state, now=LATE + 4).startswith("AUTO"))
        self.assertIn("Roussin.", self.send.call_args.args[1])

    def test_message_containing_the_relay_is_not_a_replacement(self):
        relay = self.pasted_at_claude()
        with self.db() as db:
            pasted, = db.execute("SELECT pasted FROM replies WHERE id = ?", (relay,)).fetchone()
        message = "Mon avis : " + messages.paste_text(pasted) + "\nQu'en penses-tu ?"
        auto.handle_event(self.state, "claude", typed(message, "k9"), now=LATE + 2)
        self.assertEqual(self.row_of(relay)[0], state.SENT)  # la remise est confirmée
        with self.db() as db:
            additions = db.execute("""SELECT text FROM events WHERE agent = 'claude' AND kind = 'submitted'
                                      AND text LIKE '%Qu''en penses-tu%'""").fetchall()
        self.assertEqual(len(additions), 1)
        self.assertNotIn("R1", additions[0][0])  # l'ajout seul, transmis une fois

    def test_typing_in_the_other_panel_keeps_the_draft(self):
        relay = self.pasted_at_claude()
        auto.handle_event(self.state, "codex", taken("Une question pour Codex", "t9"), now=LATE + 2)
        self.assertEqual(self.row_of(relay)[0], state.QUEUED)

    def test_duplicate_submission_does_not_replace(self):
        relay = self.pasted_at_claude()
        with self.db() as db:
            common, = db.execute("SELECT text FROM events WHERE agent = 'claude' AND kind = 'submitted' LIMIT 1").fetchone()
        auto.handle_event(self.state, "claude", typed(common, "k1"), now=LATE + 2)  # le hook d'ouverture rejoué
        self.assertEqual(self.row_of(relay)[0], state.QUEUED)

    def test_common_request_and_wake_are_not_replacements(self):
        relay = self.pasted_at_claude()
        with self.db() as db:
            common = state.common(state.share(db, {"claude": "Q2", "codex": "Q2"}), "Q2")
        auto.handle_event(self.state, "claude", typed(common, "k5"), now=LATE + 2)
        auto.handle_event(self.state, "claude", typed('Stop hook blocking error from command "Stop": rien', "k6"),
                          now=LATE + 3)
        self.assertEqual(self.row_of(relay)[0], state.QUEUED)

    def test_manual_draft_at_codex_is_replaced_too(self):
        self.launch()
        auto.handle_event(self.state, "claude", stop("Réponse de Claude.", "k2"), now=3.0)
        with patch.object(samdario, "tmux", return_value="0/0"), patch.object(samdario, "paste_to_pane"):
            samdario.relay(self.state, "%1")  # collage manuel chez Codex
        with self.db() as db:
            drafted, = db.execute("SELECT MAX(id) FROM replies WHERE sender = 'claude'").fetchone()
        self.assertEqual(self.row_of(drafted)[0], state.DRAFTED)
        auto.handle_event(self.state, "codex", taken("Autre chose", "t9"), now=4.0)
        self.assertEqual(self.row_of(drafted)[0], state.REPLACED)

    def test_uncertain_delivery_is_not_replaced(self):
        self.started()
        with patch.object(auto, "queue_send", side_effect=auto.UncertainSend("sortie illisible")):
            auto.handle_event(self.state, "claude", stop("Rapport prêt", "k1"), now=1)
        self.assertEqual(auto.status(self.state, now=2), "PAUSE — remise incertaine")
        with patch.object(samdario, "tmux", return_value="0/0"), patch.object(samdario, "paste_to_pane"), \
                patch.object(auto, "recall", return_value=None):
            samdario.relay(self.state, "%1", force=True)  # ↻ Recoller chez Codex
        with self.db() as db:
            latest, = db.execute("SELECT MAX(id) FROM replies WHERE sender = 'claude'").fetchone()
        auto.handle_event(self.state, "codex", taken("Autre chose", "t9"), now=3)
        self.assertNotEqual(self.row_of(latest)[0], state.REPLACED)
        self.assertEqual(auto.status(self.state, now=4), "PAUSE — remise incertaine")

    def test_shortcuts_do_not_repaste_a_replaced_relay(self):
        self.pasted_at_claude()
        self.renard()
        for force in (False, True):
            with self.subTest(force=force), patch.object(samdario, "tmux", return_value="0/0"), \
                    patch.object(samdario, "paste_to_pane") as paste:
                self.assertEqual(samdario.relay(self.state, "%0", force=force), "Aucune nouvelle réponse de Codex.")
                paste.assert_not_called()
        self.assertEqual(self.row_of(2)[0], state.REPLACED)

    def test_label_citation_of_a_replaced_relay_is_ignored(self):
        self.pasted_at_claude()
        self.renard()
        with self.db() as db:
            before = db.execute("SELECT upto FROM seen WHERE sender = 'codex'").fetchall()
        auto.handle_event(self.state, "claude", typed("Tu te souviens de\n[Réponse de Codex #2]", "k10"), now=LATE + 9)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT upto FROM seen WHERE sender = 'codex'").fetchall(), before)
        self.assertEqual(self.row_of(2)[0], state.REPLACED)

    def test_recoller_disappears_after_a_replacement(self):
        self.pasted_at_claude()
        with self.db() as db:
            self.assertEqual(bar.button_flags(db, "")["@samdario-recoller"], "1")
        self.renard()
        with self.db() as db:
            self.assertEqual(bar.button_flags(db, "")["@samdario-recoller"], "")

    def test_replacing_the_due_relay_of_a_reading_lets_the_new_question_go(self):
        with self.db() as db:
            sent = state.common(state.share(db, {"claude": "Question", "codex": "Question"}), "Question")
            state.put(db, autostart=1)
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", stop(ASK, "k1"), now=1.0)  # A soulève une décision
        auto.handle_event(self.state, "codex", done("R1", "t1", sent), now=LATE)  # démarrage : relais collé chez Claude
        self.assertIn("Entrée chez Claude", auto.status(self.state, now=LATE + 1))
        self.renard()  # B remplace le relais dû à A
        auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        self.assertIn("Roussin.", self.send.call_args.args[1])
        self.assertNotIn(crossread.READING, auto.status(self.state, now=LATE + 4))  # A n'attend plus
        with self.db() as db:
            self.assertEqual(state.get(db, "accord_seen"), "0")

    def test_replaced_text_reaches_the_next_relay_with_room(self):
        self.pasted_at_claude(codex="Texte remplacé sur la chouette.")
        self.renard()
        waiter = auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        sent = self.send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(sent, "t9"), now=LATE + 4)
        auto.handle_event(self.state, "codex", done("X" * 99_990, "t9", sent), now=LATE + 5)  # relais plein
        full = auto.wait(self.state, waiter, clock=clock(LATE + 6, 1.0), sleep=lambda seconds: None)
        self.assertNotIn("Texte remplacé sur la chouette.", full)
        request = "k11"
        auto.handle_event(self.state, "claude", typed('Stop hook blocking error from command "Stop": ' + full, request),
                          now=LATE + 7)
        waiter = auto.handle_event(self.state, "claude", stop("Lu.", request), now=LATE + 8)
        sent = self.send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(sent, "t10"), now=LATE + 9)
        auto.handle_event(self.state, "codex", done("Court.", "t10", sent), now=LATE + 10)
        short = auto.wait(self.state, waiter, clock=clock(LATE + 11, 1.0), sleep=lambda seconds: None)
        self.assertIn("Texte remplacé sur la chouette.", short)  # relais suivant avec de la place
        auto.handle_event(self.state, "claude", typed('Stop hook blocking error from command "Stop": ' + short, "k12"),
                          now=LATE + 12)
        waiter = auto.handle_event(self.state, "claude", stop("Encore lu.", "k12"), now=LATE + 13)
        sent = self.send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(sent, "t11"), now=LATE + 14)
        auto.handle_event(self.state, "codex", done("Fin.", "t11", sent), now=LATE + 15)
        last = auto.wait(self.state, waiter, clock=clock(LATE + 16, 1.0), sleep=lambda seconds: None)
        self.assertNotIn("Texte remplacé sur la chouette.", last)  # sans répétition

    def test_session_without_crossread_keeps_the_old_behaviour(self):
        relay = self.pasted_at_claude()
        with self.db() as db:
            db.execute("DELETE FROM meta WHERE key = 'crossread'")
        self.renard()
        self.assertEqual(self.row_of(relay)[0], state.QUEUED)


if __name__ == "__main__":
    unittest.main()
