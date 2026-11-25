import sqlite3
import unittest

from samdario import rules as rules
from samdario import state as state
from tests.test_state import Base


class RulesTest(Base):
    def on(self, now=0.0):
        with self.db() as db:
            rules.toggle(db, now, True)

    def waiting(self):
        # Ouverture, attente créée chez Claude, auto actif : R0 est à relayer vers Claude.
        _, codex_reply = self.opening()
        with self.db() as db:
            waiter = rules.new_waiter(db, 0.0)
        self.on(1.0)
        return codex_reply, waiter

    def reserved(self):
        # Ouverture, premier relais répondu, auto actif : la réponse C1 de Claude est réservée pour Codex.
        claude_reply = self.reply("claude", "C1", self.exchange())
        self.on()
        with self.db() as db:
            relay = rules.claim(db, "codex", 1.0)
        self.assertEqual(relay.reply_id, claude_reply)
        return relay

    def in_queue(self, db, relay, queue_id="q1"):
        # Comme le retour de `codex queue` pendant que l'auto est actif : la réservation garde l'identifiant.
        state.mark(db, relay.reply_id, state.QUEUED, queue_id)
        db.execute("UPDATE claims SET queue_id = ? WHERE id = ?", (queue_id, relay.claim))

    def status_of(self, reply_id):
        with self.db() as db:
            return db.execute("SELECT status FROM replies WHERE id = ?", (reply_id,)).fetchone()[0]

    def test_toggle_needs_both_codex_hooks_trusted(self):
        with self.db() as db:
            self.assertEqual(rules.toggle(db, 0.0, False), [])
            self.assertEqual(rules.status_line(db, 0.0), "PAUSE — hooks Codex à valider dans /hooks")

    def test_only_the_current_waiter_delivers_and_only_once(self):
        _, codex_reply = self.opening()
        with self.db() as db:
            old = rules.new_waiter(db, 0.0)
            current = rules.new_waiter(db, 1.0)
        self.on(2.0)
        with self.db() as db:
            self.assertIsNone(rules.claim(db, "claude", 3.0, old))
            relay = rules.claim(db, "claude", 3.0, current)
            self.assertEqual(relay[:4], (codex_reply, "codex", "claude", "R0"))
            self.assertTrue(relay.claim)  # la réservation porte son identifiant
            self.assertIsNone(rules.claim(db, "claude", 3.0, current))
            self.assertEqual(rules.status_line(db, 3.0), "AUTO 1/8")

    def test_unconfirmed_delivery_to_claude_pauses(self):
        codex_reply, waiter = self.waiting()
        with self.db() as db:
            rules.claim(db, "claude", 2.0, waiter)
            self.assertEqual(rules.status_line(db, 2.0 + rules.CONFIRM_SECONDS), "AUTO 1/8")
            self.assertEqual(rules.status_line(db, 2.5 + rules.CONFIRM_SECONDS), "PAUSE — remise incertaine")
        self.assertEqual(self.status_of(codex_reply), state.UNCERTAIN)

    def test_confirmed_delivery_to_claude_does_not_pause(self):
        _, waiter = self.waiting()
        with self.db() as db:
            woke = state.compose(db, rules.claim(db, "claude", 2.0, waiter))
        self.submit("claude", 'Stop hook blocking error from command "Stop": ' + woke)
        with self.db() as db:
            self.assertEqual(rules.status_line(db, 500.0), "AUTO 1/8")

    def test_unidentified_reply_is_no_agreement_and_pauses_with_the_relay_number(self):
        self.turn("claude", "Question", "C0")
        codex_reply = self.turn("codex", "Question", "R0\n[ACCORD]")
        old = self.submit("claude", "une demande plus ancienne")
        with self.db() as db:
            waiter = rules.new_waiter(db, 0.0)
        self.on(1.0)
        with self.db() as db:
            woke = state.compose(db, rules.claim(db, "claude", 2.0, waiter))
        self.submit("claude", woke, request="")  # remise confirmée sans identifiant
        self.reply("claude", "Ancienne fin de tour\n[ACCORD]", old)
        paused = f"PAUSE — réponse au relais #{codex_reply} non identifiée"
        with self.db() as db:
            self.assertFalse(state.agreed(db))
            self.assertEqual(rules.status_line(db, 3.0), paused)
            self.assertEqual(state.get(db, "count"), "1")  # compté une seule fois
            rules.toggle(db, 4.0, True)  # reprise refusée
            self.assertEqual(rules.status_line(db, 4.0), paused)
            self.assertEqual(state.get(db, "count"), "1")  # pas de remise à zéro sans reprise effective

    def test_submission_to_claude_expires_the_waiter(self):
        _, waiter = self.waiting()
        self.submit("claude", "bonjour")
        with self.db() as db:
            self.assertIsNone(rules.claim(db, "claude", 2.0, waiter))
            # Tâche 8 : Claude travaille, le relais attend son prochain réveil, sans pause ni collage.
            self.assertEqual(rules.status_line(db, 2.0), "AUTO 0/8 · relais à Claude après son tour")
            self.assertIsNone(rules.paste_due(db, 2.0))

    def test_off_expires_the_waiter_and_hands_back_queued_relays(self):
        relay = self.reserved()
        with self.db() as db:
            waiter = rules.new_waiter(db, 0.0)
            self.assertEqual(rules.toggle(db, 1.5, True), [])  # envoi en cours, pas encore d'identifiant de file
        self.on(1.6)
        with self.db() as db:
            self.in_queue(db, relay)
            self.assertEqual(rules.toggle(db, 2.0, True), [(relay, "fil-codex", "q1")])
            self.assertEqual(rules.status_line(db, 2.0), "OFF")
            self.assertTrue(rules.release(db, relay, cancelled=True))
            self.assertEqual(state.get(db, "awaiting"), "")
            self.assertIsNone(rules.claim(db, "claude", 3.0, waiter))

    # --- 1. Une coupure reste acquise à l'envoi en cours, même si l'auto est réactivé aussitôt.
    def test_off_during_a_send_is_remembered_after_a_quick_restart(self):
        relay = self.reserved()
        with self.db() as db:
            rules.toggle(db, 1.5, True)  # OFF pendant `codex queue`
        codex_reply = self.turn("codex", "Une précision ?", "R4")  # une réponse arrive entre-temps
        self.on(1.8)  # réactivé avant le retour de la commande
        with self.db() as db:
            self.assertEqual(rules.status_line(db, 1.9), "AUTO 0/8")
            self.assertFalse(rules.enqueued(db, relay, "q1", 2.0))  # la coupure vaut toujours pour cet envoi
            self.assertTrue(rules.release(db, relay, cancelled=True))
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.NEW)
            waiter = rules.new_waiter(db, 2.0)
            self.assertEqual(rules.claim(db, "claude", 2.1, waiter).reply_id, codex_reply)  # la boucle repart

    def status_of_in(self, db, reply_id):
        return db.execute("SELECT status FROM replies WHERE id = ?", (reply_id,)).fetchone()[0]

    def test_send_that_lands_while_auto_is_on_records_its_queue_id(self):
        relay = self.reserved()
        with self.db() as db:
            self.assertTrue(rules.enqueued(db, relay, "q1", 1.1))
            self.assertEqual(rules.toggle(db, 2.0, True), [(relay, "fil-codex", "q1")])

    # --- 2. Chaque retour s'applique une seule fois à sa réservation ; une réception confirmée fait foi.
    def test_late_failure_cannot_undo_a_confirmed_delivery(self):
        relay = self.reserved()
        with self.db() as db:
            sent = state.compose(db, relay)
        self.submit("codex", sent)  # Codex confirme la réception
        with self.db() as db:
            self.assertFalse(rules.revert(db, relay))  # le retour tardif de la commande ne compte plus
            self.assertTrue(rules.enqueued(db, relay, "q1", 2.0))  # rien à retirer : la remise enregistrée fait foi
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.SENT)
            self.assertEqual((state.get(db, "count"), state.get(db, "awaiting")), ("1", "codex"))
            self.assertIsNone(rules.claim(db, "codex", 2.0))  # pas de seconde réservation du même message

    def test_second_withdrawal_result_is_ignored(self):
        relay = self.reserved()
        with self.db() as db:
            self.in_queue(db, relay)
            rules.toggle(db, 2.0, True)
            self.assertTrue(rules.release(db, relay, cancelled=True))  # retiré
            self.assertFalse(rules.release(db, relay, cancelled=False))  # second retour : sans effet
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.NEW)

    def test_absence_from_the_queue_does_not_prove_the_reception(self):
        # « Plus dans la file » : Codex l'a peut-être prise, ou quelqu'un l'a retirée. Seul son hook le dira.
        relay = self.reserved()
        with self.db() as db:
            self.in_queue(db, relay)
            rules.toggle(db, 2.0, True)
            self.assertTrue(rules.release(db, relay, cancelled=False))
            self.assertEqual((self.status_of_in(db, relay.reply_id), state.get(db, "awaiting"), state.get(db, "count")),
                             (state.UNCERTAIN, "codex", "1"))
            self.assertEqual(db.execute("SELECT state, thread, queue_id FROM claims").fetchone(),
                             (state.UNCERTAIN, "fil-codex", "q1"))  # de quoi retenter le retrait

    # --- 3. L'auto ne réserve jamais une réponse déjà en brouillon chez son destinataire : pas de double envoi.
    def test_auto_never_reserves_a_manual_draft(self):
        claude_reply = self.reply("claude", "C1", self.exchange())
        with self.db() as db:
            pasted = state.draft(db, state.next_relay(db))  # Ctrl-b r chez Codex : brouillon visible
        self.on()  # activation refusée tant que le brouillon attend
        paused = "PAUSE — brouillon en attente chez Codex : Entrée, ou Ctrl-b R pour le recoller"
        with self.db() as db:
            self.assertEqual(rules.status_line(db, 1.0), paused)
            self.assertIsNone(rules.claim(db, "codex", 1.0))
            self.assertEqual(db.execute("SELECT count(*) FROM claims").fetchone(), (0,))
        asked = self.submit("codex", pasted + "\n\nEt les tests ?")  # phrase ajoutée, puis Entrée
        self.assertEqual(self.status_of(claude_reply), state.SENT)
        codex_reply = self.reply("codex", "R1", asked, prompt=pasted + "\n\nEt les tests ?")
        with self.db() as db:
            rules.toggle(db, 2.0, True)
            # Tâche 8 : Claude au repos et sans attente, le relais sera collé chez lui (samdario.auto).
            self.assertEqual(rules.status_line(db, 2.0), "AUTO 0/8")
            relay = state.next_relay(db)
            self.assertEqual(relay.reply_id, codex_reply)
            self.assertEqual(rules.paste_due(db, 2.0), relay)
            self.assertIn("— Demande de l'utilisateur à Codex :\nEt les tests ?", state.compose(db, relay))

    def test_revert_gives_the_reply_back_and_uncounts_it(self):
        relay = self.reserved()
        with self.db() as db:
            self.assertTrue(rules.revert(db, relay))
            self.assertFalse(rules.revert(db, relay))  # une seule fois
            self.assertEqual((state.get(db, "count"), state.get(db, "awaiting")), ("0", ""))
            self.assertEqual(rules.claim(db, "codex", 2.0).reply_id, relay.reply_id)

    # --- 4. Un relais manuel confirmé permet de reprendre après une remise incertaine, dont la trace reste.
    def test_manual_relay_gets_past_an_uncertain_delivery(self):
        codex_reply, waiter = self.waiting()
        with self.db() as db:
            rules.claim(db, "claude", 2.0, waiter)
            self.assertEqual(rules.status_line(db, 40.0), "PAUSE — remise incertaine")
            rules.toggle(db, 41.0, True)  # relancer sans rien faire ne suffit pas
            self.assertEqual(rules.status_line(db, 41.0), "PAUSE — remise incertaine")
            self.assertEqual(state.get(db, "count"), "1")
        newer = self.turn("codex", "Précise", "R3")
        self.reply("claude", "Réponse à R3", self.deliver(newer, "claude"))  # relais manuel confirmé et répondu
        with self.db() as db:
            rules.toggle(db, 50.0, True)
            self.assertEqual(rules.status_line(db, 50.0), "AUTO 0/8")
        self.assertEqual(self.status_of(codex_reply), state.UNCERTAIN)  # la trace reste

    def test_late_confirmation_resolves_an_uncertain_delivery(self):
        codex_reply, waiter = self.waiting()
        with self.db() as db:
            woke = state.compose(db, rules.claim(db, "claude", 2.0, waiter))
            rules.status_line(db, 40.0)  # délai dépassé : remise incertaine
        asked = self.submit("claude", 'Stop hook blocking error from command "Stop": ' + woke)
        self.assertEqual(self.status_of(codex_reply), state.SENT)
        self.reply("claude", "Réponse au relais", asked)
        with self.db() as db:
            rules.toggle(db, 50.0, True)
            self.assertEqual(rules.status_line(db, 50.0), "AUTO 0/8")

    # --- 5. Le décompte suit l'activation qui a compté le relais.
    def test_successful_withdrawal_uncounts_the_relay(self):
        relay = self.reserved()
        with self.db() as db:
            self.in_queue(db, relay)
            rules.toggle(db, 2.0, True)
            rules.release(db, relay, cancelled=True)
            self.assertEqual(state.get(db, "count"), "0")

    def test_late_refusal_from_an_earlier_activation_leaves_the_new_count_alone(self):
        old_relay = self.reserved()  # réservé pendant la première activation, envoi encore en cours
        with self.db() as db:
            rules.toggle(db, 1.5, True)  # OFF
        codex_reply = self.turn("codex", "Une précision ?", "R4")
        self.reply("claude", "C5", self.deliver(codex_reply, "claude"))  # relais manuel, Claude répond
        self.on(3.0)  # seconde activation
        with self.db() as db:
            new_relay = rules.claim(db, "codex", 3.1)
            self.assertEqual(rules.status_line(db, 3.2), "AUTO 1/8")
            self.assertTrue(rules.revert(db, old_relay))  # refus tardif du premier envoi
            self.assertEqual(rules.status_line(db, 3.3), "AUTO 1/8")  # le compteur de la seconde activation ne bouge pas
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "awaiting_reply")),
                             ("codex", str(new_relay.reply_id)))

    # --- 6. Un seul retrait par message en file, même après OFF, ON, OFF.
    def test_only_one_operation_withdraws_a_queued_relay(self):
        relay = self.reserved()
        with self.db() as db:
            self.assertTrue(rules.enqueued(db, relay, "q1", 1.1))
            self.assertEqual(rules.toggle(db, 2.0, True), [(relay, "fil-codex", "q1")])  # première coupure : retrait confié
        self.on(2.1)
        with self.db() as db:
            self.assertEqual(rules.toggle(db, 2.2, True), [])  # seconde coupure : rien à confier de nouveau
            self.assertTrue(rules.release(db, relay, cancelled=True))
            self.assertFalse(rules.release(db, relay, cancelled=False))
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.NEW)

    # --- 7. La confirmation tardive d'un ancien relais met son historique à jour sans déplacer l'attente.
    def test_late_confirmation_of_an_older_relay_keeps_the_current_wait(self):
        codex_reply, waiter = self.waiting()
        with self.db() as db:
            woke = state.compose(db, rules.claim(db, "claude", 2.0, waiter))
            rules.status_line(db, 40.0)  # remise incertaine du premier relais
        newer = self.turn("codex", "Précise", "R3")
        claude_reply = self.reply("claude", "C4", self.deliver(newer, "claude"))  # relais manuel, puis réponse
        asked = self.deliver(claude_reply, "codex")  # Codex confirme avoir reçu C4
        self.submit("claude", 'Stop hook blocking error from command "Stop": ' + woke)  # confirmation tardive
        self.assertEqual(self.status_of(codex_reply), state.SENT)  # l'historique est à jour
        with self.db() as db:
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "awaiting_reply")),
                             ("codex", str(claude_reply)))
        answer = self.reply("codex", "R5", asked)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (answer,)).fetchone(),
                             (claude_reply,))

    def test_late_confirmation_cannot_reopen_a_finished_wait(self):
        codex_reply, waiter = self.waiting()
        with self.db() as db:
            woke = state.compose(db, rules.claim(db, "claude", 2.0, waiter))
            rules.status_line(db, 40.0)  # remise incertaine du premier relais
        newer = self.turn("codex", "Précise", "R3")
        claude_reply = self.reply("claude", "C4", self.deliver(newer, "claude"))
        answer = self.reply("codex", "R5", self.deliver(claude_reply, "codex"))  # l'échange suivant est terminé
        self.submit("claude", 'Stop hook blocking error from command "Stop": ' + woke)  # confirmation tardive
        self.assertEqual(self.status_of(codex_reply), state.SENT)
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting"), "")
            self.assertEqual(state.next_relay(db).reply_id, answer)  # la réponse suivante est prête à partir

    def test_wait_follows_the_order_of_deliveries_not_of_replies(self):
        claude_reply, codex_reply = self.opening()  # Claude a répondu en premier : numéro plus petit
        self.deliver(codex_reply, "claude")  # d'abord la réponse de Codex chez Claude
        asked = self.deliver(claude_reply, "codex")  # puis, volontairement, celle de Claude chez Codex
        with self.db() as db:
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "awaiting_reply")),
                             ("codex", str(claude_reply)))
        answer = self.reply("codex", "R1", asked)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (answer,)).fetchone(),
                             (claude_reply,))

    # --- 8. Recoller une remise incertaine ne lève pas la pause : elle tient jusqu'à l'envoi confirmé.
    def test_repasted_uncertain_delivery_keeps_the_pause_until_it_is_sent(self):
        codex_reply, waiter = self.waiting()
        with self.db() as db:
            rules.claim(db, "claude", 2.0, waiter)
            rules.status_line(db, 40.0)
            pasted = state.draft(db, state.Relay(codex_reply, "codex", "claude", "R0"))  # Ctrl-b R
            rules.toggle(db, 41.0, True)
            self.assertEqual(rules.status_line(db, 41.0),
                             "PAUSE — brouillon en attente chez Claude : Entrée, ou Ctrl-b R pour le recoller")
            self.assertEqual(state.get(db, "count"), "1")
        self.reply("claude", "Réponse au relais", self.submit("claude", pasted))  # Entrée, puis réponse
        with self.db() as db:
            rules.toggle(db, 50.0, True)
            self.assertEqual(rules.status_line(db, 50.0), "AUTO 0/8")

    # --- 9. Un envoi vers Codex qui ne revient jamais finit en pause visible.
    def test_send_to_codex_that_never_returns_pauses(self):
        relay = self.reserved()  # réservé à 1.0, la commande ne rend jamais la main
        with self.db() as db:
            self.assertEqual(rules.status_line(db, 1.0 + rules.SEND_SECONDS), "AUTO 1/8")
            self.assertEqual(rules.status_line(db, 2.0 + rules.SEND_SECONDS), "PAUSE — remise incertaine")
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.UNCERTAIN)

    # --- 10. Le retour tardif de `codex queue` règle le sort de la copie automatique, quoi qu'ait fait l'utilisateur.
    def test_late_queue_return_after_a_manual_resend_withdraws_the_automatic_copy(self):
        relay = self.reserved()
        with self.db() as db:
            rules.status_line(db, 200.0)  # délai dépassé : remise incertaine
            pasted = state.draft(db, state.Relay(relay.reply_id, "claude", "codex", "C1"))  # Ctrl-b R
        self.submit("codex", pasted + "\n\nEt les tests ?")  # phrase ajoutée, puis Entrée : envoi manuel
        self.assertEqual(self.status_of(relay.reply_id), state.SENT)
        with self.db() as db:
            self.assertFalse(rules.enqueued(db, relay, "q1", 300.0))  # la copie automatique est à retirer
            self.assertTrue(rules.release(db, relay, cancelled=True))
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.SENT)  # l'envoi manuel reste acquis
            self.assertEqual(state.get(db, "count"), "0")  # la copie automatique n'est jamais partie
            added = db.execute("SELECT count(*) FROM events WHERE kind = 'submitted' AND text = 'Et les tests ?'")
            self.assertEqual(added.fetchone(), (1,))  # la phrase ajoutée voyagera au relais retour

    def test_late_queue_return_without_manual_resend_frees_the_reply(self):
        relay = self.reserved()
        with self.db() as db:
            rules.status_line(db, 200.0)
            self.assertFalse(rules.enqueued(db, relay, "q1", 300.0))
            self.assertTrue(rules.release(db, relay, cancelled=True))  # le retrait réussit : rien n'est parti
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.NEW)
            self.assertEqual((state.get(db, "count"), state.get(db, "awaiting")), ("0", ""))
            rules.toggle(db, 301.0, True)
            self.assertEqual(rules.status_line(db, 301.0), "AUTO 0/8")
            self.assertEqual(rules.claim(db, "codex", 302.0).reply_id, relay.reply_id)

    def test_late_queue_id_is_kept_on_the_reservation(self):
        relay = self.reserved()
        with self.db() as db:
            rules.status_line(db, 200.0)  # copie perdue de vue
            self.assertFalse(rules.enqueued(db, relay, "q1", 300.0))  # l'identifiant arrive tard
            self.assertTrue(rules.release(db, relay, cancelled=False))  # absente de la file : rien de prouvé
            self.assertEqual((self.status_of_in(db, relay.reply_id), state.get(db, "count"), state.get(db, "awaiting")),
                             (state.UNCERTAIN, "1", "codex"))
            self.assertEqual(db.execute("SELECT thread, queue_id FROM claims").fetchone(), ("fil-codex", "q1"))

    def test_late_failure_after_the_deadline_frees_the_reply(self):
        relay = self.reserved()
        with self.db() as db:
            rules.status_line(db, 200.0)
            self.assertTrue(rules.revert(db, relay))  # la commande finit par échouer : rien n'est parti
            self.assertEqual((self.status_of_in(db, relay.reply_id), state.get(db, "count")), (state.NEW, "0"))

    # --- 13. La fin d'un ancien tour ne conclut pas un accord, même si son message portait la même étiquette.
    def test_end_of_an_older_codex_turn_is_not_the_answer_to_a_manual_resend(self):
        claude_reply = self.reply("claude", "C1\n[ACCORD]", self.exchange())
        self.on()
        with self.db() as db:
            relay = rules.claim(db, "codex", 1.0)
            automatic = state.compose(db, relay)
            rules.status_line(db, 200.0)  # remise incertaine de la copie automatique
            pasted = state.draft(db, state.Relay(claude_reply, "claude", "codex", "C1\n[ACCORD]"))  # Ctrl-b R
        asked = self.submit("codex", pasted + "\n\nNouvelle contrainte : sans dépendance.")  # renvoi manuel
        self.reply("codex", "D'accord.\n[ACCORD]", self.fresh(), prompt=automatic)  # fin de l'ancien tour automatique
        with self.db() as db:
            self.assertFalse(state.agreed(db))
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "unidentified")), ("codex", "0"))
        answer = self.reply("codex", "Avec cette contrainte, je change d'avis.", asked,
                            prompt=pasted + "\n\nNouvelle contrainte : sans dépendance.")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (answer,)).fetchone(),
                             (claude_reply,))
            self.assertFalse(state.agreed(db))

    # --- 14. Pour deux brouillons préparés d'avance, c'est l'ordre des Entrée qui fait l'ordre des remises.
    def test_manual_deliveries_are_ordered_by_submission_not_by_paste(self):
        claude_reply, codex_reply = self.opening()
        with self.db() as db:
            for_claude = state.draft(db, state.Relay(codex_reply, "codex", "claude", "R0"))  # collé d'abord
            for_codex = state.draft(db, state.Relay(claude_reply, "claude", "codex", "C0"))  # collé ensuite
        self.submit("codex", for_codex)  # Entrée chez Codex en premier
        asked = self.submit("claude", for_claude)  # puis Entrée chez Claude
        with self.db() as db:
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "awaiting_reply")),
                             ("claude", str(codex_reply)))
        answer = self.reply("claude", "C1", asked)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE id = ?", (answer,)).fetchone(),
                             (codex_reply,))

    def test_older_draft_without_rank_does_not_outrank_a_newer_one(self):
        _, first = self.opening()
        second = self.turn("codex", "Précise", "R1")
        with self.db() as db:
            state.mark(db, first, state.DRAFTED)  # brouillon d'avant la mise à jour : sans rang
            state.draft(db, state.Relay(second, "codex", "claude", "R1"))
        self.submit("claude", f"[Réponse de Codex #{first}]\n\nR0\n\n[Réponse de Codex #{second}]\n\nR1")
        with self.db() as db:
            self.assertEqual(state.get(db, "awaiting_reply"), str(second))

    @unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 35), "suppression de colonne requise pour la base v4")
    def test_withdrawal_in_progress_before_the_update_still_has_a_deadline(self):
        relay = self.reserved()
        with self.db() as db:
            rules.enqueued(db, relay, "q1", 1.1)
            rules.toggle(db, 2.0, True)  # retrait confié, jamais terminé
            for column in ("thread", "queue_id", "since"):  # base de la version 4
                db.execute(f"ALTER TABLE claims DROP COLUMN {column}")
            db.execute("DELETE FROM meta WHERE key = 'claude_failed'")
            db.execute("ALTER TABLE replies DROP COLUMN delivery")
            db.execute("DELETE FROM meta WHERE key IN ('delivery_seq', 'awaited_seq')")
            db.execute("PRAGMA user_version = 4")
        with self.db() as db:
            rules.toggle(db, 86_400.0, True)
            self.assertEqual(rules.status_line(db, 86_400.0), "PAUSE — remise incertaine")

    def to_version_5(self, db):
        db.execute("ALTER TABLE claims DROP COLUMN thread")
        db.execute("ALTER TABLE claims DROP COLUMN queue_id")
        db.execute("DELETE FROM meta WHERE key = 'claude_failed'")
        db.execute("PRAGMA user_version = 5")

    @unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 35), "suppression de colonne requise pour la base v5")
    def test_migrated_copy_is_withdrawn_from_the_thread_it_was_sent_to(self):
        relay = self.reserved()
        with self.db() as db:
            self.in_queue(db, relay)
            self.to_version_5(db)
            state.put(db, codex_thread="fil-B")  # un autre fil Codex est devenu actif avant la mise à jour
        with self.db() as db:
            self.assertEqual(rules.toggle(db, 2.0, True), [(relay, "fil-codex", "q1")])

    def test_session_already_migrated_by_the_previous_plan_withdraws_from_the_original_thread(self):
        relay = self.reserved()
        with self.db() as db:
            self.in_queue(db, relay)
            # Base au format 6 du plan 17 : sa migration avait donné à la réservation le fil actif, B.
            db.execute("UPDATE claims SET thread = 'fil-B' WHERE id = ?", (relay.claim,))
            state.put(db, codex_thread="fil-B")
            db.execute("PRAGMA user_version = 6")
        with self.db() as db:
            self.assertEqual(rules.toggle(db, 2.0, True), [(relay, "fil-codex", "q1")])

    def test_previous_plan_reply_naming_the_wrong_thread_keeps_the_reservation_thread_too(self):
        relay = self.reserved()
        with self.db() as db:
            self.in_queue(db, relay)
            # Base au format 6 du plan 17 : la réservation garde le bon fil, A ; la réponse désigne B.
            db.execute("UPDATE replies SET target_session = 'fil-B' WHERE id = ?", (relay.reply_id,))
            state.put(db, codex_thread="fil-B")
            db.execute("PRAGMA user_version = 6")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT thread, other_thread FROM claims WHERE id = ?",
                                        (relay.claim,)).fetchone(), ("fil-B", "fil-codex"))
        self.submit("codex", f"[Réponse de Claude #{relay.reply_id}]\n\nC1")  # A prend la copie
        with self.db() as db:  # l'accusé établit la destination
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.SENT)
            self.assertEqual(db.execute("SELECT claims.thread, claims.other_thread, replies.target_session FROM claims "
                                        "JOIN replies ON replies.claim_id = claims.id WHERE claims.id = ?",
                                        (relay.claim,)).fetchone(), ("fil-codex", "", "fil-codex"))

    @unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 35), "suppression de colonne requise pour la base v7")
    def test_base_already_at_format_7_gets_the_second_thread_column(self):
        relay = self.reserved()
        with self.db() as db:
            db.execute("ALTER TABLE claims DROP COLUMN other_thread")
            db.execute("PRAGMA user_version = 7")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT thread, other_thread FROM claims WHERE id = ?",
                                        (relay.claim,)).fetchone(), ("fil-codex", ""))

    def test_acknowledgement_after_the_send_deadline_prevails_over_the_command_result(self):
        relay = self.reserved()
        with self.db() as db:
            self.assertEqual(rules.status_line(db, 1.0 + rules.SEND_SECONDS + 1), "PAUSE — remise incertaine")
        self.submit("codex", f"[Réponse de Claude #{relay.reply_id}]\n\nC1")  # accusé de Codex, tardif
        with self.db() as db:
            self.assertFalse(rules.revert(db, relay))  # refus tardif de `codex queue` : sans effet
            self.assertEqual(state.get(db, "count"), "1")
            self.assertTrue(rules.enqueued(db, relay, "q1", 200.0))  # succès tardif : rien à retirer
            self.assertEqual(db.execute("SELECT state FROM claims WHERE id = ?", (relay.claim,)).fetchone(),
                             (state.UNCERTAIN,))
        self.assertEqual(self.status_of(relay.reply_id), state.SENT)

    def test_queue_id_written_by_a_previous_version_is_still_withdrawn(self):
        relay = self.reserved()
        with self.db() as db:
            state.mark(db, relay.reply_id, state.QUEUED, "q1")  # un hook v5 finit son envoi après la mise à jour
            self.assertEqual(rules.toggle(db, 2.0, True), [(relay, "fil-codex", "q1")])

    # --- 12. Un retrait confié puis interrompu finit lui aussi en pause visible.
    def test_interrupted_withdrawal_ends_in_a_visible_pause(self):
        relay = self.reserved()
        with self.db() as db:
            rules.enqueued(db, relay, "q1", 1.1)
            self.assertEqual(rules.toggle(db, 2.0, True), [(relay, "fil-codex", "q1")])  # retrait confié, jamais terminé
            rules.toggle(db, 3.0, True)
            self.assertEqual(rules.status_line(db, 3.0), "AUTO 0/8")  # le retrait est encore dans son délai
            rules.toggle(db, 4.0, True)  # OFF
            rules.toggle(db, 86_400.0, True)  # reprise un jour plus tard
            self.assertEqual(rules.status_line(db, 86_400.0), "PAUSE — remise incertaine")
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.UNCERTAIN)  # recollable par Ctrl-b R
            self.assertTrue(rules.release(db, relay, cancelled=True))  # le résultat arrive enfin
            self.assertEqual(self.status_of_in(db, relay.reply_id), state.NEW)

    def test_relay_waiting_in_codex_queue_is_not_overdue(self):
        relay = self.reserved()
        with self.db() as db:
            rules.enqueued(db, relay, "q1", 1.1)  # Codex est occupé : le message attend dans sa file
            self.assertEqual(rules.status_line(db, 86_400.0), "AUTO 1/8")

    def test_only_a_result_from_the_current_activation_may_pause_it(self):
        old_relay = self.reserved()
        with self.db() as db:
            self.assertTrue(rules.of_current_run(db, old_relay))
            rules.toggle(db, 1.5, True)
            self.assertTrue(rules.of_current_run(db, old_relay))  # une coupure seule ne change pas d'activation
        self.on(2.0)
        with self.db() as db:
            self.assertFalse(rules.of_current_run(db, old_relay))

    @unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 35), "suppression de colonne requise pour la base v3")
    def test_reservation_open_before_the_update_becomes_uncertain(self):
        claude_reply = self.reply("claude", "C1", self.exchange())
        with self.db() as db:  # remettre la base dans son format précédent, avec une réservation en cours
            state.mark(db, claude_reply, state.QUEUED, "q1")
            db.execute("DROP TABLE claims")
            for column in ("claim_id", "delivery"):
                db.execute(f"ALTER TABLE replies DROP COLUMN {column}")
            db.execute("DELETE FROM meta WHERE key IN ('run', 'delivery_seq', 'awaited_seq')")
            db.execute("PRAGMA user_version = 3")
        self.assertEqual(self.status_of(claude_reply), state.UNCERTAIN)  # visible, et recollable par Ctrl-b R

    @unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 35), "suppression de colonne requise pour la base v4")
    def test_queued_reply_without_a_reservation_becomes_uncertain(self):
        claude_reply = self.reply("claude", "C1", self.exchange())
        with self.db() as db:  # base passée en version 4 avec un relais en file sans réservation associée
            state.mark(db, claude_reply, state.QUEUED, "q1")
            for column in ("thread", "queue_id", "since"):  # base de la version 4
                db.execute(f"ALTER TABLE claims DROP COLUMN {column}")
            db.execute("DELETE FROM meta WHERE key = 'claude_failed'")
            db.execute("ALTER TABLE replies DROP COLUMN delivery")
            db.execute("DELETE FROM meta WHERE key IN ('delivery_seq', 'awaited_seq')")
            db.execute("PRAGMA user_version = 4")
        self.assertEqual(self.status_of(claude_reply), state.UNCERTAIN)

    def test_limit_pauses_and_an_effective_restart_resets_the_counter(self):
        self.reply("claude", "C1", self.exchange())
        self.on()
        with self.db() as db:
            state.put(db, max=2, count=2)
            self.assertIsNone(rules.claim(db, "codex", 1.0))
            self.assertEqual(rules.status_line(db, 1.0), "PAUSE — limite de relais atteinte")
            rules.toggle(db, 2.0, True)
            self.assertEqual(rules.status_line(db, 2.0), "AUTO 0/2")

    def test_agreement_pauses_then_on_relays_the_last_reply(self):
        self.reply("claude", "C1\n[ACCORD]", self.exchange())
        self.on()
        with self.db() as db:
            waiter = rules.new_waiter(db, 0.0)
            sent = state.compose(db, rules.claim(db, "codex", 1.0))
        self.reply("codex", "R1\n[ACCORD]", self.fresh(), prompt=sent)  # la notification reprend le message entier
        with self.db() as db:
            self.assertIsNone(rules.claim(db, "claude", 2.0, waiter))
            self.assertEqual(rules.status_line(db, 2.0), "PAUSE — accord trouvé")
            rules.toggle(db, 3.0, True)
            self.assertEqual(rules.status_line(db, 3.0), "AUTO 0/8")
            self.assertEqual(rules.claim(db, "claude", 3.0, waiter).body, "R1\n[ACCORD]")

    def test_expired_deadline_hands_the_relay_to_a_paste(self):
        self.waiting()
        with self.db() as db:  # tâche 8 : au-delà de l'échéance, le relais est dû au collage chez Claude
            self.assertEqual(rules.status_line(db, rules.WAIT_SECONDS + 1.0), "AUTO 0/8")
            self.assertEqual(rules.paste_due(db, rules.WAIT_SECONDS + 1.0).recipient, "claude")

    def test_size_boundary(self):
        self.reply("claude", "x" * state.MAX_BODY, self.exchange())
        self.on()
        with self.db() as db:
            self.assertIsNotNone(rules.claim(db, "codex", 1.0))

    def test_one_character_over_the_boundary_pauses(self):
        self.reply("claude", "x" * (state.MAX_BODY + 1), self.exchange())
        self.on()
        with self.db() as db:
            self.assertIsNone(rules.claim(db, "codex", 1.0))
            self.assertEqual(rules.status_line(db, 1.0), "PAUSE — réponse trop longue pour l'auto")

    def test_reserved_relay_remembers_the_text_it_carries(self):
        relay = self.reserved()
        with self.db() as db:
            pasted, = db.execute("SELECT pasted FROM replies WHERE id = ?", (relay.reply_id,)).fetchone()
            claimed_at, = db.execute("SELECT claimed_at FROM claims WHERE id = ?", (relay.claim,)).fetchone()
            self.assertEqual((pasted, claimed_at), (state.compose(db, relay), 1.0))

    def test_permission_request_is_shown(self):
        with self.db() as db:
            state.record_permission(db, self.event("codex", "permission", "", "7", None))
            self.assertEqual(rules.status_line(db, 0.0), "OFF · Codex : autorisation demandée")


if __name__ == "__main__":
    unittest.main()
