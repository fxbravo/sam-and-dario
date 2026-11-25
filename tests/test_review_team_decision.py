"""Relecture des tâches 9 et 10 : équipe et décision. Lancer avec python3 -m unittest tests.test_review_team_decision."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from samdario import cli as samdario
from samdario import auto as auto
from samdario import state as state
from samdario import team as team
from tests import test_auto


class CharteDuProjet(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        self.charter = self.project / team.CHARTER
        self.config = self.project / team.TEAM_FILE
        self.config.write_text(json.dumps({"claude": "Alma", "codex": "Milo"}), encoding="utf-8")

    def test_exemple_entre_tildes(self):
        section = "## Travail en binôme\nRègle du projet."
        self.charter.write_text(f"~~~md\n## Travail en binôme\nExemple.\n~~~\n\n{section}\n", encoding="utf-8")
        (self.project / "CLAUDE.md").write_text("~~~md\n@AGENTS.md\n~~~\n", encoding="utf-8")
        self.assertEqual((team.charter_rules(self.project), bool(team.charter_warnings(self.project))),
                         (section, True))

    def test_exemple_entre_quatre_accents_graves(self):
        section = "## Travail en binôme\nRègle du projet."
        example = "````markdown\n```md\n## Travail en binôme\nExemple.\n```\n````\n\n"
        self.charter.write_text(example + section, encoding="utf-8")
        self.assertEqual(team.charter_rules(self.project), section)

    def accepter_les_propositions(self):
        questions = []

        def repondre(question):
            questions.append(question)
            return "oui"

        team.setup(self.project, ask=repondre, say=lambda message: None)
        return questions

    def test_quatrieme_regle_personnalisee(self):
        original = (team.PREVIOUS_SECTION + "4. Le PO valide le texte.\n").encode()
        self.charter.write_bytes(original)
        questions = self.accepter_les_propositions()
        self.assertEqual((bool(questions), self.charter.read_bytes() == original), (False, True))

    def test_migration_de_la_section_apres_un_exemple(self):
        exemple = ("```md\n" + team.PREVIOUS_SECTION + "```\n\n").encode()
        self.charter.write_bytes(exemple + team.PREVIOUS_SECTION.encode())
        self.accepter_les_propositions()
        self.assertTrue(self.charter.read_bytes().startswith(exemple), "L’exemple cité a changé")
        self.assertEqual(team.charter_rules(self.project), team.SECTION.strip())

    def test_migration_avec_fins_de_ligne_mixtes(self):
        debut, fin = b"# Charte\r\n\r\n", b"\r\n## Annexe\r\nFin\r\n"
        self.charter.write_bytes(debut + team.PREVIOUS_SECTION.encode() + fin)
        self.accepter_les_propositions()
        contenu = self.charter.read_bytes()
        decisions = json.loads(self.config.read_bytes()).get("decisions", {})
        self.assertEqual((decisions.get("section_v2"), contenu.startswith(debut), contenu.endswith(fin),
                          team.charter_rules(self.project) == team.SECTION.strip()), ("oui", True, True, True))


class LancementEtArbitrage(test_auto.LoopBase):
    def test_configuration_codex_inexploitable(self):
        (self.state / "question").write_text("Question commune", encoding="utf-8")
        with patch.object(auto, "app_server", return_value={}) as lecture, \
                patch("samdario.cli.os.execvpe") as lancer, redirect_stdout(io.StringIO()):
            samdario.main(["_pane", str(self.state), "codex", "1"])
        arguments = lancer.call_args.args[1]
        remplace_la_consigne = any(arg.startswith("developer_instructions=") for arg in arguments)
        self.assertEqual((lecture.call_count, remplace_la_consigne), (2, False))

    def arbitrage_initial(self, auteur):
        with self.db() as db:
            state.put(db, autostart=1)
            question = "Quelles colonnes exporter ?"
            numero = state.share(db, dict.fromkeys(("claude", "codex"), question))
        demande = state.common(numero, question)
        auto.handle_event(self.state, "claude", test_auto.typed(demande, "c1"), now=0.1)
        auto.handle_event(self.state, "codex", test_auto.taken(demande, "t1"), now=0.2)
        arbitrage = "Exporter les colonnes visibles ou toutes les colonnes ? Je recommande les visibles.\n[DÉCISION]"
        avis = {auteur: arbitrage, state.other(auteur): "Le format CSV convient."}
        with patch.object(auto, "queue_send", return_value="file-1") as envoi, \
                patch.object(auto, "paste_draft") as collage:
            for instant, assistant in enumerate((auteur, state.other(auteur)), start=1):
                if assistant == "claude":
                    attente = auto.handle_event(self.state, assistant, test_auto.stop(avis[assistant], "c1"), now=instant)
                else:
                    auto.handle_event(self.state, assistant, test_auto.done(avis[assistant], "t1", demande), now=instant)
                if instant == 1:
                    self.assertFalse(auto.status(self.state, now=1.1).startswith("PAUSE"))
                    envoi.assert_not_called()

            self.assertIn("lecture croisée", auto.status(self.state, now=3))
            premier = auto.wait(self.state, attente, clock=test_auto.clock(start=4), sleep=self.fail)
            self.assertIsNotNone(premier)
            self.assertTrue(premier.startswith("[Réponse de Codex #"))
            self.assertIn(avis["codex"], premier)
            reveil = 'Stop hook blocking error from command "Stop": ' + premier
            auto.handle_event(self.state, "claude", test_auto.typed(reveil, "c2"), now=5)
            reaction = "Je retiens le CSV pour les colonnes choisies."
            auto.handle_event(self.state, "claude", test_auto.stop(reaction, "c2"), now=6)
            envoi.assert_called_once()
            fil, second, _ = envoi.call_args.args
            self.assertEqual(fil, test_auto.THREAD)
            self.assertTrue(second.startswith("[Réponse de Claude #"))
            self.assertIn(reaction, second)
            self.assertIn(avis["claude"], second)
            auto.handle_event(self.state, "codex", test_auto.taken(second, "t2"), now=7)
            self.assertTrue(auto.status(self.state, now=7.1).startswith("AUTO 2/"))
            auto.handle_event(self.state, "codex", test_auto.done("J’ai lu cet avis.", "t2", second), now=8)
            self.assertEqual(auto.status(self.state, now=9), "PAUSE — décision requise : à toi de trancher")
            envoi.assert_called_once()
        collage.assert_not_called()
        with self.db() as db:
            identifiant, = json.loads(state.get(db, "decision_ids"))
            self.assertEqual(db.execute("SELECT sender, body FROM replies WHERE id = ?", (identifiant,)).fetchone(),
                             (auteur, arbitrage))

    def test_claude_demande_une_decision_en_premier(self):
        self.arbitrage_initial("claude")

    def test_codex_demande_une_decision_en_premier(self):
        self.arbitrage_initial("codex")


if __name__ == "__main__":
    unittest.main()
