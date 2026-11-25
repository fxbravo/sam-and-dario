"""Tâche 13 : la lecture croisée avant une décision (plan : docs/superpowers/plans/2026-11-21-tache-13-lecture-croisee.md).

À chaque question de l'utilisateur, une décision demandée avant que les deux assistants se soient lus ne met en
pause qu'après deux relais confirmés et la réponse au second.
"""

import json
import unittest
from unittest.mock import patch

from samdario import auto as auto
from samdario import crossread as crossread
from samdario import crossread_notes as notes
from samdario import rules as rules
from samdario import scene as scene
from samdario import state as state
from contextlib import closing
from tests.test_auto import LoopBase, clock, done, stop, taken, typed

WAKE = 'Stop hook blocking error from command "Stop": '
ASK = "Deux formats possibles :\n- PDF\n- CSV\nQue choisis-tu ?\n[DÉCISION]"
DECISION = "PAUSE — " + rules.REASONS["decision"]
READING = "lecture croisée avant ta décision"


class Crossread(LoopBase):
    """Parcours d'une lecture croisée avec les vrais traitements d'événements ; Codex et tmux sont simulés."""

    def setUp(self):
        super().setUp()
        self.send = self.patch("samdario.auto.queue_send", return_value="q1")
        self.paste = self.patch("samdario.auto.paste_draft")
        self.turns = iter(range(2, 100))

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def status(self):
        return auto.status(self.state, now=50.0)

    def meta(self, key, default=""):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def launch(self, claude_text, codex_text, first="claude"):
        """Ouverture avec démarrage prévu : chacun répond à la question commune. Renvoie l'attente de Claude."""
        with self.db() as db:
            state.put(db, autostart=1)
            self.common = state.common(state.share(db, dict.fromkeys(state.NAMES, "Q")), "Q")
        auto.handle_event(self.state, "claude", typed(self.common, "c1"), now=0.1)
        auto.handle_event(self.state, "codex", taken(self.common, "t1"), now=0.2)
        waiter = None
        for when, agent in enumerate((first, state.other(first)), start=1):
            if agent == "claude":
                waiter = auto.handle_event(self.state, "claude", stop(claude_text, "c1"), now=when)
            else:
                auto.handle_event(self.state, "codex", done(codex_text, "t1", self.common), now=when)
        return waiter

    def calm(self, on=False):
        """Un premier échange sans décision qui finit sur un accord. Renvoie l'attente de Claude. Avec `on`,
        l'auto reste actif sans relais en cours, comme après une reprise."""
        waiter = self.launch("Mon avis de Claude.", "Mon avis de Codex.\n[ACCORD]")
        _, waiter = self.claude_reads(waiter, "D'accord avec Codex.\n[ACCORD]")
        self.assertEqual(self.status(), "PAUSE — " + rules.REASONS["accord"])
        if on:
            with self.db() as db:
                state.put(db, auto=state.ON, reason="")
                db.execute("UPDATE replies SET status = 'sent' WHERE status = 'new'")  # rien ne reste à relayer
        return waiter

    def claude_reads(self, waiter, reaction):
        """Claude reçoit le relais par son réveil, puis répond. Renvoie (texte reçu, nouvelle attente)."""
        woke = auto.wait(self.state, waiter, clock=clock(), sleep=lambda seconds: None)
        self.assertIsNotNone(woke, "aucun relais remis à Claude")
        request = f"c{next(self.turns)}"
        auto.handle_event(self.state, "claude", typed(WAKE + woke, request), now=20.0)
        return woke, auto.handle_event(self.state, "claude", stop(reaction, request), now=21.0)

    def codex_reads(self, reaction):
        """Codex prend le dernier message mis dans sa file, puis répond. Renvoie le texte reçu."""
        relayed = self.send.call_args.args[1]
        turn = f"t{next(self.turns)}"
        auto.handle_event(self.state, "codex", taken(relayed, turn), now=30.0)
        auto.handle_event(self.state, "codex", done(reaction, turn, relayed), now=31.0)
        return relayed


class CommonQuestionTest(Crossread):
    def test_common_question_decision_in_first_answer_does_not_pause(self):
        for first in ("claude", "codex"):
            with self.subTest(first=first):
                self.setUp()
                waiter = self.launch(ASK, "Le CSV suffit.", first=first)
                self.assertTrue(self.status().startswith("AUTO"), self.status())
                self.assertIn(READING, self.status())
                woke, _ = self.claude_reads(waiter, "Milo a raison pour le CSV.")
                self.assertIn("Le CSV suffit.", woke)  # premier relais : la réponse de Codex
                self.assertTrue(self.status().startswith("AUTO"))
                relayed = self.codex_reads("Je maintiens le CSV.")
                self.assertIn("Milo a raison pour le CSV.", relayed)  # second relais : la réaction de Claude
                self.assertEqual(self.status(), DECISION)

    def test_common_question_with_auto_on_reads_crosswise(self):
        self.calm(on=True)
        with self.db() as db:
            self.common = state.common(state.share(db, dict.fromkeys(state.NAMES, "Q2")), "Q2")
        auto.handle_event(self.state, "codex", taken(self.common, "t21"), now=1.0)
        auto.handle_event(self.state, "codex", done(ASK, "t21", self.common), now=2.0)
        self.assertTrue(self.status().startswith("AUTO"))  # aujourd'hui : pause dès cette première réponse
        auto.handle_event(self.state, "claude", typed(self.common, "c9"), now=3.0)
        waiter = auto.handle_event(self.state, "claude", stop("PDF, pour l'impression.", "c9"), now=4.0)
        woke, _ = self.claude_reads(waiter, "Je garde le PDF.")
        self.assertIn("PDF\n- CSV", woke)
        self.assertTrue(self.status().startswith("AUTO"))
        self.codex_reads("D'accord pour le PDF.")
        self.assertEqual(self.status(), DECISION)

    def test_voluntary_off_sends_no_crossread_relay(self):
        with self.db() as db:
            state.put(db, autostart=1)
            common = state.common(state.share(db, dict.fromkeys(state.NAMES, "Q")), "Q")
        auto.handle_event(self.state, "claude", typed(common, "c1"), now=0.1)
        waiter = auto.handle_event(self.state, "claude", stop(ASK, "c1"), now=1.0)
        auto.toggle(self.state, now=1.5)  # Ctrl-b a avant le démarrage : l'utilisateur garde OFF
        auto.handle_event(self.state, "codex", taken(common, "t21"), now=2.0)
        auto.handle_event(self.state, "codex", done("Le CSV suffit.", "t21", common), now=3.0)
        self.assertIsNone(auto.wait(self.state, waiter, clock=clock(), sleep=lambda seconds: None))
        self.send.assert_not_called()
        self.paste.assert_not_called()
        self.assertTrue(self.status().startswith("OFF"))

    def test_late_reply_of_previous_question_is_ignored(self):
        self.calm(on=True)
        old = "Ancienne question"
        auto.handle_event(self.state, "codex", taken(old, "t20"), now=1.0)  # un tour de Codex encore en cours
        with self.db() as db:
            self.common = state.common(state.share(db, dict.fromkeys(state.NAMES, "Q2")), "Q2")
        auto.handle_event(self.state, "codex", taken(self.common, "t21"), now=2.0)
        auto.handle_event(self.state, "codex", done("Réponse de Codex à Q2.", "t21", self.common), now=3.0)
        auto.handle_event(self.state, "claude", typed(self.common, "c9"), now=4.0)
        waiter = auto.handle_event(self.state, "claude", stop(ASK, "c9"), now=5.0)
        # La fin tardive de l'ancien tour arrive sur le même fil, avec une décision.
        auto.handle_event(self.state, "codex", done("Vieille réponse.\n[DÉCISION]", "t20", old), now=6.0)
        woke, _ = self.claude_reads(waiter, "Réaction de Claude.")
        self.assertIn("Réponse de Codex à Q2.", woke)
        self.assertNotIn("Vieille réponse.", woke)
        relayed = self.codex_reads("Réaction de Codex.")
        self.assertIn("Réaction de Claude.", relayed)
        self.assertEqual(self.status(), DECISION)
        with self.db() as db:
            authors = [state.get(db, "decision_seen")] + json.loads(state.get(db, "decision_ids"))
            senders = {db.execute("SELECT sender FROM replies WHERE id = ?", (int(n),)).fetchone()[0]
                       for n in authors}
        self.assertEqual(senders, {"claude"})  # la vieille décision de Codex n'est pas retenue

    def test_scene_banner_announces_the_crossed_reading(self):
        self.launch(ASK, "Le CSV suffit.")
        with closing(scene.read_only(self.state)) as db:
            data = scene.scene_data(db)
        self.assertIn(crossread.READING, data.status)
        lines = ["".join(cell.char for cell in row) for row in scene.frame(data, 110, 30)]
        self.assertIn("LECTURE CROISÉE", lines[0])

    def test_two_distinct_decisions_are_both_kept(self):
        waiter = self.launch(ASK, "Deux langues possibles : FR ou EN ?\n[DÉCISION]")
        self.claude_reads(waiter, "Réaction de Claude.")
        self.codex_reads("Réaction de Codex.")
        self.assertEqual(self.status(), DECISION)
        with closing(scene.read_only(self.state)) as db:
            data = scene.scene_data(db)
        self.assertEqual(set(data.decision_by), {"claude", "codex"})

    def test_decision_then_two_accords_stays_a_decision_until_resume(self):
        waiter = self.launch(ASK, "Le CSV me va.\n[ACCORD]")
        _, waiter = self.claude_reads(waiter, "D'accord pour le CSV.\n[ACCORD]")  # accord détectable, Codex n'a pas lu
        self.assertTrue(self.status().startswith("AUTO"))
        self.codex_reads("Toujours d'accord.\n[ACCORD]")
        self.assertEqual(self.status(), DECISION)
        self.assertNotEqual(self.meta("accord_dropped", "0"), "0")  # inscrit dès la pause de décision
        self.assertEqual(self.meta("accord_seen", "0"), "0")  # l'export ne voit aucun accord confirmé
        self.assertTrue(auto.toggle(self.state, now=60.0).startswith("AUTO"))  # Ctrl-b a : le binôme poursuit
        self.assertEqual(self.meta("accord_seen", "0"), "0")


class OtherPanelTest(Crossread):
    def pause_on_codex_question(self):
        waiter = self.launch(ASK, "Mon avis de Codex.")
        _, waiter = self.claude_reads(waiter, "Réaction de Claude.")
        self.codex_reads("Le format reste à choisir : PDF ou CSV ?\n[DÉCISION]")
        self.assertEqual(self.status(), DECISION)
        return waiter

    def test_answer_in_the_other_panel_receives_the_choice(self):
        self.pause_on_codex_question()
        joined = []
        auto.handle_event(self.state, "claude", typed("La deuxième option", "c50"), now=60.0, send_context=joined.append)
        self.assertEqual(len(joined), 1)
        self.assertTrue(joined[0].startswith("[Sam & Dario]"))
        self.assertIn("PDF ou CSV ?", joined[0])  # le texte effectivement reçu par Claude
        self.assertNotIn("Deux formats possibles", joined[0])  # sa propre décision, il l'a déjà
        with self.db() as db:
            question, = db.execute("SELECT MAX(id) FROM replies WHERE sender = 'codex'").fetchone()
            joined = json.loads(state.get(db, notes.JOINED))
        self.assertIn(question, joined["s"])  # lue une par une : elle ne sera pas relayée une seconde fois

    def test_answer_context_stays_under_9000_characters_with_decisions_first(self):
        history = "Historique. " * 6000
        with self.db() as db:
            first = self.add_reply(db, "codex", history)
            decision = self.add_reply(db, "codex", "Les options : PDF ou CSV.\n" + "Analyse. " * 300 + "\n[DÉCISION]")
            state.put(db, auto=state.PAUSE, reason=rules.REASONS["decision"], decision_seen=decision,
                      decision_ids=json.dumps([decision]))
            context = notes.answer_context(db, "claude")
        self.assertLessEqual(len(context), notes.CONTEXT_LIMIT)
        self.assertEqual(notes.CONTEXT_LIMIT, 9000)
        self.assertLess(context.index("Les options : PDF ou CSV."), context.index("Historique.") if "Historique." in context else len(context))
        self.assertGreater(first, 0)

    def test_too_long_decision_is_not_joined_and_the_bar_says_where_to_answer(self):
        long_decision = "Les options : PDF ou CSV.\n" + "Analyse détaillée. " * 555 + "\nQue choisis-tu ?\n[DÉCISION]"
        self.assertGreater(len(long_decision), 10_500)
        with self.db() as db:
            decision = self.add_reply(db, "codex", long_decision)
            state.put(db, auto=state.PAUSE, reason=rules.REASONS["decision"], decision_seen=decision,
                      decision_ids=json.dumps([decision]))
            self.assertIsNone(notes.answer_context(db, "claude"))  # jamais tronquée : les options sont au début
            self.assertIsNone(db.execute("SELECT upto FROM seen WHERE session = 's' AND sender = 'codex'").fetchone())
            self.assertIn("décision de Codex : réponds chez lui", rules.status_line(db, 50.0))

    def add_reply(self, db, sender, body):
        session = "s" if sender == "claude" else "fil-codex"
        state.put(db, claude_session="s", codex_thread="fil-codex")
        cursor = db.execute("INSERT INTO replies (sender, session, turn, body) VALUES (?, ?, ?, ?)",
                            (sender, session, f"r{next(self.turns)}", body))
        return cursor.lastrowid


class TypedQuestionTest(Crossread):
    def test_question_typed_in_one_panel_opens_a_crossread(self):
        for asked in ("claude", "codex"):
            with self.subTest(asked=asked):
                self.setUp()
                self.calm()
                if asked == "claude":
                    auto.handle_event(self.state, "claude", typed("Quel format pour l'export ?", "c40"), now=1.0)
                    waiter = auto.handle_event(self.state, "claude", stop(ASK, "c40"), now=2.0)
                    self.assertTrue(self.status().startswith("AUTO"))
                    relayed = self.codex_reads("Réaction de Codex.")
                    self.assertIn("Demande de l'utilisateur à Claude", relayed)
                    self.assertIn("Quel format pour l'export ?", relayed)
                    self.assertTrue(self.status().startswith("AUTO"))
                    self.claude_reads(waiter, "Fin de la lecture.")
                else:
                    question = "Quel format pour l'export ?"
                    auto.handle_event(self.state, "codex", taken(question, "t40"), now=1.0)
                    auto.handle_event(self.state, "codex", done(ASK, "t40", question), now=2.0)
                    self.assertTrue(self.status().startswith("AUTO"))
                    waiter = int(self.meta("waiter", "0"))
                    woke, _ = self.claude_reads(waiter, "Réaction de Claude.")
                    self.assertIn("Quel format pour l'export ?", woke)
                    self.assertTrue(self.status().startswith("AUTO"))
                    self.codex_reads("Fin de la lecture.")
                self.assertEqual(self.status(), DECISION)

    def test_same_number_for_a_common_round_and_a_typed_request(self):
        one = crossread.Question("commune", 5, "", "")
        two = crossread.Question("saisie", 5, "claude", "s")
        self.assertNotEqual(one, two)
        self.calm()
        auto.handle_event(self.state, "claude", typed("Question tapée", "c60"), now=1.0)
        with self.db() as db:
            current = crossread.current(db)
            event_id, = db.execute("SELECT MAX(id) FROM events WHERE kind = 'submitted'").fetchone()
            other = self.reply_in(db, "claude", "Réponse d'un autre fil", "autre-fil")
            self.assertEqual(current.question, crossread.Question("saisie", event_id, "claude", "s"))
            self.assertFalse(crossread.belongs(db, other))

    def reply_in(self, db, sender, body, session):
        cursor = db.execute("INSERT INTO replies (sender, session, turn, body) VALUES (?, ?, ?, ?)",
                            (sender, session, "x1", body))
        return cursor.lastrowid


class LimitTest(Crossread):
    def test_max_one_resume_sends_the_remaining_relay_once(self):
        with self.db() as db:
            state.put(db, max=1)
        waiter = self.launch(ASK, "Le CSV suffit.")
        _, waiter = self.claude_reads(waiter, "Réaction de Claude.")
        self.assertTrue(self.status().startswith("PAUSE — " + rules.REASONS["limite"]), self.status())
        sent_before = self.send.call_count
        auto.toggle(self.state, now=60.0)  # reprise : le relais restant part, une seule fois
        self.assertEqual(self.send.call_count, sent_before + 1)
        self.assertIn("Réaction de Claude.", self.send.call_args.args[1])
        self.codex_reads("Fin de la lecture.")
        self.assertEqual(self.status(), DECISION)
        self.assertEqual(self.send.call_count, sent_before + 1)

    def test_new_question_at_the_limit_waits_for_the_due_relay(self):
        with self.db() as db:
            state.put(db, max=1)
        waiter = self.launch(ASK, "Le CSV suffit.")
        _, waiter = self.claude_reads(waiter, "Réaction de Claude à A.")
        self.assertTrue(self.status().startswith("PAUSE — " + rules.REASONS["limite"]))
        with self.db() as db:
            due_a, = db.execute("SELECT MAX(id) FROM replies WHERE sender = 'claude'").fetchone()
        auto.handle_event(self.state, "claude", typed("Question B", "c70"), now=60.0)
        auto.handle_event(self.state, "claude", stop("Réponse à B.", "c70"), now=61.0)
        with self.db() as db:
            answer_b, = db.execute("SELECT MAX(id) FROM replies WHERE sender = 'claude'").fetchone()
        self.assertGreater(answer_b, due_a)
        # La réponse à B relance l'échange, mais le relais envoyé est celui dû à A, seul.
        self.assertIn(f"[Réponse de Claude #{due_a}]", self.send.call_args.args[1])
        self.assertNotIn("Réponse à B.", self.send.call_args.args[1])
        self.assertTrue(self.status().startswith("AUTO 1/1"))
        auto.handle_event(self.state, "codex", taken(self.send.call_args.args[1], "t71"), now=62.0)
        auto.handle_event(self.state, "codex", done("Réaction de Codex à A.", "t71", self.send.call_args.args[1]), now=63.0)
        # Chaque activation respecte sa limite : le relais de B attend une nouvelle reprise.
        self.assertTrue(self.status().startswith("PAUSE — " + rules.REASONS["limite"]), self.status())
        auto.toggle(self.state, now=64.0)
        self.assertIn(f"[Réponse de Claude #{answer_b}]", self.send.call_args.args[1])  # puis la réponse à B


class LegacyTest(Crossread):
    def test_session_without_crossread_state_keeps_the_old_behaviour(self):
        with self.db() as db:
            state.put(db, autostart=1)
            common = state.common(state.share(db, dict.fromkeys(state.NAMES, "Q")), "Q")
            db.execute("DELETE FROM meta WHERE key = 'crossread'")  # base d'avant la tâche 13
        auto.handle_event(self.state, "claude", typed(common, "c1"), now=0.1)
        auto.handle_event(self.state, "codex", taken(common, "t1"), now=0.2)
        auto.handle_event(self.state, "claude", stop(ASK, "c1"), now=1.0)
        auto.handle_event(self.state, "codex", done("Le CSV suffit.", "t1", common), now=2.0)
        self.assertEqual(self.status(), DECISION)  # pause immédiate, comme avant
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
