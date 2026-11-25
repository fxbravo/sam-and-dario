"""Tâche 8 : l'auto par défaut. Démarrage au lancement, relance par une demande tapée, collage chez Claude."""

import json
import unittest
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import state as state
from tests.test_auto import LoopBase, clock, done, failure, stop, taken, typed

AGREED = "PAUSE — accord trouvé"
LIMIT = "PAUSE — limite de relais atteinte"
NO_WAIT = lambda seconds: None  # noqa: E731


class DefaultAutoTest(LoopBase):
    def setUp(self):
        super().setUp()
        sending = patch("samdario.auto.queue_send", return_value="q1")
        self.send = sending.start()
        self.addCleanup(sending.stop)

    def get(self, key):
        with self.db() as db:
            return state.get(db, key)

    def status_of(self, body):
        with self.db() as db:
            return db.execute("SELECT status FROM replies WHERE body = ?", (body,)).fetchone()[0]

    def pending_start(self):
        with self.db() as db:
            state.put(db, autostart=1)

    def first_exchange(self):
        """Lancement, Ctrl-b a, R1 remise à Claude par son réveil, puis réponse C2 de Claude. Renvoie l'attente."""
        first = self.launch()
        auto.toggle(self.state, now=3.0)
        woke = auto.wait(self.state, first, clock=lambda: 4.0, sleep=NO_WAIT)
        auto.handle_event(self.state, "claude", typed('Stop hook blocking error from command "Stop": ' + woke, "k2"),
                          now=4.1)
        return woke

    def agreed(self):
        """Jusqu'à `PAUSE — accord trouvé`. Renvoie l'attente de Claude, encore valide."""
        self.first_exchange()
        second = auto.handle_event(self.state, "claude", stop("C2\n[ACCORD]", "k2"), now=5.0)
        relayed = self.send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(relayed, "t2"), now=6.0)
        auto.handle_event(self.state, "codex", done("R2\n[ACCORD]", "t2", relayed), now=7.0)
        self.assertEqual(auto.status(self.state, now=8.0), AGREED)
        return second

    def limited(self):
        """Jusqu'à `PAUSE — limite de relais atteinte`, avec un plafond d'un relais. Renvoie l'attente de Claude."""
        with self.db() as db:
            state.put(db, max=1)
        self.first_exchange()
        second = auto.handle_event(self.state, "claude", stop("C2", "k2"), now=5.0)
        self.assertEqual(auto.status(self.state, now=6.0), LIMIT)
        return second

    # --- Lancement

    def test_launch_starts_the_auto_once_both_first_replies_are_in(self):
        self.pending_start()
        self.assertEqual(auto.status(self.state, now=0.1), "AUTO — démarre après les deux réponses")
        first = self.launch()
        self.assertEqual((self.get("auto"), self.get("run")), ("on", "1"))
        self.assertEqual(auto.wait(self.state, first, clock=lambda: 4.0, sleep=NO_WAIT), "[Réponse de Codex #2]\n\nR1")

    def test_ctrl_b_a_before_the_first_replies_cancels_the_automatic_start(self):
        self.pending_start()
        self.assertEqual(auto.toggle(self.state, now=0.1), "OFF")
        self.launch()
        self.assertEqual(self.get("auto"), "off")

    def test_untrusted_hooks_at_launch_pause_the_auto(self):
        self.pending_start()
        with patch("samdario.auto.codex_hooks_trusted", return_value=False):
            self.launch()
        self.assertEqual(auto.status(self.state, now=3.0), "PAUSE — hooks Codex à valider dans /hooks")

    # --- Relance par une demande tapée

    def test_request_typed_to_claude_after_an_agreement_restarts_the_loop(self):
        self.agreed()
        auto.handle_event(self.state, "claude", typed("Nouvelle demande", "k9"), now=9.0)
        auto.handle_event(self.state, "claude", stop("C9", "k9"), now=10.0)
        self.assertEqual((self.get("auto"), self.get("count")), ("on", "1"))
        relayed = self.send.call_args.args[1]
        self.assertTrue(relayed.startswith("[Réponse de Claude #5]"))
        self.assertIn("Nouvelle demande", relayed)

    def test_request_typed_to_codex_after_the_limit_restarts_the_loop(self):
        second = self.limited()
        auto.handle_event(self.state, "codex", taken("Question", "t9"), now=9.0)
        auto.handle_event(self.state, "codex", done("R9", "t9", "Question"), now=10.0)
        self.assertEqual(self.get("auto"), "on")
        woke = auto.wait(self.state, second, clock=lambda: 11.0, sleep=NO_WAIT)
        self.assertTrue(woke.startswith("[Réponse de Codex #4]"))

    def test_answer_to_a_wake_of_claude_is_not_a_typed_request(self):
        self.agreed()  # C2 répond au réveil de Claude (k2), qui lui a remis R1
        with self.db() as db:
            woken = state.Event("claude", "reply", "s", None, "C2\n[ACCORD]", "", "k2")
            self.assertFalse(auto.rules.answers_typed_request(db, woken))

    def test_relay_or_common_request_does_not_restart(self):
        self.agreed()
        with self.db() as db:  # Ctrl-b r chez Claude, puis Entrée sans rien ajouter
            pasted = state.draft(db, state.next_relay(db))
        auto.handle_event(self.state, "claude", typed(pasted, "k8"), now=9.0)
        auto.handle_event(self.state, "claude", stop("C8", "k8"), now=10.0)
        self.assertEqual(self.get("auto"), "pause")
        with patch("samdario.auto.paste_draft"):
            auto.broadcast(self.state, "Q2", now=11.0)  # Ctrl-b b
        with self.db() as db:
            common = state.common(int(state.get(db, "round_seq")), "Q2")
        auto.handle_event(self.state, "codex", taken(common, "t7"), now=12.0)
        auto.handle_event(self.state, "codex", done("R7", "t7", common), now=13.0)
        self.assertEqual(self.get("auto"), "pause")

    def test_late_end_of_a_request_typed_before_the_last_relay_does_not_restart(self):
        first = self.launch()
        auto.handle_event(self.state, "codex", taken("Demande ancienne", "t5"), now=2.5)
        auto.toggle(self.state, now=3.0)
        self.agreed_after_launch(first)
        auto.handle_event(self.state, "codex", done("Fin tardive", "t5", "Demande ancienne"), now=9.0)
        self.assertEqual(self.get("auto"), "pause")

    def agreed_after_launch(self, first):
        woke = auto.wait(self.state, first, clock=lambda: 4.0, sleep=NO_WAIT)
        auto.handle_event(self.state, "claude", typed('Stop hook blocking error from command "Stop": ' + woke, "k2"),
                          now=4.1)
        auto.handle_event(self.state, "claude", stop("C2\n[ACCORD]", "k2"), now=5.0)
        relayed = self.send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(relayed, "t2"), now=6.0)
        auto.handle_event(self.state, "codex", done("R2\n[ACCORD]", "t2", relayed), now=7.0)
        self.assertEqual(auto.status(self.state, now=8.0), AGREED)

    def test_typed_request_after_off_restarts_nothing(self):
        self.launch()
        auto.toggle(self.state, now=3.0)
        self.assertEqual(auto.toggle(self.state, now=3.5), "OFF")
        auto.handle_event(self.state, "claude", typed("Demande", "k9"), now=4.0)
        auto.handle_event(self.state, "claude", stop("C9", "k9"), now=5.0)
        self.assertEqual(auto.status(self.state, now=6.0), "OFF")

    def test_risky_pause_is_not_lifted_by_a_typed_request(self):
        self.started()
        with patch("samdario.auto.queue_send", side_effect=auto.UncertainSend("sortie illisible")):
            auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        auto.handle_event(self.state, "claude", typed("Demande", "k2"), now=2.0)
        auto.handle_event(self.state, "claude", stop("C2", "k2"), now=3.0)
        self.assertEqual(auto.status(self.state, now=4.0), "PAUSE — remise incertaine")

    def test_two_typed_answers_in_a_row_never_switch_the_auto_off(self):
        self.agreed()
        auto.handle_event(self.state, "claude", typed("Demande à Claude", "k9"), now=9.0)
        auto.handle_event(self.state, "claude", stop("C9", "k9"), now=10.0)
        auto.handle_event(self.state, "codex", taken("Demande à Codex", "t9"), now=11.0)
        auto.handle_event(self.state, "codex", done("R9", "t9", "Demande à Codex"), now=12.0)
        self.assertEqual(self.get("auto"), "on")
        auto.resume(self.state, now=13.0)  # une relance de trop ne coupe jamais
        self.assertEqual(self.get("auto"), "on")

    # --- Collage chez Claude quand son attente n'est plus valide

    def idle_claude_without_wait(self):
        """Lancement, Ctrl-b a, OFF (l'attente de Claude est annulée), puis Ctrl-b a : R1 doit aller à Claude."""
        self.launch()
        auto.toggle(self.state, now=3.0)
        auto.toggle(self.state, now=3.5)

    def test_relay_to_an_idle_claude_without_valid_wait_is_pasted_once(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft") as paste:
            self.assertEqual(auto.toggle(self.state, now=4.0), "AUTO 1/8 · Entrée chez Claude")
            self.assertEqual(auto.status(self.state, now=5.0), "AUTO 1/8 · Entrée chez Claude")
            paste.assert_called_once()
        folder, agent, text = paste.call_args.args
        self.assertEqual(agent, "claude")
        self.assertTrue(text.startswith("[Réponse de Codex #2]"))
        with self.db() as db:  # une nouvelle attente de Claude ne remet pas ce relais une seconde fois
            generation = auto.rules.new_waiter(db, 5.5)
        self.assertIsNone(auto.wait(self.state, generation, clock=clock(), sleep=NO_WAIT))
        auto.handle_event(self.state, "claude", typed(state.paste_text(text), "k2"), now=6.0)  # Entrée
        self.assertEqual(self.status_of("R1"), state.SENT)
        self.assertEqual(auto.status(self.state, now=7.0), "AUTO 1/8")

    def test_text_typed_around_a_pasted_relay_travels_as_an_addition(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft") as paste:
            auto.toggle(self.state, now=4.0)
        text = paste.call_args.args[2]
        # Forme relevée sur Claude Code 2.1.329 : texte tapé, puis le collage dans sa balise, puis Entrée.
        submitted = f'Texte à moitié tapé. \n\n<pasted_content id="d1fa">\n{text}\n\n</pasted_content id="d1fa">'
        auto.handle_event(self.state, "claude", typed(submitted, "k2"), now=6.0)
        self.assertEqual(self.status_of("R1"), state.SENT)
        with self.db() as db:
            kinds = db.execute("SELECT kind, text FROM events WHERE request = 'k2' ORDER BY id").fetchall()
        self.assertEqual(kinds, [("relay", submitted), ("submitted", "Texte à moitié tapé. ")])

    def test_busy_claude_gets_the_relay_by_its_next_wake(self):
        self.launch()
        auto.toggle(self.state, now=3.0)
        with patch("samdario.auto.paste_draft") as paste:
            auto.handle_event(self.state, "claude", typed("Demande", "k5"), now=4.0)  # Claude travaille
            self.assertEqual(auto.status(self.state, now=5.0), "AUTO 0/8 · relais à Claude après son tour")
            waiter = auto.handle_event(self.state, "claude", stop("C5", "k5"), now=6.0)
            paste.assert_not_called()
        woke = auto.wait(self.state, waiter, clock=lambda: 7.0, sleep=NO_WAIT)
        self.assertTrue(woke.startswith("[Réponse de Codex #2]"))

    def test_erased_pasted_relay_is_pasted_again_by_ctrl_b_shift_r(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft") as paste:
            auto.toggle(self.state, now=4.0)
        text = paste.call_args.args[2]
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertEqual(samdario.relay(self.state, "%0"), "Déjà collée. Ctrl-b R pour la recoller.")
            self.assertEqual(samdario.relay(self.state, "%0", force=True),
                             "Réponse de Codex recollée. Entrée pour envoyer.")
        loaded = next(call.kwargs["data"].decode() for call in mux.call_args_list if call.args[1] == "load-buffer")
        self.assertTrue(loaded.startswith("[Réponse de Codex #2]"))
        self.assertEqual(self.get("count"), "1")  # recoller ne compte pas un second relais
        auto.handle_event(self.state, "claude", typed(state.paste_text(text), "k2"), now=6.0)  # Entrée
        self.assertEqual(self.status_of("R1"), state.SENT)

    def test_off_turns_a_pasted_relay_into_a_draft(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft"):
            auto.toggle(self.state, now=4.0)
            self.assertEqual(auto.toggle(self.state, now=5.0), "OFF")
            self.assertEqual(self.status_of("R1"), state.DRAFTED)
            self.assertEqual(auto.toggle(self.state, now=6.0),
                             "PAUSE — brouillon en attente chez Claude : Entrée, ou Ctrl-b R pour le recoller")

    def test_failed_paste_pauses_for_a_manual_relay(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft", side_effect=RuntimeError("panneau indisponible")):
            self.assertEqual(auto.toggle(self.state, now=4.0), "PAUSE — relais manuel requis vers Claude")
        self.assertEqual((self.status_of("R1"), self.get("count")), (state.NEW, "0"))

    # --- Relecture de Codex (tâche 8)

    def common_sent(self):
        with self.db() as db:
            return state.common(state.share(db, {"claude": "Question", "codex": "Question"}), "Question")

    def test_press_during_the_engaged_start_keeps_the_auto_off(self):
        self.pending_start()
        sent = self.common_sent()
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)

        def press_during_the_check(folder):  # Ctrl-b a pendant la vérification des hooks du démarrage
            self.assertEqual(auto.toggle(self.state, now=2.5), "OFF")
            return True

        with patch("samdario.auto.codex_hooks_trusted", side_effect=press_during_the_check):
            auto.handle_event(self.state, "codex", done("R1", "t1", sent), now=2.0)
        self.assertEqual((self.get("auto"), auto.status(self.state, now=3.0)), ("off", "OFF"))

    def test_failed_first_turn_of_claude_suspends_the_start_until_the_round_is_abandoned(self):
        self.pending_start()
        sent = self.common_sent()
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", failure("k1"), now=1.0)
        auto.handle_event(self.state, "codex", done("R1", "t1", sent), now=2.0)
        self.assertIn("démarrage suspendu : Claude n'a pas répondu à la demande commune", auto.status(self.state, now=3.0))
        auto.handle_event(self.state, "claude", typed("Encore", "k2"), now=4.0)
        auto.handle_event(self.state, "claude", stop("C2", "k2"), now=5.0)
        self.assertIn("démarrage suspendu", auto.status(self.state, now=6.0))
        self.assertEqual(auto.broadcast(self.state, "", now=7.0), "Tour de table abandonné.")
        auto.handle_event(self.state, "claude", typed("Et maintenant", "k3"), now=8.0)
        auto.handle_event(self.state, "claude", stop("C3", "k3"), now=9.0)
        self.assertEqual(self.get("auto"), "on")

    def test_common_request_waits_while_a_relay_is_pasted_at_claude(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft") as paste:
            auto.toggle(self.state, now=4.0)
            self.assertIn("Collage en attente chez Claude", auto.broadcast(self.state, "Q2", now=5.0))
            bar = auto.status(self.state, now=6.0)
            self.assertEqual(paste.call_count, 1)  # le relais seul : pas deux brouillons dans le même champ
        self.assertIn("Entrée chez Claude", bar)
        self.assertIn("tour de table : collage en attente chez Claude", bar)
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0"):  # brouillon effacé : Ctrl-b R reste possible pendant le tour
            self.assertEqual(samdario.relay(self.state, "%0", force=True),
                             "Réponse de Codex recollée. Entrée pour envoyer.")

    def test_ctrl_b_shift_r_pastes_the_awaited_relay_again_after_a_newer_codex_reply(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft"):
            auto.toggle(self.state, now=4.0)
        auto.handle_event(self.state, "codex", taken("Encore", "t3"), now=5.0)
        auto.handle_event(self.state, "codex", done("R3", "t3", "Encore"), now=6.0)
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertEqual(samdario.relay(self.state, "%0"), "Déjà collée. Ctrl-b R pour la recoller.")
            samdario.relay(self.state, "%0", force=True)
        loaded = next(call.kwargs["data"].decode() for call in mux.call_args_list if call.args[1] == "load-buffer")
        self.assertTrue(loaded.startswith("[Réponse de Codex #2]"))

    def test_late_reply_of_a_hidden_codex_thread_does_not_restart(self):
        second = self.agreed()
        auto.handle_event(self.state, "codex", dict(taken("Sur B", "b1"), session_id="fil-B"), now=9.0)
        auto.handle_event(self.state, "codex", taken("Retour sur A", "a9"), now=10.0)
        auto.handle_event(self.state, "codex", dict(done("Réponse de B", "b1", "Sur B"), **{"thread-id": "fil-B"}),
                          now=11.0)
        self.assertEqual(self.get("auto"), "pause")
        auto.handle_event(self.state, "codex", done("Réponse de A", "a9", "Retour sur A"), now=12.0)
        self.assertEqual(self.get("auto"), "on")
        woke = auto.wait(self.state, second, clock=lambda: 13.0, sleep=NO_WAIT)
        self.assertIn("Réponse de A", woke)

    def test_request_typed_during_the_last_relay_does_not_open_a_new_counter(self):
        with self.db() as db:
            state.put(db, max=1)
        self.first_exchange()  # AUTO 1/1 : R1 vient d'être remise à Claude
        auto.handle_event(self.state, "codex", taken("Pendant l'échange", "t8"), now=4.5)
        auto.handle_event(self.state, "claude", stop("C2", "k2"), now=5.0)
        self.assertEqual(auto.status(self.state, now=5.5), LIMIT)
        auto.handle_event(self.state, "codex", done("R8", "t8", "Pendant l'échange"), now=6.0)
        self.assertEqual(auto.status(self.state, now=7.0), LIMIT)

    def test_late_failure_of_an_old_turn_does_not_make_a_busy_claude_idle(self):
        self.launch()
        auto.toggle(self.state, now=3.0)
        auto.handle_event(self.state, "claude", typed("A", "ka"), now=4.0)
        auto.handle_event(self.state, "claude", typed("B", "kb"), now=5.0)
        auto.handle_event(self.state, "claude", stop("CB", "kb"), now=6.0)
        auto.handle_event(self.state, "claude", typed("C", "kc"), now=7.0)  # Claude travaille
        with patch("samdario.auto.paste_draft") as paste:
            auto.handle_event(self.state, "claude", failure("ka"), now=8.0)  # échec tardif du premier tour
            self.assertIn("relais à Claude après son tour", auto.status(self.state, now=9.0))
            paste.assert_not_called()

    def test_interrupted_paste_is_never_pasted_twice(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                auto.toggle(self.state, now=4.0)  # processus interrompu juste après le collage
        with patch("samdario.auto.paste_draft") as paste:
            auto.status(self.state, now=5.0)
            self.assertEqual(auto.status(self.state, now=4.0 + auto.rules.PASTE_SECONDS + 1),
                             "PAUSE — remise incertaine")
            paste.assert_not_called()
        self.assertEqual(self.get("count"), "1")


    # --- Seconde relecture de Codex (tâche 8)

    def test_start_waits_for_the_claude_reply_that_lifts_the_failure(self):
        self.pending_start()
        sent = self.common_sent()
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", failure("k1"), now=1.0)
        self.assertEqual(auto.broadcast(self.state, "", now=1.5), "Tour de table abandonné.")
        auto.handle_event(self.state, "codex", done("R1", "t1", sent), now=2.0)  # réponse tardive à la nº 1
        self.assertEqual(self.get("auto"), "off")
        auto.handle_event(self.state, "claude", typed("Nouvelle demande", "k2"), now=3.0)
        auto.handle_event(self.state, "claude", stop("C2", "k2"), now=4.0)
        self.assertEqual((self.get("auto"), self.get("run")), ("on", "1"))

    def started_launch_with(self, **patches):
        """Lancement en attente : Claude a répondu, la réponse de Codex déclenche le démarrage sous `patches`."""
        self.pending_start()
        sent = self.common_sent()
        auto.handle_event(self.state, "claude", typed(sent, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
        with patch.multiple("samdario.auto", **patches):
            auto.handle_event(self.state, "codex", done("R1", "t1", sent), now=2.0)

    def test_failed_panel_check_never_overwrites_an_off_pressed_meanwhile(self):
        def press_then_report_a_closed_panel(folder):
            auto.toggle(self.state, now=2.5)  # Ctrl-b a pendant la vérification des panneaux
            return {"claude": True, "codex": False}

        self.started_launch_with(alive=press_then_report_a_closed_panel)
        self.assertEqual(auto.status(self.state, now=3.0), "OFF")

    def test_start_interrupted_during_the_hooks_check_is_resumed_by_the_status_bar(self):
        with self.assertRaises(KeyboardInterrupt):
            self.started_launch_with(codex_hooks_trusted=lambda folder: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(self.get("auto"), "off")
        auto.status(self.state, now=3.0)  # tentative interrompue trop récente : pas de seconde vérification
        self.assertEqual(self.get("auto"), "off")
        auto.status(self.state, now=2.0 + auto.rules.LAUNCH_RETRY_SECONDS + 1)
        self.assertEqual((self.get("auto"), self.get("run")), ("on", "1"))

    def test_off_with_a_pasted_relay_keeps_the_common_request_out_of_the_field(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft") as paste:
            auto.toggle(self.state, now=4.0)
            auto.broadcast(self.state, "Q2", now=5.0)
            self.assertEqual(auto.toggle(self.state, now=6.0), "OFF · tour de table : collage en attente chez Claude")
            auto.status(self.state, now=7.0)
            self.assertEqual(paste.call_count, 1)  # le relais seul, même après OFF

    def uncertain_paste(self):
        """Collage effectué, puis processus interrompu avant de le noter : la remise devient incertaine."""
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                auto.toggle(self.state, now=4.0)
        with self.db() as db:
            text, = db.execute("SELECT pasted FROM replies WHERE body = 'R1'").fetchone()
        self.assertEqual(auto.status(self.state, now=4.0 + auto.rules.PASTE_SECONDS + 1), "PAUSE — remise incertaine")
        return text

    def test_uncertain_paste_still_owns_the_field_and_its_recall(self):
        text = self.uncertain_paste()
        with patch("samdario.auto.paste_draft") as paste:
            auto.broadcast(self.state, "Q2", now=20.0)
            auto.status(self.state, now=21.0)
            paste.assert_not_called()  # la question attend : le champ porte peut-être encore le relais
        auto.handle_event(self.state, "codex", taken("Encore", "t3"), now=22.0)
        auto.handle_event(self.state, "codex", done("R3", "t3", "Encore"), now=23.0)
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertEqual(samdario.relay(self.state, "%0", force=True),
                             "Réponse de Codex recollée. Entrée pour envoyer.")
        loaded = next(call.kwargs["data"].decode() for call in mux.call_args_list if call.args[1] == "load-buffer")
        self.assertEqual(loaded, text + "\n\n")

    def test_text_typed_around_an_uncertain_paste_still_travels_once(self):
        text = self.uncertain_paste()
        submitted = f'Ma phrase. \n\n<pasted_content id="d1fa">\n{text}\n\n</pasted_content id="d1fa">'
        auto.handle_event(self.state, "claude", typed(submitted, "k2"), now=20.0)
        self.assertEqual(self.status_of("R1"), state.SENT)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT text FROM events WHERE request = 'k2' AND kind = 'submitted'")
                             .fetchall(), [("Ma phrase. ",)])

    def reserved_paste(self):
        """Auto actif, réservation du collage de R1 enregistrée, effet tmux pas encore fait."""
        self.idle_claude_without_wait()
        with self.db() as db:
            state.put(db, auto="on")
            relay = auto.rules.paste_due(db, 8.0)
            text = state.compose(db, relay, for_paste=True)
            claim, seq = auto.rules.reserve_paste(db, relay, text, 8.0)
        return relay._replace(claim=claim), text, seq

    def test_off_before_the_paste_effect_cancels_it_and_releases_the_wait(self):
        relay, text, seq = self.reserved_paste()
        self.assertEqual(auto.toggle(self.state, now=8.5), "OFF")
        with patch("samdario.auto.paste_draft") as paste:
            self.assertFalse(auto.paste_reserved(self.state, relay, text, seq))
            paste.assert_not_called()
        self.assertEqual(self.status_of("R1"), state.NEW)  # jamais collée : rien n'attend dans le champ
        self.assertEqual((self.get("awaiting"), self.get("count")), ("", "0"))
        with patch("samdario.auto.paste_draft") as paste:
            self.assertEqual(auto.toggle(self.state, now=9.0), "AUTO 1/8 · Entrée chez Claude")
            paste.assert_called_once()

    def test_ctrl_b_shift_r_during_the_cut_keeps_its_draft_against_the_late_paste(self):
        relay, text, seq = self.reserved_paste()
        auto.toggle(self.state, now=8.5)  # OFF
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0"):
            samdario.relay(self.state, "%0", force=True)  # Ctrl-b R pendant la coupure
        with patch("samdario.auto.paste_draft") as paste:
            self.assertFalse(auto.paste_reserved(self.state, relay, text, seq))
            auto.broadcast(self.state, "Q2", now=9.0)
            auto.status(self.state, now=9.5)
            paste.assert_not_called()  # ni second relais ni question derrière le brouillon recollé
        self.assertEqual(self.status_of("R1"), state.DRAFTED)

    def test_claude_starting_a_turn_before_the_paste_effect_defers_the_relay(self):
        relay, text, seq = self.reserved_paste()
        auto.handle_event(self.state, "claude", typed("Demande", "k9"), now=8.5)  # Claude se met au travail
        with patch("samdario.auto.paste_draft") as paste:
            self.assertFalse(auto.paste_reserved(self.state, relay, text, seq))
            paste.assert_not_called()
        self.assertEqual((self.status_of("R1"), self.get("awaiting")), (state.NEW, ""))
        waiter = auto.handle_event(self.state, "claude", stop("C9", "k9"), now=9.0)
        woke = auto.wait(self.state, waiter, clock=lambda: 9.5, sleep=NO_WAIT)
        self.assertTrue(woke.startswith("[Réponse de Codex #2]"))

    def test_manual_relay_after_off_follows_the_latest_reply_once_the_field_is_emptied(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft"):
            auto.toggle(self.state, now=4.0)  # R1 collée chez Claude
        auto.toggle(self.state, now=5.0)  # OFF
        auto.handle_event(self.state, "claude", typed("Autre chose", "k9"), now=6.0)  # brouillon effacé
        auto.handle_event(self.state, "claude", stop("C9", "k9"), now=7.0)
        auto.handle_event(self.state, "codex", taken("Encore", "t4"), now=8.0)
        auto.handle_event(self.state, "codex", done("R4", "t4", "Encore"), now=9.0)
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertEqual(samdario.relay(self.state, "%0"), "Réponse de Codex collée. Entrée pour envoyer.")
        loaded = next(call.kwargs["data"].decode() for call in mux.call_args_list if call.args[1] == "load-buffer")
        self.assertTrue(loaded.startswith("[Réponse de Codex #4]"))

    def test_ctrl_b_shift_r_pastes_a_waiting_common_request_after_its_field_was_emptied(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft"):
            auto.broadcast(self.state, "Q1", now=4.0)  # collée chez Claude, puis effacée
            self.assertEqual(auto.broadcast(self.state, "", now=5.0), "Tour de table abandonné.")
            self.assertIn("Collage en attente chez Claude", auto.broadcast(self.state, "Q2", now=6.0))
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertEqual(samdario.relay(self.state, "%0", force=True),
                             "Question « pour les deux » collée. Entrée pour l'envoyer.")
        loaded = next(call.kwargs["data"].decode() for call in mux.call_args_list if call.args[1] == "load-buffer")
        with self.db() as db:
            self.assertEqual(loaded, state.common_text(db, 3) + "\n\n")
        self.assertIn("Entrée chez Claude", auto.status(self.state, now=7.0))

    def test_relay_pasted_by_the_previous_version_still_occupies_the_field(self):
        self.idle_claude_without_wait()
        with patch("samdario.auto.paste_draft") as paste:
            auto.toggle(self.state, now=4.0)
            with self.db() as db:  # base de la version précédente : pas de suivi du champ
                db.execute("DELETE FROM meta WHERE key LIKE 'claude_field%'")
            auto.broadcast(self.state, "Q2", now=5.0)
            auto.status(self.state, now=6.0)
            self.assertEqual(paste.call_count, 1)

    def test_start_engaged_by_the_previous_version_still_happens(self):
        self.launch()
        with self.db() as db:
            state.put(db, autostart="go")  # démarrage engagé avant la mise à jour
        auto.status(self.state, now=3.0)
        self.assertEqual(self.get("auto"), "on")

    def test_paste_delay_starts_at_the_actual_reservation(self):
        self.idle_claude_without_wait()
        clock_now = [100.0]

        def slow_hooks_check(folder):
            clock_now[0] += 15.0  # vérification des hooks de 15 secondes
            return True

        real_paste = auto.paste_reserved

        def refresh_then_paste(folder, relay, text, seq):
            auto.status(self.state, now=116.0)  # rafraîchissement de la barre entre réservation et collage
            return real_paste(folder, relay, text, seq)

        with patch("samdario.auto.time.time", side_effect=lambda: clock_now[0]), \
                patch("samdario.auto.codex_hooks_trusted", side_effect=slow_hooks_check), \
                patch("samdario.auto.paste_reserved", side_effect=refresh_then_paste), \
                patch("samdario.auto.paste_draft"):
            auto.toggle(self.state)
            self.assertEqual(auto.status(self.state, now=116.0), "AUTO 1/8 · Entrée chez Claude")

    def test_thread_switch_during_the_restart_check_waits_for_the_new_answer(self):
        second = self.agreed()
        auto.handle_event(self.state, "codex", dict(taken("Sur B", "b1"), session_id="fil-B"), now=9.0)

        def back_on_a_during_the_check(folder):
            auto.handle_event(self.state, "codex", taken("Retour sur A", "a9"), now=10.5)
            return True

        with patch("samdario.auto.codex_hooks_trusted", side_effect=back_on_a_during_the_check):
            auto.handle_event(self.state, "codex",
                              dict(done("Réponse de B", "b1", "Sur B"), **{"thread-id": "fil-B"}), now=10.0)
        self.assertEqual(self.get("auto"), "pause")  # l'ancien accord de A ne part pas
        auto.handle_event(self.state, "codex", done("Réponse de A", "a9", "Retour sur A"), now=12.0)
        woke = auto.wait(self.state, second, clock=lambda: 13.0, sleep=NO_WAIT)
        self.assertIn("Réponse de A", woke)


if __name__ == "__main__":
    unittest.main()
