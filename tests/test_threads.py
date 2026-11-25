import io
import json
import os
from contextlib import redirect_stdout
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import rules as rules
from samdario import state as state
from samdario import team as team
from tests.test_auto import LoopBase, THREAD, done, taken


NEW_THREAD = "fil-après-new"
OLD_QUESTION = "Quel nom retenir pour le chat roux ?"
OLD_ANSWER = "Nous avons retenu Safran."


class NewThreadTest(LoopBase):
    def previous_discussion(self):
        waiter = self.launch()
        auto.handle_event(self.state, "codex", done(OLD_ANSWER, "ancien", OLD_QUESTION), now=3)
        return waiter

    def codex_hook(self, thread, request, text):
        raw = dict(taken(text, request), session_id=thread)
        out = io.StringIO()
        with patch.dict(os.environ, SAMDARIO_STATE=str(self.state)), patch("sys.stdin", io.StringIO(json.dumps(raw))), \
                redirect_stdout(out):
            samdario.main(["_event", "codex"])
        return json.loads(out.getvalue()) if out.getvalue() else None

    def new_without_hook(self):
        raw = dict(done("Je reprends.", "b0", "Où en sommes-nous ?"), **{"thread-id": NEW_THREAD})
        auto.handle_event(self.state, "codex", raw, now=4)

    def test_first_direct_message_after_new_gets_rules_objective_and_previous_exchange_once(self):
        self.previous_discussion()
        context = self.codex_hook(NEW_THREAD, "b1", "Quelle était la décision ?")
        self.assertIsNotNone(context)
        hook = context["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "UserPromptSubmit")
        brief = hook["additionalContext"]
        for part in ("en binôme avec Claude", "exactement [ACCORD]", '"Question"', OLD_QUESTION, OLD_ANSWER):
            self.assertIn(part, brief)
        self.assertNotIn('"C1"', brief)  # Claude avait répondu indépendamment, cet avis n’avait pas été remis.
        self.assertIsNone(self.codex_hook(NEW_THREAD, "b1", "Quelle était la décision ?"))
        self.assertIsNone(self.codex_hook(THREAD, "a2", "Retour par /resume"))
        self.assertIsNone(self.codex_hook(NEW_THREAD, "b2", "On continue"))
        with self.db() as db:
            self.assertEqual(state.get(db, "count"), "0")
            self.assertIsNone(state.round_open(db))

    def test_reminder_of_a_new_thread_carries_the_names(self):
        self.previous_discussion()
        with self.db() as db:
            team.store(db, {"claude": "Alma", "codex": "Milo"}, team.SECTION.strip())
        brief = self.codex_hook(NEW_THREAD, "b1", "Qui es-tu ?")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Tu es Milo (Codex). Tu travailles en binôme avec Alma (Claude).", brief)
        self.assertIn("Le binôme tranche un désaccord technique par un essai.", brief)

    def test_a_title_thread_never_receives_the_discussion(self):
        self.previous_discussion()
        with patch("samdario.state.is_codex_user_thread", return_value=False):
            self.assertIsNone(self.codex_hook("fil-interne", "titre", "Titre ?"))
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_thread"), THREAD)

    def test_second_new_thread_keeps_earlier_decisions_and_only_confirmed_claude_input(self):
        self.previous_discussion()
        first = self.reply("claude", "Claude retient Safran aussi.", "c2", session="s")
        with self.db() as db:
            wire = state.draft(db, state.Relay(first, "claude", "codex", "Claude retient Safran aussi."))
        auto.handle_event(self.state, "codex", done("Décision partagée.", "a2", wire))
        self.codex_hook(NEW_THREAD, "b1", "Quelle suite ?")
        self.reply("codex", "Préparer le dessin.", "b1", session=NEW_THREAD)
        brief = self.codex_hook("troisième", "c1", "Où en sommes-nous ?")["hookSpecificOutput"]["additionalContext"]
        for phrase in (OLD_ANSWER, "Claude retient Safran aussi.", "Préparer le dessin."):
            self.assertIn(phrase, brief)
        self.assertLess(brief.index(OLD_ANSWER), brief.index("Préparer le dessin."))

    def test_history_is_bounded_and_newest_exchange_survives_a_long_earlier_turn(self):
        self.launch()
        auto.handle_event(self.state, "codex", done("x" * 100_000, "long", "Détail"))
        auto.handle_event(self.state, "codex", done(OLD_ANSWER, "last", OLD_QUESTION))
        info = self.codex_hook(NEW_THREAD, "b1", "Suite")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(OLD_ANSWER, info)
        self.assertIn("omis", info)
        self.assertLess(len(info), 8_000)

    def test_session_upgraded_after_new_supplies_the_missing_context(self):
        self.previous_discussion()
        self.new_without_hook()
        with self.db() as db:
            db.execute("DROP TABLE IF EXISTS codex_threads")
            db.execute("PRAGMA user_version = 9")
        brief = self.codex_hook(NEW_THREAD, "b1", "Suite")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(OLD_ANSWER, brief)
        self.assertIsNone(self.codex_hook(THREAD, "a2", "Retour"))

    def test_new_before_the_first_answer_knows_the_common_objective(self):
        with self.db() as db:
            number = state.share(db, dict.fromkeys(state.NAMES, OLD_QUESTION))
        self.assertIsNone(self.codex_hook(THREAD, "a1", samdario.prompt(OLD_QUESTION, "codex", number)))
        brief = self.codex_hook(NEW_THREAD, "b1", "Quel était l’objectif ?")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(OLD_QUESTION, brief)
        auto.handle_event(self.state, "codex", done("Tardive.", "a1", samdario.prompt(OLD_QUESTION, "codex", number)))
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_thread"), NEW_THREAD)

    def test_upgrade_finds_the_launch_thread_even_if_only_the_new_one_has_answered(self):
        with self.db() as db:
            number = state.share(db, dict.fromkeys(state.NAMES, OLD_QUESTION))
        self.submit("codex", samdario.prompt(OLD_QUESTION, "codex", number), session=THREAD)
        self.new_without_hook()
        with self.db() as db:
            db.execute("DROP TABLE codex_threads")
            db.execute("PRAGMA user_version = 9")
        brief = self.codex_hook(NEW_THREAD, "b1", "Suite")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(OLD_QUESTION, brief)
        self.assertIsNone(self.codex_hook(THREAD, "a1", "Retour au lancement"))

    def test_full_sized_relay_keeps_its_body_and_defers_the_reminder(self):
        self.previous_discussion()
        self.new_without_hook()
        body = "x" * state.MAX_BODY
        rid = self.reply("claude", body, "grand", session="s")
        with self.db() as db:
            wire = state.draft(db, state.Relay(rid, "claude", "codex", body))
            self.assertTrue(wire.endswith(body))
            self.assertIn("différé", wire)
            self.assertNotIn(OLD_ANSWER, wire)
        auto.handle_event(self.state, "codex", dict(done("Reçu.", "b1", wire), **{"thread-id": NEW_THREAD}))
        rid = self.reply("claude", "Suite.", "court", session="s")
        with self.db() as db:
            self.assertIn(OLD_ANSWER, state.draft(db, state.Relay(rid, "claude", "codex", "Suite.")))

    def test_automatic_copy_in_background_acknowledges_the_reminder_for_its_own_thread(self):
        self.previous_discussion()
        with self.db() as db:
            last = state.next_relay(db)
            sent = state.draft(db, last)
        request = self.submit("claude", sent, session="s")
        self.new_without_hook()
        rid = self.reply("claude", "Ma conclusion.", request, session="s")

        def queue(thread, wire, cwd):
            self.assertEqual(thread, NEW_THREAD)
            self.assertIn(OLD_ANSWER, wire)
            self.codex_hook("autre", "n1", "Une autre question")
            auto.handle_event(self.state, "codex", dict(taken(wire, "b2"), session_id=thread))
            return "q"

        with patch("samdario.auto.queue_send", side_effect=queue) as send:
            auto.toggle(self.state, now=6)
            send.assert_called_once()
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_thread"), "autre")
            self.assertEqual(db.execute("SELECT confirm_session FROM replies WHERE id = ?", (rid,)).fetchone(),
                             (NEW_THREAD,))
        self.assertIsNone(self.codex_hook(NEW_THREAD, "b3", "Retour au fil"))

    def test_manual_relay_without_hooks_carries_the_brief_until_receipt(self):
        self.previous_discussion()
        self.new_without_hook()
        rid = self.reply("claude", "Que penses-tu du nom ?", "suite", session="s")
        relay = state.Relay(rid, "claude", "codex", "Que penses-tu du nom ?")
        with self.db() as db:
            wire = state.draft(db, relay)
            self.assertTrue(wire.startswith(state.label("claude", rid) + "\n\n"))
            self.assertIn("exactement [ACCORD]", wire)
            self.assertIn(OLD_ANSWER, wire)
            self.assertTrue(wire.endswith(relay.body))
            self.assertEqual(state.draft(db, relay), wire)  # brouillon effacé puis recollé
        received = dict(done("Safran me convient.", "b1", wire), **{"thread-id": NEW_THREAD})
        auto.handle_event(self.state, "codex", received, now=5)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE session=? AND turn='b1'",
                                        (NEW_THREAD,)).fetchone(), (rid,))
            self.assertNotIn(OLD_ANSWER, state.compose(db, relay))

    def test_common_question_can_be_first_contact_without_hooks(self):
        waiter = self.previous_discussion()
        self.new_without_hook()
        with patch("samdario.auto.queue_send", return_value="q") as send:
            auto.broadcast(self.state, "Quel est votre avis ?", now=5)
        thread, wire, _ = send.call_args.args
        self.assertEqual((thread, state.common_number(wire)), (NEW_THREAD, "2"))
        self.assertIn(OLD_ANSWER, wire)
        self.assertEqual(auto.wait(self.state, waiter, clock=lambda: 6, sleep=self.fail),
                         state.common(2, "Quel est votre avis ?"))
        auto.handle_event(self.state, "codex", dict(done("Mon avis.", "b1", wire), **{"thread-id": thread}), now=7)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT request FROM shared WHERE round=2 AND agent='codex'").fetchone(),
                             ("b1",))
        self.assertIsNone(self.codex_hook(thread, "b2", "Autre chose"))

    def test_a_late_completion_from_a_hidden_thread_does_not_mask_the_active_agreement(self):
        self.previous_discussion()
        waiting = self.submit("codex", "Encore sur A", session=THREAD)
        self.codex_hook(NEW_THREAD, "b0", "Je reprends.")
        self.reply("codex", "Reprenons.", "b0", session=NEW_THREAD)
        rid = self.reply("claude", "D’accord pour Safran.\n[ACCORD]", "s2", session="s")
        with self.db() as db:
            wire = state.draft(db, state.Relay(rid, "claude", "codex", "D’accord pour Safran.\n[ACCORD]"))
        req = self.submit("codex", wire, session=NEW_THREAD)
        last = self.reply("codex", "Même choix.\n[ACCORD]", req, session=NEW_THREAD, prompt=wire)
        self.reply("codex", "Ancien résultat.", waiting, session=THREAD, prompt="Encore sur A")
        with self.db() as db:
            self.assertEqual(state.get(db, "codex_thread"), NEW_THREAD)
            rules.toggle(db, 9, True)
            self.assertEqual(rules.status_line(db, 9), "PAUSE — accord trouvé")
            self.assertEqual(int(state.get(db, "accord_seen")), last)

    def test_a_claude_agreement_with_the_old_codex_does_not_stop_the_new_thread(self):
        self.opening()
        codex = self.turn("codex", "Précise", "Mon choix.\n[ACCORD]")
        self.reply("claude", "Même choix.\n[ACCORD]", self.deliver(codex, "claude"))
        with self.db() as db:
            self.assertTrue(state.agreed(db))
        self.codex_hook(NEW_THREAD, "b0", "Une autre piste")
        with self.db() as db:
            self.assertFalse(state.agreed(db))
