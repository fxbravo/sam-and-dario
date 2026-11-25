"""Relecture de la tâche 13. Lancer avec python3 -m unittest tests.test_review_crossread."""

import unittest

from samdario import cli as samdario
from samdario import auto as auto
from samdario import export as export
from samdario import state as state
from tests.test_auto import done, stop, taken, typed
from tests.test_crossread import ASK, Crossread


class Relecture(Crossread):
    def test_une_ancienne_decision_ne_suspend_pas_la_nouvelle_question(self):
        attente = self.launch("Démarrons.", "Ma première proposition.")
        self.claude_reads(attente, "Ma réaction.")
        self.codex_reads("Je continue.")
        auto.handle_event(self.state, "codex", taken("Ancien sujet A", "t20"), now=51)
        auto.handle_event(self.state, "claude", typed("Nouveau sujet B", "c40"), now=52)
        ancienne = "Archiver A ?\n[DÉCISION]"
        auto.handle_event(self.state, "codex", done(ancienne, "t20", "Ancien sujet A"), now=53)
        with self.db() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM replies WHERE body = ?", (ancienne,)).fetchone())
        barre = auto.status(self.state, now=54)
        self.assertTrue(barre.startswith("AUTO"), barre)

    def test_l_accord_de_A_ne_devient_pas_exportable_pendant_que_B_attend(self):
        with self.db() as db:
            state.put(db, max=1, cwd=self.state)
        attente = self.launch(ASK, "Format CSV.")
        self.claude_reads(attente, "Je propose de figer A.\n[ACCORD]")
        self.assertIn("limite de relais", self.status())
        auto.handle_event(self.state, "claude", typed("Question B", "c70"), now=60)
        auto.handle_event(self.state, "claude", stop("Réponse à B.", "c70"), now=61)
        relais = self.send.call_args.args[1]
        self.assertTrue(relais.startswith("[Réponse de Claude #3]"))
        auto.handle_event(self.state, "codex", taken(relais, "t71"), now=62)
        auto.handle_event(self.state, "codex", done("Je valide A.\n[ACCORD]", "t71", relais), now=63)
        self.assertIsNone(export.latest(self.state), "L’accord sur A est proposé à l’export.")

    def test_les_options_restent_accessibles_apres_un_relais_qui_omet_la_decision(self):
        attente = self.launch(ASK, "Un CSV peut convenir.")
        self.claude_reads(attente, "R" * 99_990)
        recu = self.codex_reads("Réaction de Codex.")
        self.assertIn("décision requise", self.status())
        ajout = []
        auto.handle_event(self.state, "codex", taken("La deuxième option.", "t90"), now=70, send_context=ajout.append)
        options = "- PDF\n- CSV"
        self.assertTrue(any(options in texte for texte in [recu, *ajout]),
                        "Codex reçoit la réponse de l’utilisateur sans les deux options.")

    def test_le_contexte_joint_contient_aussi_l_historique_court_encore_inedit(self):
        auto.handle_event(self.state, "codex", taken("Note en cours.", "t19"), now=0)
        attente = self.launch(ASK, "Mon avis.")
        self.claude_reads(attente, "Voilà ma réaction.")
        note = "Le serveur garde les fichiers une journée."
        auto.handle_event(self.state, "codex", done(note, "t19", "Note en cours."), now=22)
        decision = "Reste à choisir le fuseau horaire."
        self.codex_reads(decision + "\n[DÉCISION]")
        ajout = []
        auto.handle_event(self.state, "claude", typed("UTC.", "c50"), now=61, send_context=ajout.append)
        contexte, = ajout
        self.assertIn(decision, contexte)
        self.assertIn(note, contexte)
        self.assertLess(contexte.index(decision), contexte.index(note))

    def choix_dans_un_nouveau_fil(self):
        ajouts = []
        saisie = dict(taken("Mon choix.", "t90"), session_id="nouveau-fil")
        auto.handle_event(self.state, "codex", saisie, now=70, send_context=ajouts.append)
        contexte, = ajouts
        return contexte

    def test_la_decision_courte_ne_fait_pas_compter_la_longue_comme_lue(self):
        longue = "PDF ou CSV ?\n" + "x" * 10_150 + "\nQue choisis-tu ?\n[DÉCISION]"
        courte = "Choisir aussi la langue ?"
        attente = self.launch(longue, "Je lis.")
        self.claude_reads(attente, courte + "\n[DÉCISION]")
        self.codex_reads("J’ai lu.")
        contexte = self.choix_dans_un_nouveau_fil()
        self.assertIn(courte, contexte)
        self.assertFalse(longue in contexte, "La décision longue dépasse à elle seule le budget du hook.")
        demande = self.submit("claude", "Récapitule.", session="s")
        numero = self.reply("claude", "Détail complémentaire.", demande, session="s")
        with self.db() as db:
            suivant = state.compose(db, state.Relay(numero, "claude", "codex", "Détail complémentaire."))
        self.assertTrue(longue in suivant, "La décision omise a disparu du prochain relais malgré la place disponible.")

    def test_le_budget_du_hook_inclut_le_rappel_du_nouveau_fil_codex(self):
        decision = "Je propose deux options.\n" + "R" * 7300 + "\nPDF ou CSV ?"
        attente = self.launch(decision + "\n[DÉCISION]", "Compris.")
        self.claude_reads(attente, "J’ai lu la réponse.")
        self.codex_reads("Réaction de Codex.")
        contexte = self.choix_dans_un_nouveau_fil()
        self.assertTrue(decision in contexte, "La décision entière tient dans les 9 000 caractères.")
        self.assertLessEqual(len(contexte), 9000)


class SecondeRelecture(Crossread):
    def test_une_troisieme_question_ne_rend_pas_exportable_l_accord_de_la_premiere(self):
        with self.db() as db:
            state.put(db, max=1, cwd=self.state)
        attente = self.launch(ASK, "Format CSV.")
        self.claude_reads(attente, "Je propose de figer A.\n[ACCORD]")
        self.assertIn("limite de relais", self.status())
        auto.handle_event(self.state, "claude", typed("Question B", "c70"), now=60)
        auto.handle_event(self.state, "claude", stop("Réponse à B.", "c70"), now=61)
        dernier_relais_de_a = self.send.call_args.args[1]
        self.assertTrue(dernier_relais_de_a.startswith("[Réponse de Claude #3]"))
        auto.handle_event(self.state, "codex", taken(dernier_relais_de_a, "t71"), now=62)
        auto.handle_event(self.state, "claude", typed("Question C", "c72"), now=63)
        auto.handle_event(self.state, "codex", done("Je valide A.\n[ACCORD]", "t71", dernier_relais_de_a), now=64)
        self.assertIsNone(export.latest(self.state), "La question C a rendu exportable l’accord écarté sur A.")
        self.assertNotIn("accord trouvé", auto.status(self.state, now=65))

    def relais_long_vers_codex(self):
        attente = self.launch(ASK, "Je regarde.")
        self.claude_reads(attente, "R" * 99_990)
        recu = self.codex_reads("Réaction de Codex.")
        self.assertIn("décision requise", self.status())
        return recu

    def test_les_options_omises_survivent_au_refus_du_relais_qui_les_reprenait(self):
        premier_relais_recu = self.relais_long_vers_codex()
        auto.handle_event(self.state, "claude", typed("Voici une précision.", "c62"), now=62)
        self.send.side_effect = RuntimeError("envoi refusé pour cet essai")
        auto.handle_event(self.state, "claude", stop("Proposition revue.", "c62"), now=63)
        self.assertIn("suivi perdu", auto.status(self.state, now=64))
        tentatives = self.send.call_count
        self.send.side_effect = None
        auto.toggle(self.state, now=65)
        self.assertEqual(self.send.call_count, tentatives + 1)
        accepte = self.send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(accepte, "t75"), now=66)
        self.assertTrue(any(ASK in texte for texte in (premier_relais_recu, accepte)),
                        "Les options ne figuraient que dans l’envoi refusé.")

    def test_un_relais_apres_clear_garde_l_historique_de_la_nouvelle_session_claude(self):
        self.relais_long_vers_codex()
        demande = "Établir la devise du nouveau projet."
        reponse = "Devise retenue : EUR."
        nouveau = "nouvelle-session"
        auto.handle_event(self.state, "claude", dict(typed(demande, "c41"), session_id=nouveau), now=52)
        auto.handle_event(self.state, "claude", dict(stop(reponse, "c41"), session_id=nouveau), now=53)
        relais = self.send.call_args.args[1]
        self.assertIn(demande, relais)
        self.assertIn(reponse, relais)
        self.assertNotIn(ASK, relais)

    def test_le_contexte_joint_chez_claude_ecarte_la_fin_tardive_de_l_ancien_fil_codex(self):
        ancienne = "Note réservée à l’ancien fil."
        en_cours = "Choisir le fuseau horaire, UTC ou CET ?"
        demande = "Sujet sur l’ancien fil."
        auto.handle_event(self.state, "codex", dict(taken(demande, "t19"), session_id="ancien-fil"), now=0.01)
        auto.handle_event(self.state, "codex", taken("Nouveau fil.", "t0"), now=0.02)
        attente = self.launch(ASK, "Avis du fil courant.")
        self.claude_reads(attente, "Réaction de Claude.")
        fin_tardive = done(ancienne, "t19", demande)
        fin_tardive["thread-id"] = "ancien-fil"
        auto.handle_event(self.state, "codex", fin_tardive, now=22)
        self.codex_reads(en_cours + "\n[DÉCISION]")
        contexte = []
        auto.handle_event(self.state, "claude", typed("UTC.", "c62"), now=62, send_context=contexte.append)
        joint, = contexte
        self.assertIn(en_cours, joint)
        self.assertNotIn(ancienne, joint)

    def test_la_barre_oriente_vers_l_auteur_quand_la_decision_ne_tient_pas(self):
        attente = self.launch(ASK, "Mon avis.")
        self.claude_reads(attente, "Ma réaction.")
        choix, marqueur = "Archives PDF ou CSV ?\n", "\n[DÉCISION]"
        decision = choix + "X" * (8736 - len(choix) - len(marqueur)) + marqueur
        self.codex_reads(decision)
        barre = self.status()
        contexte = []
        auto.handle_event(self.state, "claude", typed("La seconde.", "c62"), now=62, send_context=contexte.append)
        self.assertTrue(any(decision in texte for texte in contexte) or "décision de Codex : réponds chez lui" in barre,
                        "Claude reçoit le choix sans les options et la barre n’a pas indiqué où répondre.")


class TroisiemeRelecture(Crossread):
    def ouvrir_b_quand_a_attend_son_dernier_relais(self):
        with self.db() as db:
            state.put(db, max=1, cwd=self.state)
        attente = self.launch(ASK, "Format CSV.")
        self.claude_reads(attente, "Je propose de figer A.\n[ACCORD]")
        self.assertIn("limite de relais", self.status())
        auto.handle_event(self.state, "claude", typed("Question B", "c70"), now=60)

    def test_l_envoi_manuel_du_relais_de_a_ne_valide_pas_son_accord_ecarte(self):
        self.ouvrir_b_quand_a_attend_son_dernier_relais()
        (self.state / "panes.json").write_text('{"%0": "claude", "%1": "codex"}', encoding="utf-8")
        self.patch("samdario.cli.tmux", return_value="0/0")
        collage = self.patch("samdario.cli.paste_to_pane")
        samdario.relay(self.state, "%1")
        collage.assert_called_once()
        relais = collage.call_args.args[2]
        self.assertTrue(relais.startswith("[Réponse de Claude #3]"))
        auto.handle_event(self.state, "codex", taken(relais, "t71"), now=61)
        auto.handle_event(self.state, "codex", done("Je valide A.\n[ACCORD]", "t71", relais), now=62)
        self.assertIsNone(export.latest(self.state), "L’envoi manuel a rendu A exportable.")

    def test_l_exclusion_de_a_survit_aux_questions_suivantes_pendant_le_tour_codex(self):
        self.ouvrir_b_quand_a_attend_son_dernier_relais()
        auto.handle_event(self.state, "claude", stop("Réponse à B.", "c70"), now=61)
        relais = self.send.call_args.args[1]
        self.assertTrue(relais.startswith("[Réponse de Claude #3]"))
        auto.handle_event(self.state, "codex", taken(relais, "t71"), now=62)
        for numero in range(20):
            demande = f"c-suite-{numero}"
            instant = 63 + 2 * numero
            auto.handle_event(self.state, "claude", typed(f"Question suivante {numero}", demande), now=instant)
            auto.handle_event(self.state, "claude", stop(f"Réponse suivante {numero}.", demande), now=instant + 1)
        auto.handle_event(self.state, "codex", done("Je valide A.\n[ACCORD]", "t71", relais), now=105)
        self.assertIsNone(export.latest(self.state), "A est redevenue exportable pendant le même tour de Codex.")

    def test_le_premier_choix_apres_clear_recoit_les_options_lues_seulement_dans_l_ancienne_session(self):
        attente = self.launch("J’attends ton avis.", ASK)
        ancien_relais, _ = self.claude_reads(attente, "Le choix revient à l’utilisateur.")
        self.assertIn(ASK, ancien_relais)
        self.codex_reads("J’attends son choix.")
        self.assertIn("décision requise", self.status())
        contexte = []
        premier_message = dict(typed("La deuxième option.", "c-apres-clear"), session_id="nouvelle-session")
        auto.handle_event(self.state, "claude", premier_message, now=60, send_context=contexte.append)
        self.assertTrue(any(ASK in texte for texte in contexte), "La nouvelle session de Claude n’a pas reçu les options.")

    def test_une_decision_reconnue_avec_un_marqueur_indente_garde_ses_options(self):
        decision = ASK.replace("\n[DÉCISION]", "\n  [DÉCISION]")
        attente = self.launch(decision, "Je regarde.")
        self.claude_reads(attente, "R" * 99_990)
        recu = self.codex_reads("Réaction de Codex.")
        self.assertIn("décision requise", self.status())
        contexte = []
        auto.handle_event(self.state, "codex", taken("La deuxième option.", "t90"), now=60, send_context=contexte.append)
        self.assertTrue(any("- PDF\n- CSV" in texte for texte in (recu, *contexte)),
                        "Sam & Dario attend le choix de l’utilisateur, mais Codex n’a reçu aucune des deux options.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
