"""Relecture des boutons. Lancer avec python3 -m unittest tests.test_review_buttons."""

import json
import unittest
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import bar as bar
from samdario import state as state
from tests.test_auto import LoopBase, stop, taken


class Boutons(LoopBase):
    def setUp(self):
        super().setUp()
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        self.options = {}

    def boutons(self, instant):
        """Relève les options que le vrai rafraîchissement publie ou conserve entre deux affichages."""
        with patch.object(bar, "tmux") as mux:
            bar.dashboard(self.state, now=instant)
        for call in mux.call_args_list:
            _, command, *args = call.args
            if command == "set-option" and len(args) == 2:
                self.options[args[0]] = args[1]
        return self.options.copy()

    def envoi_incertain(self):
        self.started()
        with patch.object(auto, "queue_send", side_effect=auto.UncertainSend("sortie illisible")) as send:
            auto.handle_event(self.state, "claude", stop("Rapport prêt", "k1"), now=1)
        self.assertEqual(auto.status(self.state, now=2), "PAUSE — remise incertaine")
        return send.call_args.args[1]

    def test_le_bouton_annonce_la_coupure_du_demarrage(self):
        with self.db() as db:
            state.share(db, dict.fromkeys(state.NAMES, "On commence ?"))
            state.put(db, autostart=1)
        self.assertIn("démarre après les deux réponses", auto.status(self.state, now=1))
        avant = self.boutons(2)
        self.assertTrue(auto.toggle(self.state, now=3).startswith("OFF"))
        apres = self.boutons(4)
        # Dans le format de la barre, « on » affiche ⏸ Pause, « off » affiche ▶ Auto.
        self.assertEqual((avant["@samdario-auto"], avant["@samdario-coller"],
                          apres["@samdario-auto"], apres["@samdario-coller"]), ("on", "", "off", "1"))

    def test_recoller_reste_accessible_pour_le_brouillon_manuel_de_codex(self):
        self.launch()
        with patch.object(samdario, "tmux", return_value="0/0") as mux:
            samdario.relay(self.state, "%1")
            brouillon = self.boutons(3)
            # Effacer le brouillon dans le panneau ne soumet aucun événement à Sam & Dario.
            samdario.relay(self.state, "%1", force=True)
        copies = [call.kwargs["data"] for call in mux.call_args_list if call.args[1] == "load-buffer"]
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0], copies[1])
        auto.handle_event(self.state, "codex", taken(copies[1].decode().rstrip("\n"), "t2"), now=4)
        remis = self.boutons(5)
        self.assertEqual((brouillon["@samdario-recoller"], remis["@samdario-recoller"]), ("1", ""))

    def test_une_autre_raison_de_pause_ne_masque_pas_la_remise_incertaine(self):
        self.envoi_incertain()
        with patch.object(auto, "codex_hooks_trusted", return_value=False):
            self.assertIn("hooks Codex à valider", auto.toggle(self.state, now=3))
        boutons = self.boutons(4)
        with patch.object(samdario, "tmux", return_value="0/0") as mux:
            samdario.relay(self.state, "%1", force=True)
        self.assertEqual(sum(call.args[1] == "paste-buffer" for call in mux.call_args_list), 1)
        self.assertEqual(boutons["@samdario-recoller"], "1")

    def test_un_accuse_tardif_retire_recoller_meme_si_la_pause_reste(self):
        message = self.envoi_incertain()
        self.assertEqual(self.boutons(2.1)["@samdario-recoller"], "1")
        auto.handle_event(self.state, "codex", taken(message, "t2"), now=3)
        self.assertEqual(auto.status(self.state, now=4), "PAUSE — remise incertaine")
        with self.db() as db:
            self.assertEqual(state.latest_reply(db, "claude")[2], state.SENT)
        with patch.object(samdario, "tmux", return_value="0/0") as mux:
            samdario.relay(self.state, "%1", force=True)
        self.assertFalse(any(call.args[1] == "paste-buffer" for call in mux.call_args_list))
        self.assertEqual(self.boutons(5)["@samdario-recoller"], "")


if __name__ == "__main__":
    unittest.main()
