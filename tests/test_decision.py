"""Tâche 10 : une décision requise met l'auto en pause ; la réponse de l'utilisateur relance l'échange."""

import unittest
from unittest.mock import patch

from samdario import auto as auto
from samdario import state as state
from tests.test_auto import LoopBase, clock, done, stop, taken, typed

DECISION = "PAUSE — décision requise : à toi de trancher"
ASKING = "Deux options : UTF-8, ou UTF-8 avec BOM. Je recommande la seconde pour Excel.\n[DÉCISION]"


class DecisionTest(LoopBase):
    def get(self, key):
        with self.db() as db:
            return state.get(db, key)

    def test_decision_from_claude_pauses_without_relaying_it(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            auto.handle_event(self.state, "claude", stop(ASKING, "k1"), now=1.0)
            self.assertEqual(auto.status(self.state, now=2.0), DECISION)
            send.assert_not_called()

    def test_answer_typed_after_a_decision_restarts_the_exchange(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            auto.handle_event(self.state, "claude", stop(ASKING, "k1"), now=1.0)
            auto.handle_event(self.state, "claude", typed("Prends UTF-8 avec BOM.", "k2"), now=3.0)
            auto.handle_event(self.state, "claude", stop("J'applique le BOM.", "k2"), now=4.0)
            self.assertEqual(self.get("auto"), "on")
            relayed = send.call_args.args[1]
        self.assertIn("Prends UTF-8 avec BOM.", relayed)
        self.assertIn("J'applique le BOM.", relayed)

    def test_decision_from_codex_pauses_before_waking_claude(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            waiter = auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
            relayed = send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(relayed, "t1"), now=2.0)
        auto.handle_event(self.state, "codex", done(ASKING, "t1", relayed), now=3.0)
        self.assertEqual(auto.status(self.state, now=4.0), DECISION)
        self.assertIsNone(auto.wait(self.state, waiter, clock=clock(), sleep=lambda seconds: None))

    def test_decision_word_inside_a_reply_is_not_a_decision(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            auto.handle_event(self.state, "claude", stop("Le mot [DÉCISION] n'est cité ici.\nSuite.", "k1"), now=1.0)
            send.assert_called_once()
        self.assertTrue(auto.status(self.state, now=2.0).startswith("AUTO"))

    def test_ctrl_b_a_after_a_decision_goes_on_without_asking_again(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            auto.handle_event(self.state, "claude", stop(ASKING, "k1"), now=1.0)
            self.assertEqual(auto.status(self.state, now=2.0), DECISION)
            self.assertEqual(auto.toggle(self.state, now=3.0), "AUTO 1/8")  # l'utilisateur laisse le binôme poursuivre
            self.assertIn("[DÉCISION]", send.call_args.args[1])

    def test_decision_in_the_first_round_waits_for_a_crossed_reading_before_the_pause(self):
        # Tâche 13 : la pause attend que les deux se soient lus (test_crossread.py couvre la suite de la lecture).
        for author in ("claude", "codex"):
            with self.subTest(author=author):
                self.setUp()
                with self.db() as db:
                    state.put(db, autostart=1)
                    common = state.common(state.share(db, {"claude": "Q", "codex": "Q"}), "Q")
                auto.handle_event(self.state, "claude", typed(common, "c1"), now=0.1)
                auto.handle_event(self.state, "codex", taken(common, "t1"), now=0.2)
                with patch("samdario.auto.queue_send", return_value="q1"), patch("samdario.auto.paste_draft"):
                    for when, agent in enumerate((author, state.other(author)), start=1):
                        text = ASKING if agent == author else "Le format CSV convient."
                        if agent == "claude":
                            auto.handle_event(self.state, "claude", stop(text, "c1"), now=when)
                        else:
                            auto.handle_event(self.state, "codex", done(text, "t1", common), now=when)
                    line = auto.status(self.state, now=3.0)
                    self.assertTrue(line.startswith("AUTO"), line)
                    self.assertIn("lecture croisée avant ta décision", line)


if __name__ == "__main__":
    unittest.main()
