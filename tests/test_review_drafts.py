"""Brouillons de la quatrième relecture (tâche 8). Lancer avec python3 -m unittest tests.test_review_drafts."""

import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import state as state
from tests import test_auto
from tests.test_auto import done, stop, typed


class BrouillonsTache8(test_auto.LoopBase):
    def setUp(self):
        super().setUp()
        queue = patch.object(auto, "queue_send", return_value="q1")
        self.envoi = queue.start()
        self.addCleanup(queue.stop)
        collage = patch.object(auto, "paste_draft")
        self.collage = collage.start()
        self.addCleanup(collage.stop)
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        self.launch()
        auto.toggle(self.state, now=3)
        auto.toggle(self.state, now=3.5)  # OFF, sans réveil en attente chez Claude

    def touche_relais(self, force):
        with patch.object(samdario, "tmux", return_value="0/0") as mux:
            notice = samdario.relay(self.state, "%0", force=force)
        buffers = [call.kwargs["data"].decode() for call in mux.call_args_list if call.args[1] == "load-buffer"]
        self.assertEqual(len(buffers), 1, notice)
        return buffers[0]

    def activer_avec_interruption(self, pendant):
        coller = auto.paste_reserved

        def apres_reservation(*args, **kwargs):
            pendant()  # la réservation a été validée en base, le panneau n'a encore rien reçu
            return coller(*args, **kwargs)

        with patch.object(auto, "paste_reserved", side_effect=apres_reservation):
            auto.toggle(self.state, now=4)

    def test_recoller_le_relais_manuel_avant_la_question(self):
        premier = self.touche_relais(force=False)
        self.assertTrue(premier.startswith("[Réponse de Codex #2]"))
        auto.broadcast(self.state, "Question aux deux", now=5)
        self.collage.assert_not_called()
        # L'effacement dans le panneau ne produit aucun événement de soumission.
        recolle = self.touche_relais(force=True)
        self.assertEqual(recolle, premier)
        auto.handle_event(self.state, "claude", typed(recolle, "k2"), now=6)
        auto.status(self.state, now=6.1)
        attente = auto.handle_event(self.state, "claude", stop("Réponse au relais", "k2"), now=7)
        commun = self.envoi.call_args.args[1]
        if self.collage.called:
            self.collage.assert_called_once_with(self.state, "claude", commun)
        else:
            self.assertEqual(auto.wait(self.state, attente, clock=lambda: 8, sleep=self.fail), commun)

    def test_retour_du_collage_apres_son_delai(self):
        self.activer_avec_interruption(lambda: auto.status(self.state, now=15))
        self.collage.assert_not_called()
        with self.db() as db:
            status, = db.execute("SELECT status FROM replies WHERE id = 2").fetchone()
            suivi = (status, state.get(db, "awaiting"), state.get(db, "count"))
        self.assertEqual(suivi, (state.NEW, "", "0"))

    def test_coupure_puis_autre_brouillon_avant_le_retour_du_collage(self):
        def prendre_le_champ():
            auto.toggle(self.state, now=4.1)
            auto.handle_event(self.state, "claude", typed("Autre demande", "k9"), now=4.2)
            auto.broadcast(self.state, "Une question commune", now=4.3)

        self.activer_avec_interruption(prendre_le_champ)
        commun = self.envoi.call_args.args[1]
        self.collage.assert_called_once_with(self.state, "claude", commun)
        auto.handle_event(self.state, "claude", stop("Réponse à mon autre demande", "k9"), now=5)
        auto.handle_event(self.state, "claude", typed(commun, "k10"), now=6)
        attente = auto.handle_event(self.state, "claude", stop("Avis de Claude", "k10"), now=7)
        auto.handle_event(self.state, "codex", done("Avis de Codex", "t5", commun), now=8)
        barre = auto.toggle(self.state, now=9)
        self.assertTrue(barre.startswith("AUTO "), barre)
        suivant = auto.wait(self.state, attente, clock=lambda: 10, sleep=self.fail)
        self.assertIn("Avis de Codex", suivant)

    def ancien_champ(self):
        # Base reconstituée au format 10 avec une copie queued/pasted. La commande suivante ouvre la base.
        with closing(sqlite3.connect(self.state / "replies.db")) as db, db:
            db.execute("DELETE FROM meta WHERE key LIKE 'claude_field%'")
            db.execute("PRAGMA user_version = 10")

    def test_coupure_comme_premier_geste_apres_mise_a_jour(self):
        auto.toggle(self.state, now=4)
        auto.broadcast(self.state, "Question aux deux", now=5)
        self.collage.assert_called_once()
        self.ancien_champ()
        auto.toggle(self.state, now=6)
        auto.status(self.state, now=7)
        self.collage.assert_called_once()  # le relais est toujours physiquement dans le champ

    def test_entree_annotee_comme_premier_geste_apres_mise_a_jour(self):
        auto.toggle(self.state, now=4)
        relais = self.collage.call_args.args[2]
        self.ancien_champ()
        phrase = "Précision ajoutée avant Entrée."
        message = f'{phrase}\n\n<pasted_content id="1234">\n{relais}\n\n</pasted_content id="1234">'
        auto.handle_event(self.state, "claude", typed(message, "k2"), now=5)
        auto.handle_event(self.state, "claude", stop("Réponse au relais", "k2"), now=6)
        self.envoi.assert_called_once()
        self.assertEqual(self.envoi.call_args.args[1].count(phrase), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
