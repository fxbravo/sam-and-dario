"""Relecture du remplacement d'un relais. Lancer avec python3 -m unittest tests.test_review_replace."""

import unittest
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import crossread as crossread
from samdario import state as state
from tests.test_auto import clock, done, stop, taken, typed
from tests.test_replace import ASK, LATE, Replace


CHOUETTE = "La chouette se reconnaît à son disque facial."


class Remplacement(Replace):
    """Événements natifs sur une base jetable. Les files et les collages enregistrent le texte transporté."""

    def coller(self, pane):
        with patch.object(samdario, "tmux", return_value="0/0") as mux:
            samdario.relay(self.state, pane)
        return next(call.kwargs["data"].decode() for call in mux.call_args_list if call.args[1] == "load-buffer")

    def ouverture_manuelle(self, texte_codex):
        with self.db() as db:
            number = state.share(db, dict.fromkeys(state.NAMES, "Un nom ?"))
            question = state.common(number, "Un nom ?")
        auto.handle_event(self.state, "claude", typed(question, "k1"), now=0.5)
        auto.handle_event(self.state, "claude", stop("Premier avis de Claude.", "k1"), now=1)
        auto.handle_event(self.state, "codex", taken(question, "t1"), now=1.5)
        auto.handle_event(self.state, "codex", done(texte_codex, "t1", question), now=2)

    def reponse_codex_au_relais(self, texte, tour, instant):
        message = self.send.call_args.args[1]
        auto.handle_event(self.state, "codex", taken(message, tour), now=instant)
        auto.handle_event(self.state, "codex", done(texte, tour, message), now=instant + 1)

    def prochain_reveil(self, generation, instant):
        # Au deuxième passage l'attente expire si aucun message n'est prêt.
        message = auto.wait(self.state, generation, clock=clock(instant, 4000), sleep=lambda _: None)
        self.assertIsNotNone(message)
        return message

    def test_la_reponse_au_renard_part_avant_la_fin_tardive_de_codex(self):
        self.pasted_at_claude(codex=CHOUETTE)
        ancienne = "Un détail sur la chouette ?"
        auto.handle_event(self.state, "codex", taken(ancienne, "t-ancien"), now=LATE + 1.5)
        self.renard()
        auto.handle_event(self.state, "codex", done("Précision sur la chouette.", "t-ancien", ancienne),
                          now=LATE + 2.5)
        auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        self.assertEqual(self.send.call_count, 1, "La réponse de Claude au renard devrait déjà partir vers Codex.")
        message = self.send.call_args.args[1]
        self.assertTrue(message.startswith("[Réponse de Claude #4]"), message)
        self.assertIn("Et pour un renard ?", message)
        self.assertIn("Roussin.", message)

    def test_une_question_dans_l_autre_panneau_ne_leve_pas_la_pause_brouillon(self):
        self.launch()
        self.assertIn("[Réponse de Codex #2]", self.coller("%0"))
        self.assertIn("brouillon en attente", auto.toggle(self.state, now=3))
        question = "Une question indépendante chez Codex"
        auto.handle_event(self.state, "codex", taken(question, "t-autre"), now=4)
        auto.handle_event(self.state, "codex", done("Réponse indépendante.", "t-autre", question), now=5)
        self.assertEqual(self.row_of(2)[0], state.DRAFTED)
        self.assertIn("brouillon en attente", auto.status(self.state, now=6))

    def test_effacer_un_autre_brouillon_ne_clot_pas_la_lecture_du_relais_du(self):
        self.ouverture_manuelle(ASK)
        with self.db() as db:
            du = crossread.due(db)
        self.assertEqual((du.reply_id, du.recipient), (2, "claude"))
        self.assertIn("[Réponse de Claude #1]", self.coller("%1"))
        question = "Autre question chez Codex"
        auto.handle_event(self.state, "codex", taken(question, "tB"), now=3)
        auto.handle_event(self.state, "codex", done("L'avis de Codex sur B.", "tB", question), now=4)
        self.assertEqual(self.row_of(1)[0], state.REPLACED)
        auto.toggle(self.state, now=LATE)
        self.paste.assert_called_once()
        message = self.paste.call_args.args[2]
        self.assertTrue(message.startswith("[Réponse de Codex #2]"), message)

    def test_le_contexte_efface_du_brouillon_avant_entree_reste_a_transmettre(self):
        self.pasted_at_claude(codex=CHOUETTE)
        self.renard()
        auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=LATE + 3)
        self.reponse_codex_au_relais("Goupil.", "t9", LATE + 4)
        # Attente expirée. Le nouveau relais, avec la chouette en contexte, est collé chez Claude.
        self.assertIn("Entrée chez Claude", auto.status(self.state, now=2 * LATE))
        brouillon = self.paste.call_args.args[2]
        self.assertIn(CHOUETTE, brouillon)
        self.assertTrue(brouillon.startswith("[Réponse de Codex #4]"))
        modifie = brouillon.replace(CHOUETTE, "")
        auto.handle_event(self.state, "claude", typed(modifie, "k11"), now=2 * LATE + 1)
        self.assertEqual(self.row_of(4)[0], state.SENT)
        generation = auto.handle_event(self.state, "claude", stop("J'ai lu Goupil.", "k11"), now=2 * LATE + 2)
        self.reponse_codex_au_relais("Autre avis.", "t11", 2 * LATE + 3)
        suivant = self.prochain_reveil(generation, 2 * LATE + 5)
        self.assertIn(CHOUETTE, suivant)

    def test_un_texte_remplace_depassant_le_plafond_indique_ou_le_lire(self):
        long = "O" * (state.MAX_BODY + 1)
        self.ouverture_manuelle(long)
        self.assertIn(long, self.coller("%0"))
        self.renard(now=4)
        generation = auto.handle_event(self.state, "claude", stop("Roussin.", "k9"), now=5)
        auto.toggle(self.state, now=6)
        self.reponse_codex_au_relais("Goupil.", "t9", 7)
        message = self.prochain_reveil(generation, 9)
        self.assertNotIn(long, message)
        visible = (message + "\n" + auto.status(self.state, now=9)).casefold()
        self.assertRegex(visible, r"panneau|termina(?:l|ux)|chez codex|côté codex")

    def test_un_collage_empeche_par_off_reste_disponible_apres_la_saisie(self):
        self.launch()
        appliquer = auto.paste_reserved

        def couper_avant_collage(folder, relay, text, seq):
            auto.toggle(folder, now=LATE + 0.1)
            auto.handle_event(folder, "claude", typed("Question avant le collage", "k9"), now=LATE + 0.2)
            return appliquer(folder, relay, text, seq)

        with patch.object(auto, "paste_reserved", side_effect=couper_avant_collage) as reservation:
            auto.toggle(self.state, now=LATE)
        reservation.assert_called_once()
        self.paste.assert_not_called()
        self.assertEqual((self.meta("auto"), self.meta("count")), (state.OFF, "0"))
        self.assertEqual(self.row_of(2), (state.NEW, None))


if __name__ == "__main__":
    unittest.main()
