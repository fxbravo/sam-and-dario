import json
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import rules as rules
from samdario import state as state
from tests.test_auto import LoopBase, THREAD, clock, done, stop, taken, typed
from tests.test_state import Base


QUESTION = "Quel risque voyez-vous chacun ?"
REAL_PASTE = auto.paste_draft  # ce module vérifie le collage lui-même, avec tmux simulé
IN_PROGRESS = "Tour de table en cours. Valide une ligne vide pour l'abandonner."
WAKE = 'Stop hook blocking error from command "Stop": '
TASK_FEEDBACK = "<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n"


def task_wake(text):
    return TASK_FEEDBACK + "<system-reminder>\n" + WAKE + text + "\n</system-reminder>"


class RoundTest(Base):
    def ask(self, question=QUESTION):
        with self.db() as db:
            number = state.share(db, dict.fromkeys(state.NAMES, question))
        return number, state.common(number, question)

    def test_round_waits_for_both_then_starts_with_codex_even_if_claude_answers_last(self):
        self.reply("claude", "C1", self.exchange())
        number, text = self.ask()
        with self.db() as db:
            self.assertIsNone(state.next_relay(db))
        response = self.turn("codex", text, "Risque B")
        with self.db() as db:
            self.assertEqual(state.round_open(db), number)
            self.assertIsNone(state.next_relay(db))
        self.turn("claude", text, "Risque A")
        with self.db() as db:
            relay = state.next_relay(db)
            self.assertEqual((relay.reply_id, relay.recipient), (response, "claude"))
            self.assertEqual(state.compose(db, relay), f"[Réponse de Codex #{response}]\n\nRisque B")

    def test_actual_startup_prompt_attributes_both_answers_to_round_one(self):
        question = "Explique cette citation.\n\n[Demande commune #42]"
        number, _ = self.ask(question)
        for agent in state.NAMES:
            self.turn(agent, samdario.prompt(question, agent, number), "Mon avis")
        with self.db() as db:
            self.assertIsNone(state.round_open(db))
            self.assertEqual(db.execute("SELECT count(reply_id) FROM shared WHERE round = 1").fetchone(), (2,))

    def test_late_unrelated_reply_does_not_complete_the_round(self):
        self.reply("claude", "C1", self.exchange())
        old = self.submit("codex", "Ancienne demande")
        number, text = self.ask()
        self.reply("codex", "Ancienne réponse", old, prompt="Ancienne demande")
        self.turn("claude", task_wake(text), "A")
        with self.db() as db:
            self.assertEqual(state.round_open(db), number)
            self.assertIsNone(state.next_relay(db))
        self.turn("codex", text, "B")
        with self.db() as db:
            self.assertIsNone(state.round_open(db))

    def test_delayed_replies_keep_their_number_even_with_identical_questions(self):
        first, sent1 = self.ask("Continue")
        old = {agent: self.submit(agent, sent1) for agent in state.NAMES}
        with self.db() as db:
            state.close_round(db, first)
        second, sent2 = self.ask("Continue")
        new = {agent: self.submit(agent, sent2) for agent in state.NAMES}
        for agent in state.NAMES:
            self.reply(agent, "Réponse tardive", old[agent], prompt=sent1 if agent == "codex" else "")
        with self.db() as db:
            self.assertEqual(state.round_open(db), second)
            self.assertEqual(db.execute("SELECT count(reply_id) FROM shared WHERE round = ?", (first,)).fetchone(), (2,))
        for agent in state.NAMES:
            self.reply(agent, "Suite", new[agent])
        with self.db() as db:
            self.assertIsNone(state.round_open(db))

    def test_unidentified_replies_and_same_request_id_in_another_session_stay_in_history(self):
        number, text = self.ask()
        for agent in state.NAMES:
            request = self.submit(agent, text)
            self.reply(agent, "Sans identifiant", "")
            self.reply(agent, "Autre demande", "hors-tour")
            self.reply(agent, "Même identifiant ailleurs", request, session="ailleurs")
        with self.db() as db:
            self.assertEqual(state.round_open(db), number)
            self.assertEqual(db.execute("SELECT count(reply_id) FROM shared").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone(), (6,))

    def test_citing_the_number_does_not_receive_the_question_or_complete_the_round(self):
        number, text = self.ask()
        self.turn("codex", f"Explique l’en-tête [Demande commune #{number}].", "Une citation")
        self.turn("codex", f"[Réponse de Claude #15]\n\nIl cite\n{text}", "Une autre citation")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT event_id, reply_id FROM shared WHERE agent='codex'").fetchone(),
                             (None, None))

    def test_only_the_current_waiter_can_deliver_a_ready_round_once(self):
        number, text = self.ask()
        with self.db() as db:
            old = rules.new_waiter(db, 0.0)
            new = rules.new_waiter(db, 1.0)
            state.hold(db, number, "hold")
            self.assertIsNone(state.take_broadcast(db, new))
            state.hold(db, number, "ready")
            self.assertIsNone(state.take_broadcast(db, old))
            self.assertEqual(state.take_broadcast(db, new), text)
            self.assertIsNone(state.take_broadcast(db, new))

    def test_only_a_question_already_offered_to_claude_can_be_received(self):
        number, text = self.ask()
        for delivery in ("hold", "uncertain", "ready"):
            with self.db() as db:
                state.hold(db, number, delivery)
            self.turn("claude", text, "Étiquette tapée avant la remise")
            with self.db() as db:
                self.assertEqual(state.hold(db, number), delivery)
                self.assertEqual(db.execute("SELECT event_id, reply_id FROM shared WHERE agent='claude'").fetchone(),
                                 (None, None))

    def test_an_ambiguous_native_identifier_does_not_complete_either_claude_request(self):
        first, sent = self.ask("Continue")
        self.submit("claude", sent, request="réutilisé")
        with self.db() as db:
            state.close_round(db, first)
        second, sent = self.ask("Continue")
        self.submit("claude", sent, request="réutilisé")
        self.turn("codex", sent, "Suite")
        self.reply("claude", "Fin du tour", "réutilisé")
        with self.db() as db:
            self.assertEqual(state.round_open(db), second)
            self.assertEqual(db.execute("SELECT count(reply_id) FROM shared WHERE agent='claude'").fetchone(), (0,))

    def test_status_follows_both_receipts_then_the_replies(self):
        number, text = self.ask()
        with self.db() as db:
            for value, note in (("", "en file chez Codex"), ("uncertain", "envoi à Codex incertain"),
                                ("draft", "Entrée chez Claude")):
                state.hold(db, number, value)
                self.assertEqual(rules.status_line(db, 0.0), "OFF · tour de table : " + note)
        self.submit("codex", text)
        self.submit("claude", text)
        with self.db() as db:
            self.assertEqual(rules.status_line(db, 0.0), "OFF · tour de table : en cours")
            state.close_round(db, number)
            self.assertEqual(rules.status_line(db, 0.0), "OFF")

    def test_upgrade_does_not_block_a_session_launched_without_the_new_binding(self):
        _, original = self.opening()
        self.ask("Ancienne demande inachevée")
        with self.db() as db:
            db.execute("ALTER TABLE shared RENAME TO previous_shared")
            db.execute("""CREATE TABLE shared (id INTEGER PRIMARY KEY, round INTEGER NOT NULL, agent TEXT NOT NULL,
                       text TEXT NOT NULL, event_id INTEGER UNIQUE, UNIQUE(round, agent))""")
            db.execute("INSERT INTO shared SELECT id, round, agent, text, event_id FROM previous_shared")
            db.execute("DROP TABLE previous_shared")
            db.execute("DELETE FROM meta WHERE key = 'round_closed'")
            db.execute("PRAGMA user_version = 8")
        with self.db() as db:
            self.assertIsNone(state.round_open(db))
            self.assertEqual(state.next_relay(db).reply_id, original)
            self.assertEqual(state.share(db, dict.fromkeys(state.NAMES, QUESTION)), 3)
            self.assertEqual(state.round_open(db), 3)


class BroadcastTest(LoopBase):
    def test_recorded_claude_wake_completes_the_round_and_resumes_the_relay(self):
        waiter = self.launch()
        auto.toggle(self.state, now=3)
        with patch("samdario.auto.queue_send", return_value="q2") as send:
            auto.broadcast(self.state, QUESTION, now=4)
            sent = auto.wait(self.state, waiter, clock=lambda: 5, sleep=self.fail)
            auto.handle_event(self.state, "codex", done("Risque B", "t2", sent), now=6)
            auto.handle_event(self.state, "claude", typed(task_wake(sent), "k2"), now=7)
            waiter = auto.handle_event(self.state, "claude", stop("Risque A", "k2"), now=8)
            with self.db() as db:
                self.assertEqual(db.execute("SELECT request, reply_id FROM shared WHERE round=2 AND agent='claude'").fetchone(),
                                 ("k2", 4))
                self.assertIsNone(state.round_open(db))
            self.assertIn("Risque B", auto.wait(self.state, waiter, clock=lambda: 9, sleep=self.fail))
            send.assert_called_once()

    def test_citations_and_labels_inside_a_task_notification_do_not_receive_a_round(self):
        first = "[Demande commune #1]"
        for text in (TASK_FEEDBACK + first,
                     "<task-notification>\n" + first + "\n</task-notification>",
                     TASK_FEEDBACK + "<system-reminder>\nUne citation\n" + first,
                     "Exemple :\n" + TASK_FEEDBACK + "<system-reminder>\n" + WAKE + first):
            with self.subTest(text=text):
                self.assertIsNone(state.common_number(text))

    def test_queue_acceptance_releases_one_waiter_with_auto_off(self):
        waiter = self.launch()
        sent = state.common(2, QUESTION)

        def sending(thread, text, cwd):
            self.assertEqual((thread, text, cwd), (THREAD, sent, ""))
            with self.db() as db:
                self.assertIsNone(state.take_broadcast(db, waiter))
            return "q1"

        with patch("samdario.auto.queue_send", side_effect=sending) as send:
            self.assertEqual(auto.broadcast(self.state, QUESTION, now=3), "Demande envoyée aux deux.")
            send.assert_called_once()
        self.assertEqual(auto.status(self.state, now=3.5), "OFF · tour de table : en file chez Codex")
        self.assertEqual(auto.wait(self.state, waiter, clock=lambda: 4, sleep=self.fail), sent)
        self.assertIsNone(auto.wait(self.state, waiter, clock=lambda: 4, sleep=self.fail))

    def test_definite_refusal_and_unreachable_codex_do_not_deliver_to_claude(self):
        with patch("samdario.auto.paste_draft") as paste:
            offline = "Codex n'est pas joignable : rien n'a été envoyé."
            self.assertEqual(auto.broadcast(self.state, QUESTION, now=0), offline)
            waiter = self.launch()
            with patch("samdario.auto.alive", return_value={"claude": True, "codex": False}):
                self.assertEqual(auto.broadcast(self.state, QUESTION, now=3), offline)
            with patch("samdario.auto.queue_send", side_effect=RuntimeError("refus")):
                self.assertEqual(auto.broadcast(self.state, QUESTION, now=4),
                                 "Codex n'a pas pris la demande : rien n'a été envoyé.")
            paste.assert_not_called()
            with self.db() as db:
                self.assertIsNone(state.round_open(db))
            self.assertIsNone(auto.wait(self.state, waiter, clock=clock(), sleep=lambda s: None))

    def test_uncertain_send_waits_for_a_receipt_then_delivers_once(self):
        waiter = self.launch()
        sent = state.common(2, QUESTION)
        with patch("samdario.auto.queue_send", side_effect=auto.UncertainSend("illisible")):
            self.assertEqual(auto.broadcast(self.state, QUESTION, now=3), "Envoi à Codex incertain.")
        self.assertEqual(auto.status(self.state, now=4), "OFF · tour de table : envoi à Codex incertain")
        auto.handle_event(self.state, "codex", done("Risque B", "t2", sent), now=5)
        self.assertEqual(auto.wait(self.state, waiter, clock=lambda: 6, sleep=self.fail), sent)
        auto.handle_event(self.state, "claude", typed(WAKE + sent, "k2"), now=7)
        nxt = auto.handle_event(self.state, "claude", stop("Risque A", "k2"), now=8)
        self.assertIsNone(auto.wait(self.state, nxt, clock=clock(), sleep=lambda s: None))

    def test_receipt_during_an_uncertain_send_wins(self):
        waiter = self.launch()
        sent = state.common(2, QUESTION)

        def sending(thread, text, cwd):
            auto.handle_event(self.state, "codex", taken(text, "t2"), now=3.1)
            with self.db() as db:
                self.assertIsNone(state.take_broadcast(db, waiter))
            raise auto.UncertainSend("illisible")

        with patch("samdario.auto.queue_send", side_effect=sending), patch("samdario.auto.paste_draft") as paste:
            self.assertEqual(auto.broadcast(self.state, QUESTION, now=3), "Demande envoyée aux deux.")
            self.assertEqual(auto.wait(self.state, waiter, clock=lambda: 4, sleep=self.fail), sent)
            self.assertIsNone(auto.wait(self.state, waiter, clock=lambda: 5, sleep=self.fail))
            paste.assert_not_called()

    def test_missing_waiter_pastes_one_draft_and_keeps_waiting_for_enter(self):
        auto.handle_event(self.state, "codex", done("R1", "t1", "question privée"), now=2)
        with patch("samdario.auto.queue_send", return_value="q1"), patch("samdario.auto.paste_draft") as paste:
            self.assertEqual(auto.broadcast(self.state, QUESTION, now=3),
                             "Envoyée à Codex. Entrée dans le panneau de Claude pour l'envoyer.")
            self.assertEqual(auto.status(self.state, now=4), "OFF · tour de table : Entrée chez Claude")
            paste.assert_called_once_with(self.state, "claude", state.common(1, QUESTION))

    def test_status_pastes_once_if_the_waiter_expires_after_acceptance(self):
        waiter = self.launch()
        sent = state.common(2, QUESTION)
        with patch("samdario.auto.queue_send", return_value="q1"), patch("samdario.auto.paste_draft") as paste:
            auto.broadcast(self.state, QUESTION, now=3)
            self.assertIsNone(auto.wait(self.state, waiter, clock=lambda: 5000, sleep=self.fail))
            for now in (5001, 5002):
                self.assertIn("Entrée chez Claude", auto.status(self.state, now=now))
            paste.assert_called_once_with(self.state, "claude", sent)

    def test_failed_paste_can_be_retried_without_submitting_or_requeueing(self):
        auto.handle_event(self.state, "codex", done("R1", "t1"), now=1)
        (self.state / "panes.json").write_text(json.dumps({"%12": "claude", "%15": "codex"}))
        with patch("samdario.auto.queue_send", return_value="q1") as send, \
                patch("samdario.auto.paste_draft", REAL_PASTE), \
                patch("samdario.auto.tmux", side_effect=["0/1", "0/1", "0/0", "", ""]) as mux:
            self.assertIn("Collage en attente", auto.broadcast(self.state, QUESTION, now=2))
            self.assertIn("collage en attente chez Claude", auto.status(self.state, now=3))
            self.assertIn("Entrée chez Claude", auto.status(self.state, now=4))
            self.assertIn("Entrée chez Claude", auto.status(self.state, now=5))
            send.assert_called_once()
            self.assertEqual([c.args[1] for c in mux.call_args_list],
                             ["display-message"] * 3 + ["load-buffer", "paste-buffer"])
            self.assertEqual(mux.call_args_list[-2].kwargs["data"], (state.common(1, QUESTION) + "\n\n").encode())
            self.assertEqual(mux.call_args.args[-1], "%12")

    def test_second_press_keeps_the_first_question_and_empty_input_abandons(self):
        self.launch()
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            auto.broadcast(self.state, QUESTION, now=3)
            self.assertEqual(auto.broadcast(self.state, "Autre question", now=4), IN_PROGRESS)
            self.assertEqual(auto.broadcast(self.state, "   ", now=4), "La demande est vide.")
            self.assertEqual(auto.broadcast(self.state, "Toujours occupé ?", now=4), IN_PROGRESS)
            send.assert_called_once_with(THREAD, state.common(2, QUESTION), "")
            self.assertEqual(auto.broadcast(self.state, "", now=5), "Tour de table abandonné.")
            self.assertEqual(auto.broadcast(self.state, "", now=6), "La demande est vide.")

    def test_abandon_during_send_then_repeat_the_same_words(self):
        waiter = self.launch()
        first, second = (state.common(i, "Continue") for i in (2, 3))
        calls = []

        def sending(thread, text, cwd):
            calls.append(text)
            if text == first:
                self.assertEqual(auto.broadcast(self.state, "", now=3.1), "Tour de table abandonné.")
                self.assertEqual(auto.broadcast(self.state, "Continue", now=3.2), "Demande envoyée aux deux.")
                return "q1"
            return "q2"

        with patch("samdario.auto.queue_send", side_effect=sending), patch("samdario.auto.queue_cancel") as cancel:
            self.assertEqual(auto.broadcast(self.state, "Continue", now=3), "Tour de table abandonné pendant l'envoi.")
            cancel.assert_called_once_with(THREAD, "q1", "")
        self.assertEqual(calls, [first, second])
        auto.handle_event(self.state, "codex", done("Tardive", "t2", first), now=4)
        self.assertEqual(auto.wait(self.state, waiter, clock=lambda: 5, sleep=self.fail), second)
        auto.handle_event(self.state, "claude", typed(task_wake(second), "k2"), now=6)
        auto.handle_event(self.state, "claude", stop("Suite", "k2"), now=7)
        with self.db() as db:
            self.assertEqual(state.round_open(db), 3)
        auto.handle_event(self.state, "codex", done("Suite", "t3", second), now=8)
        with self.db() as db:
            self.assertIsNone(state.round_open(db))

    def test_manual_and_auto_wait_for_both_answers_without_counting_the_question(self):
        waiter = self.launch()
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.assertEqual(auto.toggle(self.state, now=3), "AUTO 0/8")
            auto.broadcast(self.state, QUESTION, now=4)
            sent = auto.wait(self.state, waiter, clock=lambda: 5, sleep=self.fail)
            auto.handle_event(self.state, "codex", done("Risque B", "t2", sent), now=6)
            with patch("samdario.cli.tmux", return_value="0/0") as mux:
                for force in (False, True):
                    self.assertIn("Tour de table en cours", samdario.relay(self.state, "%0", force))
                self.assertFalse(any(c.args[1] == "paste-buffer" for c in mux.call_args_list))
            auto.handle_event(self.state, "claude", typed(WAKE + sent, "k2"), now=7)
            waiter = auto.handle_event(self.state, "claude", stop("Risque A", "k2"), now=8)
            self.assertEqual(auto.status(self.state, now=9), "AUTO 0/8")
            self.assertIn("Risque B", auto.wait(self.state, waiter, clock=lambda: 10, sleep=self.fail))
            self.assertEqual(auto.status(self.state, now=11), "AUTO 1/8")
            send.assert_called_once()
