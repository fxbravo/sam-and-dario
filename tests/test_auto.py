import itertools
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import rules as rules
from samdario import state as state
from tests.test_state import Base


# Les charges ci-dessous ont la forme des événements natifs ; chaque fin de tour porte l'identifiant de sa demande.
THREAD = "fil-codex"


def typed(prompt, request):
    """Claude reçoit une demande : `prompt_id` relie cette réception à sa fin de tour."""
    return {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt_id": request, "prompt": prompt}


def stop(body, request):
    """Fin du tour de Claude qui traite la demande `request`."""
    return {"hook_event_name": "Stop", "session_id": "s", "prompt_id": request, "last_assistant_message": body}


def done(body, turn, prompt=""):
    """Notification de Codex : elle reprend le texte entier du message auquel il répond."""
    return {"type": "agent-turn-complete", "thread-id": THREAD, "turn-id": turn,
            "last-assistant-message": body, "input-messages": [prompt]}


def taken(prompt, turn):
    """Hook de Codex : il vient de prendre ce message en charge."""
    return {"hook_event_name": "UserPromptSubmit", "session_id": THREAD, "turn_id": turn, "prompt": prompt}


def failure(request=None):
    """Claude Code n'a pas pu finir le tour (limite d'usage, erreur de l'API). `prompt_id` peut manquer."""
    raw = {"hook_event_name": "StopFailure", "session_id": "s", "error": "rate_limit",
           "last_assistant_message": "API Error: Rate limit reached"}
    return raw if request is None else dict(raw, prompt_id=request)


PAUSED_BY_FAILURE = "PAUSE — Claude n'a pas pu répondre : un nouveau tour de Claude est nécessaire"


def clock(start=10.0, step=1000.0):
    """Horloge dont chaque lecture avance de `step` secondes."""
    ticks = itertools.count()
    return lambda: start + step * next(ticks)


@contextmanager
def temporary_state():
    with tempfile.TemporaryDirectory(prefix="samdario canaux #", dir="/tmp") as name:
        yield Path(name)


class LoopBase(Base):
    def setUp(self):
        super().setUp()
        for name, value in (("alive", {"claude": True, "codex": True}), ("codex_hooks_trusted", True)):
            patcher = patch(f"samdario.auto.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("queue_send", "queue_cancel", "queue_holds", "paste_draft"):  # ni Codex ni tmux réels
            patcher = patch(f"samdario.auto.{name}", side_effect=AssertionError(f"appel imprévu : {name}"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def launch(self):
        # Lancement : la demande commune part aux deux, chacun répond. Renvoie l'attente de Claude.
        with self.db() as db:
            sent = state.common(state.share(db, {"claude": "Question", "codex": "Question"}), "Question")
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        waiter = auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)  # réponse 1
        auto.handle_event(self.state, "codex", done("R1", "t1", sent), now=2.0)  # réponse 2
        return waiter

    def started(self):
        # Ouverture, premier relais remis et répondu, auto actif. Claude parle ensuite sur la session "s".
        self.reply("claude", "Bien reçu.", self.exchange())
        with self.db() as db:
            state.put(db, codex_thread=THREAD, claude_session="s")
            rules.toggle(db, 0.0, True)


class LoopTest(LoopBase):
    def test_full_loop_until_agreement(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            first = self.launch()
            self.assertEqual(auto.toggle(self.state, now=3.0), "AUTO 0/8")
            woke = auto.wait(self.state, first, clock=lambda: 4.0, sleep=lambda seconds: None)
            self.assertEqual(woke, "[Réponse de Codex #2]\n\nR1")
            auto.handle_event(self.state, "claude",
                              typed('Stop hook blocking error from command "Stop": ' + woke, "k2"), now=4.1)
            second = auto.handle_event(self.state, "claude", stop("C2\n[ACCORD]", "k2"), now=5.0)  # réponse 3
            relayed = ("[Réponse de Claude #3]\n\nAvant cette réponse, depuis le dernier relais :\n\n"
                       "— Réponse de Claude :\nC1\n\n"
                       "— Réponse la plus récente de Claude :\nC2\n[ACCORD]")
            send.assert_called_once_with(THREAD, relayed, "")
            auto.handle_event(self.state, "codex", done("R2\n[ACCORD]", "t2", relayed), now=7.0)  # message entier
            self.assertIsNone(auto.wait(self.state, second, clock=clock(), sleep=lambda seconds: None))
            self.assertEqual(auto.status(self.state, now=8.0), "PAUSE — accord trouvé")
            send.assert_called_once()

    def test_only_a_claude_reply_creates_a_waiter(self):
        self.assertIsNone(auto.handle_event(self.state, "claude", typed("Question", "k1"), now=0.5))
        self.assertEqual(auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0), 1)
        self.assertIsNone(auto.handle_event(self.state, "codex", done("R1", "t1"), now=2.0))
        self.assertIsNone(auto.handle_event(self.state, "claude", {"hook_event_name": "Autre"}, now=3.0))

    def test_superseded_waiter_stops_at_once(self):
        first = auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        auto.handle_event(self.state, "claude", stop("C1 bis", "k2"), now=2.0)
        self.assertIsNone(auto.wait(self.state, first, clock=lambda: 3.0, sleep=self.fail))

    def test_failed_send_pauses_and_keeps_the_reply(self):
        self.started()
        with patch("samdario.auto.queue_send", side_effect=RuntimeError("erreur de Codex")):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=2.0), "PAUSE — suivi perdu")
        with self.db() as db:
            self.assertEqual((state.get(db, "count"), state.get(db, "awaiting")), ("0", ""))

    def test_unreadable_queue_output_pauses_without_losing_the_relay(self):
        self.started()
        with patch("samdario.auto.queue_send", side_effect=auto.UncertainSend("sortie illisible")):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=2.0), "PAUSE — remise incertaine")
        with self.db() as db:
            self.assertEqual((state.get(db, "count"), state.get(db, "awaiting")), ("1", "codex"))

    def test_nothing_is_sent_to_a_closed_codex_pane(self):
        self.started()
        with patch("samdario.auto.alive", return_value={"claude": True, "codex": False}), \
                patch("samdario.auto.queue_send") as send:
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
            send.assert_not_called()
            self.assertEqual(auto.status(self.state, now=2.0), "PAUSE — suivi perdu")

    def test_pane_closed_while_waiting_for_a_reply_pauses(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1"):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=2.0), "AUTO 1/8")
        with patch("samdario.auto.alive", return_value={"claude": True, "codex": False}):
            self.assertEqual(auto.status(self.state, now=3.0), "PAUSE — suivi perdu")

    def test_off_withdraws_a_queued_relay_and_nothing_reaches_claude(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1"), \
                patch("samdario.auto.queue_cancel", return_value=True) as cancel:
            waiter = auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
            self.assertEqual(auto.toggle(self.state, now=2.0), "OFF")
            cancel.assert_called_once_with(THREAD, "q1", "")
            auto.handle_event(self.state, "codex", done("R1", "t1", "une demande tapée chez Codex"), now=3.0)
            self.assertIsNone(auto.wait(self.state, waiter, clock=lambda: 4.0, sleep=self.fail))

    def test_off_during_the_send_withdraws_the_relay_afterwards(self):
        self.started()

        def cut(thread, text, cwd):
            auto.toggle(self.state, now=1.5)  # OFF pendant `codex queue`
            return "q1"

        with patch("samdario.auto.queue_send", side_effect=cut), \
                patch("samdario.auto.queue_cancel", return_value=True) as cancel:
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
            cancel.assert_called_once_with(THREAD, "q1", "")
        self.assertEqual(auto.status(self.state, now=2.0), "OFF")
        with self.db() as db:
            self.assertIsNotNone(state.next_relay(db))  # la réponse reste à relayer

    def test_off_during_the_send_when_the_copy_is_no_longer_in_the_queue(self):
        self.started()

        def cut(thread, text, cwd):
            auto.toggle(self.state, now=1.5)
            return "q1"

        with patch("samdario.auto.queue_send", side_effect=cut), \
                patch("samdario.auto.queue_cancel", return_value=False):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=2.0), "PAUSE — remise incertaine")  # coupure seule

    def test_confirmation_received_during_the_send_stands(self):
        self.started()

        def sending(thread, text, cwd):
            auto.handle_event(self.state, "codex", taken(text, "t9"), now=1.2)  # Codex confirme avant le retour
            return "q1"

        with patch("samdario.auto.queue_send", side_effect=sending), \
                patch("samdario.auto.queue_cancel") as cancel:
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
            cancel.assert_not_called()  # on ne retire pas un message déjà reçu
        self.assertEqual(auto.status(self.state, now=2.0), "AUTO 1/8")
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "codex")

    def test_reception_on_the_original_thread_is_confirmed_after_another_thread_spoke(self):
        self.started()

        def sending(thread, text, cwd):
            other = dict(done("RB", "tb", "une question sur un autre fil"), **{"thread-id": "fil-B"})
            auto.handle_event(self.state, "codex", other, now=1.1)  # un autre fil répond pendant l'envoi
            return "q1"

        with patch("samdario.auto.queue_send", side_effect=sending) as send:
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        relayed = send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(relayed, "t9"), now=2.0)  # accusé sur le fil d'origine
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status, target_session FROM replies WHERE body = 'C1'").fetchone(),
                             (state.SENT, THREAD))
        auto.handle_event(self.state, "codex", done("R2", "t9", relayed), now=3.0)
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "")  # la réponse de Codex est rattachée au relais

    def test_failure_reported_after_codex_confirmed_does_not_pause(self):
        self.started()

        def sending(thread, text, cwd):
            auto.handle_event(self.state, "codex", taken(text, "t9"), now=1.2)
            raise RuntimeError("erreur signalée après coup")

        with patch("samdario.auto.queue_send", side_effect=sending):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=2.0), "AUTO 1/8")  # la remise confirmée fait foi

    def test_activation_asks_codex_for_both_hooks(self):
        with patch("samdario.auto.codex_hooks_trusted", return_value=False):
            self.assertEqual(auto.toggle(self.state, now=1.0), "PAUSE — hooks Codex à valider dans /hooks")

    def test_restart_refused_by_a_closed_pane_keeps_the_counter(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1"), \
                patch("samdario.auto.queue_cancel", return_value=False):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)  # un relais compté, en attente de Codex
            auto.toggle(self.state, now=2.0)  # coupure : plus dans la file, sort incertain, il reste compté
        with patch("samdario.auto.alive", return_value={"claude": False, "codex": True}):
            self.assertEqual(auto.toggle(self.state, now=3.0), "PAUSE — suivi perdu")
        with self.db() as db:
            self.assertEqual(state.get(db, "count"), "1")

    def awaiting_claude(self, send):
        # Ouverture, auto actif, relais de Codex remis à Claude et confirmé : la barre affiche AUTO 1/8.
        first = self.launch()
        auto.toggle(self.state, now=3.0)
        woke = auto.wait(self.state, first, clock=lambda: 4.0, sleep=lambda seconds: None)
        auto.handle_event(self.state, "claude",
                          typed('Stop hook blocking error from command "Stop": ' + woke, "k2"), now=4.1)
        self.assertEqual(auto.status(self.state, now=4.2), "AUTO 1/8")

    def test_claude_failing_to_answer_pauses_and_a_new_turn_resumes(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.awaiting_claude(send)
            self.assertIsNone(auto.handle_event(self.state, "claude", failure(), now=5.0))  # sans prompt_id
            self.assertEqual(auto.status(self.state, now=5.1), PAUSED_BY_FAILURE)
            with self.db() as db:
                self.assertEqual(state.get(db, "count"), "1")
            # Reprise immédiate refusée : aucun relais ne peut venir tant que Claude n'a pas refait un tour.
            self.assertEqual(auto.toggle(self.state, now=5.5), PAUSED_BY_FAILURE)
            auto.handle_event(self.state, "claude", typed("Reprends ta réponse.", "k3"), now=6.0)
            auto.handle_event(self.state, "claude", stop("C2", "k3"), now=7.0)
            send.assert_not_called()  # en pause
            self.assertEqual(auto.toggle(self.state, now=8.0), "AUTO 1/8")
            self.assertTrue(send.call_args.args[1].startswith("[Réponse de Claude #3]"))

    def test_only_a_turn_started_after_the_failure_lifts_the_pause(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.awaiting_claude(send)
            auto.handle_event(self.state, "claude", failure("k2"), now=5.0)
            auto.handle_event(self.state, "claude", stop("C0 tardive", "k0"), now=5.1)  # fin tardive d'un ancien tour
            again = 'Stop hook blocking error from command "Stop": [Réponse de Codex #2]\n\nR1'
            auto.handle_event(self.state, "claude", typed(again, "k2"), now=5.2)  # doublon de la soumission échouée
            self.assertEqual(auto.toggle(self.state, now=5.3), PAUSED_BY_FAILURE)
            auto.handle_event(self.state, "claude", typed("Reprends.", "k3"), now=6.0)
            self.assertEqual(auto.toggle(self.state, now=6.1), PAUSED_BY_FAILURE)  # le tour k3 est en cours
            auto.handle_event(self.state, "claude", stop("C3", "k3"), now=7.0)
            send.assert_not_called()
            self.assertEqual(auto.toggle(self.state, now=8.0), "AUTO 1/8")
            self.assertTrue(send.call_args.args[1].endswith("C3"))

    def test_repeated_report_of_the_same_failure_keeps_the_first_one(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.awaiting_claude(send)
            auto.handle_event(self.state, "claude", failure("k2"), now=5.0)
            auto.handle_event(self.state, "claude", typed("Reprends.", "k3"), now=5.5)
            auto.handle_event(self.state, "claude", failure("k2"), now=5.7)  # second signalement du même échec
            auto.handle_event(self.state, "claude", stop("C3", "k3"), now=6.0)
            self.assertEqual(auto.toggle(self.state, now=8.0), "AUTO 1/8")
            self.assertTrue(send.call_args.args[1].endswith("C3"))

    def test_repeated_report_of_an_older_failure_keeps_the_newer_one(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.awaiting_claude(send)
            auto.handle_event(self.state, "claude", failure("k2"), now=5.0)
            auto.handle_event(self.state, "claude", typed("Reprends.", "k3"), now=5.5)
            auto.handle_event(self.state, "claude", typed("Autre chose.", "k4"), now=5.6)
            auto.handle_event(self.state, "claude", failure("k4"), now=5.7)
            auto.handle_event(self.state, "claude", failure("k2"), now=5.8)  # doublon de l'ancien échec
            auto.handle_event(self.state, "claude", stop("C3", "k3"), now=6.0)  # k3 a été soumis avant l'échec de k4
            self.assertEqual(auto.toggle(self.state, now=8.0), PAUSED_BY_FAILURE)
            send.assert_not_called()

    def test_failure_flag_left_by_the_previous_plan_still_needs_a_new_turn(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.awaiting_claude(send)
            auto.handle_event(self.state, "claude", failure("k2"), now=5.0)
            with self.db() as db:  # base du plan 17 : l'échec y était un simple drapeau
                state.put(db, claude_failed=1)
                db.execute("PRAGMA user_version = 6")
            auto.handle_event(self.state, "claude", stop("C2 ancienne", "k2"), now=5.1)  # demande d'avant l'échec
            self.assertEqual(auto.toggle(self.state, now=5.3), PAUSED_BY_FAILURE)
            auto.handle_event(self.state, "claude", typed("Reprends.", "k3"), now=6.0)
            auto.handle_event(self.state, "claude", stop("C3", "k3"), now=7.0)
            self.assertEqual(auto.toggle(self.state, now=8.0), "AUTO 1/8")

    def test_late_failure_of_a_turn_older_than_a_finished_one_is_only_journaled(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.awaiting_claude(send)
            auto.handle_event(self.state, "claude", stop("C2", "k2"), now=5.0)  # le tour k2 finit, relayé
            auto.handle_event(self.state, "claude", failure("k1"), now=6.0)  # échec tardif du tour k1, plus ancien
        self.assertEqual(auto.status(self.state, now=7.0), "AUTO 2/8")

    def test_failure_of_another_turn_keeps_the_awaited_relay(self):
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.awaiting_claude(send)
            auto.handle_event(self.state, "claude", failure("autre demande"), now=5.0)  # journalisé seulement
            self.assertEqual(auto.status(self.state, now=5.1), "AUTO 1/8")
            with self.db() as db:
                self.assertEqual(state.get(db, "awaiting"), "claude")
            auto.handle_event(self.state, "claude", stop("C2", "k2"), now=6.0)  # la bonne réponse arrive
            self.assertTrue(send.call_args.args[1].startswith("[Réponse de Claude #3]"))

    def test_claude_failing_while_auto_is_off_stays_off_until_the_next_press(self):
        with self.db() as db:
            sent = state.common(state.share(db, {"claude": "Question", "codex": "Question"}), "Question")
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", failure("k1"), now=1.0)  # la demande d'ouverture échoue
        auto.handle_event(self.state, "codex", done("R1", "t1", sent), now=1.5)
        pending = " · tour de table : en cours"
        self.assertEqual(auto.status(self.state, now=2.0), "OFF" + pending)
        self.assertEqual(auto.toggle(self.state, now=3.0), PAUSED_BY_FAILURE + pending)

    def queued_then_cut(self, cancelled):
        # Un relais part dans la file de Codex (q1), puis l'auto est coupé ; le retrait répond `cancelled`.
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1"), \
                patch("samdario.auto.queue_cancel", return_value=cancelled) as cancel:
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
            auto.toggle(self.state, now=2.0)
            cancel.assert_called_once_with(THREAD, "q1", "")

    def test_withdrawal_without_answer_keeps_the_message_uncertain(self):
        self.queued_then_cut(None)  # app-server muet : rien ne prouve le retrait ni la réception
        self.assertEqual(auto.status(self.state, now=3.0), "PAUSE — remise incertaine")
        with self.db() as db:
            number, status, queue_id = db.execute("SELECT id, status, queue_id FROM replies WHERE body = 'C1'").fetchone()
        self.assertEqual((status, queue_id), (state.UNCERTAIN, "q1"))
        auto.handle_event(self.state, "codex", taken(f"[Réponse de Claude #{number}]\n\nC1", "t9"), now=4.0)
        with self.db() as db:  # la réception confirmée fait foi
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.SENT,))

    def paste(self, cancelled):
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0") as mux, \
                patch("samdario.auto.queue_cancel", return_value=cancelled) as cancel:
            notice = samdario.relay(self.state, "%1", force=True)
            cancel.assert_called_once_with(THREAD, "q1", "")
        return notice, mux

    def test_withdrawals_target_the_thread_where_the_copy_was_queued(self):
        self.queued_then_cut(None)
        with self.db() as db:
            state.put(db, codex_thread="fil-B")  # un autre fil Codex s'ouvre pendant la pause
        self.paste(True)  # le retrait vise toujours le fil d'origine
        self.setUp()
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1"), \
                patch("samdario.auto.queue_cancel", return_value=True) as cancel:
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
            with self.db() as db:
                state.put(db, codex_thread="fil-B")
            auto.toggle(self.state, now=2.0)
            cancel.assert_called_once_with(THREAD, "q1", "")

    def previous_plan_with_the_wrong_thread_on_the_reply(self):
        # Base du plan 17 : la copie est dans la file de A, mais la réponse désigne B, le fil actif.
        with self.db() as db:
            db.execute("UPDATE replies SET target_session = 'fil-B' WHERE body = 'C1'")
            state.put(db, codex_thread="fil-B")
            db.execute("PRAGMA user_version = 6")

    def test_previous_plan_divergence_withdraws_from_both_candidate_threads(self):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1"):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.previous_plan_with_the_wrong_thread_on_the_reply()
        with patch("samdario.auto.queue_cancel", side_effect=lambda thread, queue_id, cwd: thread == THREAD) as cancel, \
                patch("samdario.auto.queue_holds", return_value=True) as holds:
            self.assertEqual(auto.toggle(self.state, now=2.0), "OFF")
            self.assertEqual([call.args for call in cancel.call_args_list], [("fil-B", "q1", ""), (THREAD, "q1", "")])
            # Dans chaque fil candidat, Sam & Dario vérifie d'abord que le message portant cet identifiant est le sien.
            self.assertEqual([call.args for call in holds.call_args_list],
                             [("fil-B", "q1", "[Réponse de Claude #4]", ""), (THREAD, "q1", "[Réponse de Claude #4]", "")])
        with self.db() as db:  # retirée de A : la réponse est libre
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.NEW,))

    def test_candidate_thread_is_left_alone_when_its_message_is_not_ours(self):
        for ours, expected in ((THREAD, state.NEW), (None, state.UNCERTAIN)):
            with self.subTest(ours=ours):
                self.setUp()
                self.started()
                with patch("samdario.auto.queue_send", return_value="q1"):
                    auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
                self.previous_plan_with_the_wrong_thread_on_the_reply()
                # B, candidat désigné par la réponse, contient sous q1 un message personnel de l'utilisateur.
                with patch("samdario.auto.queue_cancel", return_value=True) as cancel, \
                        patch("samdario.auto.queue_holds", side_effect=lambda thread, *_: thread == ours):
                    auto.toggle(self.state, now=2.0)
                    self.assertEqual([call.args for call in cancel.call_args_list],
                                     [(THREAD, "q1", "")] if ours else [])
                with self.db() as db:
                    self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (expected,))

    def test_previous_plan_divergence_is_withdrawn_from_both_threads_before_a_paste(self):
        self.queued_then_cut(None)
        self.previous_plan_with_the_wrong_thread_on_the_reply()
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0"), patch("samdario.auto.queue_holds", return_value=True), \
                patch("samdario.auto.queue_cancel", side_effect=lambda thread, queue_id, cwd: thread == THREAD) as cancel:
            samdario.relay(self.state, "%1", force=True)
            self.assertEqual([call.args for call in cancel.call_args_list], [("fil-B", "q1", ""), (THREAD, "q1", "")])

    def test_absence_from_the_queue_at_the_cut_keeps_the_copy_uncertain(self):
        self.queued_then_cut(False)  # pas d'accusé de Codex : rien ne prouve la réception
        self.assertEqual(auto.status(self.state, now=3.0), "PAUSE — remise incertaine")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status, queue_id FROM replies WHERE body = 'C1'").fetchone(),
                             (state.UNCERTAIN, "q1"))
        notice, mux = self.paste(False)  # Ctrl-b R reste possible, avec l'avertissement
        self.assertIn("pas confirmé", notice)
        self.assertEqual(mux.call_args_list[2].args[1], "paste-buffer")

    def test_queue_id_arriving_after_the_deadline_can_still_be_withdrawn(self):
        self.started()

        def late(thread, text, cwd):
            auto.status(self.state, now=1.0 + rules.SEND_SECONDS + 1)  # copie perdue de vue
            return "q1"

        with patch("samdario.auto.queue_send", side_effect=late), \
                patch("samdario.auto.queue_cancel", return_value=None):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        notice, _ = self.paste(True)  # le retrait muet est retenté par Ctrl-b R avec l'identifiant tardif
        self.assertEqual(notice, "Réponse de Claude collée. Entrée pour envoyer.")

    def test_forced_paste_first_withdraws_the_uncertain_copy(self):
        self.queued_then_cut(None)
        notice, mux = self.paste(True)
        self.assertEqual(notice, "Réponse de Claude collée. Entrée pour envoyer.")
        self.assertEqual(mux.call_args_list[2].args[1], "paste-buffer")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.DRAFTED,))
            self.assertEqual(db.execute("SELECT state FROM claims").fetchall()[-1], (rules.WITHDRAWN,))

    def test_forced_paste_warns_when_the_fate_of_the_copy_stays_unknown(self):
        # Sans réponse, ou « plus dans la file » : la première tentative restée muette a pu la retirer,
        # donc rien ne prouve la réception. Le choix de l'utilisateur est respecté, avec un avertissement.
        for cancelled in (None, False):
            with self.subTest(cancelled=cancelled):
                self.setUp()
                self.queued_then_cut(None)
                notice, mux = self.paste(cancelled)
                self.assertIn("pas confirmé", notice)
                self.assertEqual(mux.call_args_list[2].args[1], "paste-buffer")
                with self.db() as db:
                    self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(),
                                     (state.DRAFTED,))

    def test_second_press_during_the_hooks_check_withdraws_what_the_first_queued(self):
        self.started()
        with self.db() as db:
            rules.toggle(db, 0.5, True)  # auto coupé
        auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)  # une réponse attend son relais
        checks = itertools.count()

        def checking(folder):
            if next(checks) == 0:
                auto.toggle(self.state, now=2.5)  # second appui, pendant la vérification du premier
            return True

        with patch("samdario.auto.codex_hooks_trusted", side_effect=checking), \
                patch("samdario.auto.queue_send", return_value="q1"), \
                patch("samdario.auto.queue_cancel", return_value=True) as cancel:
            self.assertEqual(auto.toggle(self.state, now=2.0), "OFF")
            cancel.assert_called_once_with(THREAD, "q1", "")

    def restarted_during_the_send(self, first_send, presses=2):
        # Auto coupé, trois relais déjà comptés, une réponse attend. Ctrl-b a ; pendant le premier envoi,
        # `presses` appuis (OFF puis ON, par défaut). `first_send` donne l'issue de chaque envoi.
        self.started()
        with self.db() as db:
            rules.toggle(db, 0.5, True)
            state.put(db, count=3)
        auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        calls = itertools.count()

        def sending(thread, text, cwd):
            if next(calls) == 0:
                for press in range(presses):
                    auto.toggle(self.state, now=2.1 + press / 10)
            return first_send()

        return sending

    def test_late_refusal_under_a_newer_activation_retries_once(self):
        outcomes = iter([RuntimeError("refus de codex queue"), "q2"])

        def first_send():
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("samdario.auto.queue_send", side_effect=self.restarted_during_the_send(first_send)) as send, \
                patch("samdario.auto.queue_cancel") as cancel:
            auto.toggle(self.state, now=2.0)
            self.assertEqual(send.call_count, 2)
            cancel.assert_not_called()
        # Le compteur appartient à la nouvelle activation : seule la nouvelle tentative y est comptée.
        self.assertEqual(auto.status(self.state, now=3.0), "AUTO 1/8")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.QUEUED,))

    def test_refusal_of_the_retry_pauses(self):
        def first_send():
            raise RuntimeError("refus de codex queue")

        with patch("samdario.auto.queue_send", side_effect=self.restarted_during_the_send(first_send)) as send:
            auto.toggle(self.state, now=2.0)
            self.assertEqual(send.call_count, 2)  # une seule nouvelle tentative
        self.assertEqual(auto.status(self.state, now=3.0), "PAUSE — suivi perdu")
        with self.db() as db:  # rien n'est parti : la réponse reste à relayer
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.NEW,))
            # Le compteur reprend sa valeur d'avant l'appui qui a ouvert l'activation refusée, moins la copie
            # alors en cours d'envoi, dont le refus est désormais établi : 0. Ce n'est pas celle d'avant le
            # premier appui (3), qui comptait des relais d'une activation plus ancienne.
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "count")), ("", "0"))

    def test_next_pass_sends_a_reply_left_ready_by_an_interrupted_process(self):
        self.started()
        with patch("samdario.auto.advance"):  # processus arrêté avant l'envoi : la réponse reste prête
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.assertEqual(auto.status(self.state, now=86_400.0), "AUTO 1/8")
            send.assert_called_once()

    def test_pass_started_before_a_press_does_not_overwrite_it(self):
        for presses, expected in ((1, "OFF"), (2, "AUTO 1/8")):  # OFF, ou OFF puis ON
            with self.subTest(presses=presses):
                self.setUp()
                self.started()
                checks = itertools.count()

                def checking(folder):
                    if next(checks) == 0:  # l'ancienne lecture des panneaux revient après les appuis
                        for press in range(presses):
                            auto.toggle(self.state, now=2.1 + press / 10)
                        return {"claude": True, "codex": False}
                    return {"claude": True, "codex": True}

                with patch("samdario.auto.alive", side_effect=checking), \
                        patch("samdario.auto.queue_send", return_value="q1"):
                    self.assertEqual(auto.status(self.state, now=2.0), expected)

    def test_refused_first_send_reserved_by_the_status_bar_keeps_the_counter(self):
        self.started()
        with self.db() as db:
            rules.toggle(db, 0.5, True)  # auto coupé
            state.put(db, count=3)
        auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)  # une réponse attend son relais
        with self.db() as db:
            rules.toggle(db, 2.0, True)  # l'appui active l'auto ; la barre réserve l'envoi avant lui
        with patch("samdario.auto.queue_send", side_effect=RuntimeError("refus de codex queue")):
            self.assertEqual(auto.status(self.state, now=2.1), "PAUSE — suivi perdu")
        with self.db() as db:
            self.assertEqual(state.get(db, "count"), "3")

    def test_refusal_after_the_send_deadline_says_the_reply_is_free_again(self):
        self.started()

        def late(thread, text, cwd):
            auto.status(self.state, now=1.0 + rules.SEND_SECONDS + 1)  # copie perdue de vue : pause incertaine
            raise RuntimeError("refus de codex queue")

        with patch("samdario.auto.queue_send", side_effect=late):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=200.0), "PAUSE — suivi perdu")  # rien n'est parti
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.NEW,))

    def test_acknowledgement_after_the_send_deadline_prevails_over_a_late_refusal(self):
        self.started()

        def late(thread, text, cwd):
            auto.status(self.state, now=1.0 + rules.SEND_SECONDS + 1)  # copie perdue de vue : pause incertaine
            auto.handle_event(self.state, "codex", taken(text, "t9"), now=1.0 + rules.SEND_SECONDS + 2)  # accusé
            raise RuntimeError("refus de codex queue")

        with patch("samdario.auto.queue_send", side_effect=late):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=200.0), "PAUSE — remise incertaine")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.SENT,))
            self.assertEqual(state.get(db, "count"), "1")

    def test_manual_resend_after_ctrl_b_r_leaves_the_automatic_copy_to_its_own_outcome(self):
        for answer in (False, None):  # la copie automatique n'est plus dans la file, ou pas de réponse
            with self.subTest(answer=answer):
                self.setUp()
                self.started()
                (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))

                def late(thread, text, cwd):
                    auto.status(self.state, now=1.0 + rules.SEND_SECONDS + 1)  # copie perdue de vue
                    with patch("samdario.cli.tmux", return_value="0/0"):
                        samdario.relay(self.state, "%1", force=True)  # Ctrl-b R
                    with self.db() as db:
                        pasted, = db.execute("SELECT pasted FROM replies WHERE body = 'C1'").fetchone()
                    auto.handle_event(self.state, "codex", taken(pasted, "t9"), now=1.0 + rules.SEND_SECONDS + 2)
                    return "q1"  # retour tardif : la copie automatique était en file

                with patch("samdario.auto.queue_send", side_effect=late), \
                        patch("samdario.auto.queue_cancel", return_value=answer):
                    auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
                # L'envoi manuel est acquis ; le sort de la copie automatique reste inconnu (doublon possible).
                self.assertEqual(auto.status(self.state, now=300.0), "PAUSE — remise incertaine")
                with self.db() as db:
                    self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(),
                                     (state.SENT,))
                    self.assertEqual(db.execute("SELECT state FROM claims ORDER BY id DESC LIMIT 1").fetchone(),
                                     (state.UNCERTAIN,))
                self.assertEqual(auto.toggle(self.state, now=301.0), "AUTO 0/8")  # la reprise n'en est pas bloquée

    def test_label_cited_in_a_question_is_no_agreement(self):
        self.started()
        with self.db() as db:
            rules.toggle(db, 0.5, True)  # auto coupé
        auto.handle_event(self.state, "claude", stop("C1\n[ACCORD]", "k1"), now=1.0)
        with self.db() as db:
            number, = db.execute("SELECT id FROM replies WHERE body = 'C1\n[ACCORD]'").fetchone()
            state.draft(db, state.Relay(number, "claude", "codex", "C1\n[ACCORD]"))  # Ctrl-b r
        question = f"Que signifie le numéro dans [Réponse de Claude #{number}] ?"  # le brouillon est remplacé
        auto.handle_event(self.state, "codex", taken(question, "t5"), now=2.0)
        auto.handle_event(self.state, "codex", done("R5\n[ACCORD]", "t5", question), now=3.0)
        self.assertNotIn("accord", auto.toggle(self.state, now=4.0))
        with self.db() as db:
            # Tâche 17 : une vraie question tapée à la place du brouillon le remplace ; le contrôle contre un
            # faux accord ci-dessus reste.
            self.assertEqual(db.execute("SELECT status FROM replies WHERE id = ?", (number,)).fetchone(),
                             (state.REPLACED,))

    def test_late_end_of_an_older_turn_does_not_divert_the_next_relay(self):
        self.started()
        with self.db() as db:
            rules.toggle(db, 0.5, True)  # auto coupé
        on_b = {"session_id": "fil-B"}
        auto.handle_event(self.state, "codex", taken("Question sur A", "tA"), now=1.0)
        auto.handle_event(self.state, "codex", dict(taken("Ailleurs", "tB1"), **on_b), now=1.1)
        auto.handle_event(self.state, "codex", dict(done("RB", "tB1", "Ailleurs"), **{"thread-id": "fil-B"}), now=1.2)
        auto.handle_event(self.state, "codex", dict(taken("Encore", "tB2"), **on_b), now=1.3)
        auto.handle_event(self.state, "codex", {"hook_event_name": "PermissionRequest", "session_id": "fil-B",
                                                "turn_id": "tB2"}, now=1.4)
        auto.handle_event(self.state, "codex", done("RA", "tA", "Question sur A"), now=1.5)  # fin tardive de A
        auto.handle_event(self.state, "claude", stop("C1", "k1"), now=2.0)
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            self.assertEqual(auto.toggle(self.state, now=3.0), "AUTO 1/8 · Codex : autorisation demandée")
            self.assertEqual(send.call_args.args[0], "fil-B")

    def test_activation_refused_by_a_stale_pane_check_does_not_overwrite_later_presses(self):
        self.started()
        with self.db() as db:
            rules.toggle(db, 0.5, True)  # auto coupé
        checks = itertools.count()

        def checking(folder):
            if next(checks) == 0:  # l'appui 1 lit les panneaux ; pendant ce temps, ON puis OFF
                auto.toggle(self.state, now=2.1)
                auto.toggle(self.state, now=2.2)
                return {"claude": True, "codex": False}
            return {"claude": True, "codex": True}

        with patch("samdario.auto.alive", side_effect=checking), \
                patch("samdario.auto.queue_send", return_value="q1"), \
                patch("samdario.auto.queue_cancel", return_value=True):
            self.assertEqual(auto.toggle(self.state, now=2.0), "OFF")

    def test_refusal_after_off_leaves_the_auto_off(self):
        self.started()

        def refused(thread, text, cwd):
            auto.toggle(self.state, now=1.5)  # OFF pendant `codex queue`
            raise RuntimeError("refus de codex queue")

        with patch("samdario.auto.queue_send", side_effect=refused):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=2.0), "OFF")

    def test_nothing_is_retried_once_the_auto_is_off_again(self):
        def refused():
            raise RuntimeError("refus de codex queue")

        for name, first_send in (("refus", refused), ("retrait", lambda: "q1")):
            with self.subTest(name):
                self.setUp()
                sending = self.restarted_during_the_send(first_send, presses=3)  # OFF, ON, OFF
                with patch("samdario.auto.queue_send", side_effect=sending) as send, \
                        patch("samdario.auto.queue_cancel", return_value=True):
                    auto.toggle(self.state, now=2.0)
                    self.assertEqual(send.call_count, 1)
                self.assertEqual(auto.status(self.state, now=3.0), "OFF")

    def test_withdrawal_finishing_after_a_new_activation_retries_once(self):
        ids = iter(["q1", "q2"])
        with patch("samdario.auto.queue_send", side_effect=self.restarted_during_the_send(lambda: next(ids))) as send, \
                patch("samdario.auto.queue_cancel", return_value=True) as cancel:
            auto.toggle(self.state, now=2.0)
            cancel.assert_called_once_with(THREAD, "q1", "")  # la copie coupée est retirée
            self.assertEqual(send.call_count, 2)  # puis la réponse repart sous la nouvelle activation
        self.assertEqual(auto.status(self.state, now=3.0), "AUTO 1/8")

    def test_unreadable_output_after_codex_confirmed_does_not_pause(self):
        self.started()

        def sending(thread, text, cwd):
            auto.handle_event(self.state, "codex", taken(text, "t9"), now=1.2)
            raise auto.UncertainSend("sortie illisible")

        with patch("samdario.auto.queue_send", side_effect=sending):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        self.assertEqual(auto.status(self.state, now=2.0), "AUTO 1/8")

    def test_unreadable_output_leaves_a_visible_choice(self):
        self.started()
        with patch("samdario.auto.queue_send", side_effect=auto.UncertainSend("sortie illisible")):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM replies WHERE body = 'C1'").fetchone(), (state.UNCERTAIN,))
        self.assertEqual(auto.status(self.state, now=2.0), "PAUSE — remise incertaine")
        self.assertEqual(auto.toggle(self.state, now=3.0), "PAUSE — remise incertaine")  # reprise immédiate refusée
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0") as mux, patch("samdario.auto.queue_cancel") as cancel:
            self.assertEqual(samdario.relay(self.state, "%1"), "Remise incertaine. Ctrl-b R pour la recoller.")
            mux.assert_called_once()  # aucun collage sans demande explicite
            # Aucun identifiant : rien à retirer, et la copie peut encore partir. Le collage avertit.
            self.assertIn("pas confirmé", samdario.relay(self.state, "%1", force=True))
            cancel.assert_not_called()

    def test_restart_whose_first_send_is_refused_keeps_the_counter(self):
        self.started()
        with self.db() as db:
            rules.toggle(db, 0.5, True)  # auto coupé
            state.put(db, count=3)  # trois relais déjà comptés
        auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)  # une réponse attend son relais
        with patch("samdario.auto.queue_send", side_effect=RuntimeError("refus de codex queue")):
            self.assertEqual(auto.toggle(self.state, now=2.0), "PAUSE — suivi perdu")
        with self.db() as db:
            self.assertEqual(state.get(db, "count"), "3")


if __name__ == "__main__":
    unittest.main()
