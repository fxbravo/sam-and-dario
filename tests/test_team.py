"""Tâche 9 : une équipe par projet. Prénoms, charte et premier lancement dans le terminal."""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from samdario import team as team
from samdario.messages import NAMES

TEAM = {"claude": "Alma", "codex": "Milo"}


class TeamBase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="samdario projet été ")
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        self.said = []

    def file(self, name):
        return self.project / name

    def setup_with(self, *answers):
        replies = iter(answers)
        asked = []

        def ask(question):
            asked.append(question)
            reply = next(replies)
            if reply is KeyboardInterrupt:
                raise KeyboardInterrupt
            return reply

        names = team.setup(self.project, ask=ask, say=self.said.append)
        return names, asked


class TeamTest(TeamBase):
    def test_valid_team_file_gives_the_two_names(self):
        self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
        self.assertEqual(team.read_team(self.project), TEAM)
        self.assertIsNone(team.read_team(self.project / "absent"))

    def test_invalid_team_files_are_reported(self):
        cases = {
            "{pas du json": "illisible",
            "[]": "illisible",
            '{"claude": "Alma"}': "Prénom de Codex manquant",
            '{"claude": "", "codex": "Milo"}': "Prénom de Claude manquant",
            json.dumps({"claude": "A" * 31, "codex": "Milo"}): "Prénom de Claude trop long (30 caractères au plus)",
            json.dumps({"claude": "Al\nma", "codex": "Milo"}): "Prénom de Claude sur une seule ligne",
            json.dumps({"claude": "Alma", "codex": "alma"}): "Les deux prénoms doivent être différents",
            json.dumps({"claude": "Codex", "codex": "Milo"}): "sont les noms des outils",
            json.dumps({"claude": "Alma", "codex": "claude"}): "sont les noms des outils",
        }
        for content, message in cases.items():
            with self.subTest(content=content):
                self.file(team.TEAM_FILE).write_text(content, encoding="utf-8")
                with self.assertRaises(team.TeamError) as raised:
                    team.read_team(self.project)
                self.assertIn(message, str(raised.exception))
                if message == "illisible":
                    self.assertIn(".samdario.json", str(raised.exception))

    def test_setup_in_a_new_project_creates_both_files(self):
        names, asked = self.setup_with("Alma", "Milo")
        self.assertEqual(names, TEAM)
        self.assertEqual(asked, ["Prénom de Claude : ", "Prénom de Codex : "])
        self.assertEqual(json.loads(self.file(team.TEAM_FILE).read_text(encoding="utf-8")), TEAM)
        self.assertEqual(self.file(team.CHARTER).read_text(encoding="utf-8"), team.TEMPLATE)
        self.assertFalse(self.file(team.CLAUDE_FILE).exists())
        self.assertIn("Pour qui construisons-nous cette application ?", team.TEMPLATE)
        self.assertIn("Quel résultat voulons-nous obtenir ?", team.TEMPLATE)
        self.assertTrue(team.TEMPLATE.endswith(team.SECTION))

    def test_setup_appends_the_section_only_with_consent(self):
        original = "# Règles\n- pnpm\n"
        for answer, expected in (("o", True), ("n", False)):
            with self.subTest(answer=answer):
                self.file(team.CHARTER).write_text(original, encoding="utf-8")
                self.file(team.TEAM_FILE).unlink(missing_ok=True)
                _, asked = self.setup_with("Alma", "Milo", answer)
                self.assertIn("section « Travail en binôme »", asked[-1])
                charter = self.file(team.CHARTER).read_text(encoding="utf-8")
                if expected:
                    self.assertTrue(charter.startswith(original))
                    self.assertTrue(charter.endswith(team.SECTION))
                else:
                    self.assertEqual(charter, original)

    def test_setup_adds_the_import_only_with_consent(self):
        self.file(team.CHARTER).write_text(team.TEMPLATE, encoding="utf-8")
        for original, answer, expected in (("# Projet", "o", "# Projet\n@AGENTS.md\n"),
                                           ("# Projet\n", "oui", "# Projet\n@AGENTS.md\n"),
                                           ("# Projet\n", "n", "# Projet\n")):
            with self.subTest(original=original, answer=answer):
                self.file(team.CLAUDE_FILE).write_text(original, encoding="utf-8")
                self.file(team.TEAM_FILE).unlink(missing_ok=True)
                _, asked = self.setup_with("Alma", "Milo", answer)
                self.assertIn("@AGENTS.md", asked[-1])
                self.assertEqual(self.file(team.CLAUDE_FILE).read_text(encoding="utf-8"), expected)
        self.file(team.CLAUDE_FILE).write_text("# Projet\n@AGENTS.md\n", encoding="utf-8")
        self.file(team.TEAM_FILE).unlink()
        _, asked = self.setup_with("Alma", "Milo")
        self.assertEqual(len(asked), 2)  # déjà présente : seule la question des prénoms

    def test_existing_team_file_asks_nothing(self):
        self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
        names, asked = self.setup_with()
        self.assertEqual((names, asked, self.said), (TEAM, [], []))
        self.assertFalse(self.file(team.CHARTER).exists())  # l'initialisation n'a lieu qu'une fois

    def test_invalid_team_file_is_reported_and_kept(self):
        self.file(team.TEAM_FILE).write_text("{pas du json", encoding="utf-8")
        names, _ = self.setup_with("Alma", "Milo")
        self.assertEqual(names, TEAM)
        self.assertTrue(any(".samdario.json" in line for line in self.said))
        self.assertEqual(self.file(team.TEAM_FILE).read_text(encoding="utf-8"), "{pas du json")
        self.assertFalse(self.file(team.CHARTER).exists())

    def test_empty_first_name_skips_the_team(self):
        names, asked = self.setup_with("")
        self.assertEqual((names, len(asked)), (dict(NAMES), 1))
        self.assertEqual(list(self.project.iterdir()), [])

    def test_invalid_answers_are_asked_again(self):
        names, asked = self.setup_with("Claude", "Alma", "Alma", "Milo")
        self.assertEqual(names, TEAM)
        self.assertEqual(asked, ["Prénom de Claude : ", "Prénom de Claude : ", "Prénom de Codex : ",
                                 "Prénom de Codex : "])
        self.assertTrue(any("noms des outils" in line for line in self.said))
        self.assertTrue(any("différents" in line for line in self.said))

    def test_title(self):
        self.assertEqual(team.title("claude", TEAM), "Alma · Claude")
        self.assertEqual(team.title("codex", {"claude": "Al#ma", "codex": "Mi'lo"}), "Mi'lo · Codex")
        self.assertEqual(team.title("claude", dict(NAMES)), "Claude")

    # --- Relecture de Codex (tâche 9)

    def test_charter_rules_come_from_the_section_or_the_default(self):
        self.assertEqual(team.charter_rules(self.project), team.SECTION.strip())  # pas de charte
        self.file(team.CHARTER).write_text("# Règles\n- pnpm\n", encoding="utf-8")
        self.assertEqual(team.charter_rules(self.project), team.SECTION.strip())  # charte sans section
        own = f"{team.SECTION_TITLE}\n\n1. Tout passe par une revue.\n"
        self.file(team.CHARTER).write_text(f"# Règles\n\n{own}\n## Annexe\nAutre chose\n", encoding="utf-8")
        self.assertEqual(team.charter_rules(self.project), own.strip())

    def test_charter_warnings_name_the_tool_that_will_not_read_it(self):
        cases = {
            "CLAUDE.local.md": ("Claude", "CLAUDE.local.md"),
            ".claude/CLAUDE.md": ("Claude", ".claude/CLAUDE.md"),
            "AGENTS.override.md": ("Codex", "AGENTS.override.md"),
        }
        for name, (tool, shown) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as other:
                project = Path(other)
                (project / team.CHARTER).write_text(team.TEMPLATE, encoding="utf-8")
                (project / name).parent.mkdir(exist_ok=True)
                (project / name).write_text("# Instructions\n", encoding="utf-8")
                warnings = team.charter_warnings(project)
                self.assertEqual(len(warnings), 1)
                self.assertIn(tool, warnings[0])
                self.assertIn(shown, warnings[0])
        self.file(team.CHARTER).write_text(team.TEMPLATE, encoding="utf-8")
        self.assertEqual(team.charter_warnings(self.project), [])
        self.file(team.CLAUDE_FILE).write_text("# P\n```\n@AGENTS.md\n```\n", encoding="utf-8")
        self.assertEqual(len(team.charter_warnings(self.project)), 1)  # import cité dans un bloc de code
        self.file(team.CLAUDE_FILE).write_text("# P\n@AGENTS.md\n", encoding="utf-8")
        self.assertEqual(team.charter_warnings(self.project), [])
        nested = self.project / "sous" / "projet"
        nested.mkdir(parents=True)
        (nested / team.CHARTER).write_text(team.TEMPLATE, encoding="utf-8")
        self.assertIn("dossier parent", team.charter_warnings(nested)[0])  # CLAUDE.md hérité

    def test_interrupted_setup_asks_the_pending_proposal_again(self):
        self.file(team.CLAUDE_FILE).write_text("# Projet\n", encoding="utf-8")
        with self.assertRaises(KeyboardInterrupt):
            self.setup_with("Alma", "Milo", KeyboardInterrupt)  # quitter à la question @AGENTS.md
        self.assertEqual(team.read_team(self.project), TEAM)
        names, asked = self.setup_with("o")
        self.assertEqual((names, len(asked)), (TEAM, 1))
        self.assertIn("@AGENTS.md", asked[0])
        self.assertIn("@AGENTS.md", self.file(team.CLAUDE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(self.setup_with()[1], [])  # décidé : plus reposé

    def test_refusal_is_remembered(self):
        self.file(team.CLAUDE_FILE).write_text("# Projet\n", encoding="utf-8")
        self.setup_with("Alma", "Milo", "n")
        self.assertEqual(self.setup_with()[1], [])
        self.assertEqual(self.file(team.CLAUDE_FILE).read_text(encoding="utf-8"), "# Projet\n")

    def test_names_saved_by_another_launch_are_kept(self):
        other = {"claude": "Nora", "codex": "Simon"}

        def ask(question):
            if question == "Prénom de Codex : ":  # pendant ce temps, un autre terminal finit son initialisation
                self.file(team.TEAM_FILE).write_text(json.dumps(other), encoding="utf-8")
                return "Milo"
            return "Alma"

        self.assertEqual(team.setup(self.project, ask=ask, say=self.said.append), other)
        self.assertEqual(json.loads(self.file(team.TEAM_FILE).read_text(encoding="utf-8")), other)
        self.assertTrue(any("Nora" in line for line in self.said))

    def test_appending_keeps_the_existing_bytes_and_line_endings(self):
        charter, claude = b"# R\r\n- pnpm\r\n", b"# P\r\nfin"
        self.file(team.CHARTER).write_bytes(charter)
        self.file(team.CLAUDE_FILE).write_bytes(claude)
        self.setup_with("Alma", "Milo", "o", "o")
        new_charter = self.file(team.CHARTER).read_bytes()
        self.assertTrue(new_charter.startswith(charter))
        self.assertTrue(new_charter.endswith(team.SECTION.replace("\n", "\r\n").encode()))
        self.assertEqual(self.file(team.CLAUDE_FILE).read_bytes(), claude + b"\r\n@AGENTS.md\r\n")

    def test_claude_md_linked_to_the_charter_gets_no_import(self):
        self.file(team.CHARTER).write_text("# Règles\n", encoding="utf-8")
        self.file(team.CLAUDE_FILE).symlink_to(team.CHARTER)
        _, asked = self.setup_with("Alma", "Milo", "n")
        self.assertEqual(len(asked), 3)  # la section seulement : CLAUDE.md est déjà la charte
        self.assertEqual(self.file(team.CHARTER).read_text(encoding="utf-8"), "# Règles\n")

    # --- Tâche 10 : revue triée dans la charte

    def test_section_carries_the_sorted_review(self):
        for part in ("À corriger", "À reporter", "Décision requise", "tests qu'il a lui-même exécutés",
                     "n'est jamais reporté", "ne remonte que des constats vérifiés"):
            self.assertIn(part, team.SECTION)
        self.assertNotEqual(team.SECTION, team.PREVIOUS_SECTION)

    def test_previous_default_section_is_offered_an_update_once(self):
        before, after = "# Charte\n\n", "\n## Annexe\nAutre chose\n"
        for answer, expected in (("o", team.SECTION), ("n", team.PREVIOUS_SECTION)):
            with self.subTest(answer=answer):
                self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
                self.file(team.CHARTER).write_text(before + team.PREVIOUS_SECTION + after, encoding="utf-8")
                _, asked = self.setup_with(answer)
                self.assertEqual(len(asked), 1)
                self.assertIn("ancienne version", asked[0])
                self.assertEqual(self.file(team.CHARTER).read_text(encoding="utf-8"), before + expected + after)
                self.assertEqual(self.setup_with()[1], [])  # réponse retenue
        own = team.PREVIOUS_SECTION.replace("Pour chaque ticket", "Pour chaque demande")
        self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
        self.file(team.CHARTER).write_text(own, encoding="utf-8")
        self.assertEqual(self.setup_with()[1], [])  # une section adaptée par le projet n'est jamais remplacée

    # --- Seconde relecture de Codex (tâche 9)

    def test_charter_rules_ignore_headings_inside_code_blocks(self):
        self.file(team.CHARTER).write_text(
            "# Doc\n```md\n## Travail en binôme\n1. Exemple montré.\n```\n\n"
            "## Travail en binôme\n\n1. Vraie règle.\n```text\n## Aperçu\n```\n2. Suite.\n\n"
            "# Annexe\nNe pas transmettre.\n", encoding="utf-8")
        self.assertEqual(team.charter_rules(self.project),
                         "## Travail en binôme\n\n1. Vraie règle.\n```text\n## Aperçu\n```\n2. Suite.")

    def concurrent(self, inner_answer, outer_answer):
        """Deux lanceurs à la même proposition : l'autre répond pendant que celui-ci attend encore."""
        self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
        self.file(team.CHARTER).write_text("# Règles\n", encoding="utf-8")

        def ask(question):
            team.setup(self.project, ask=lambda other_question: inner_answer, say=lambda line: None)
            return outer_answer

        team.setup(self.project, ask=ask, say=self.said.append)
        return self.file(team.CHARTER).read_text(encoding="utf-8")

    def test_two_launchers_apply_a_proposal_once(self):
        self.assertEqual(self.concurrent("o", "o").count(team.SECTION_TITLE), 1)
        self.assertTrue(any("déjà" in line for line in self.said))

    def test_a_late_answer_never_overwrites_a_recorded_refusal(self):
        self.assertEqual(self.concurrent("n", "o"), "# Règles\n")
        decisions = json.loads(self.file(team.TEAM_FILE).read_text(encoding="utf-8"))["decisions"]
        self.assertEqual(decisions["section"], "non")

    def test_charter_is_created_before_the_names_are_saved(self):
        def ask(question):
            if question == "Prénom de Codex : ":
                self.assertFalse(self.file(team.TEAM_FILE).exists())
                return "Milo"
            return "Alma"

        original = team._create

        def created(project, names):  # la charte existe déjà quand les prénoms sont enregistrés
            self.assertEqual(self.file(team.CHARTER).read_text(encoding="utf-8"), team.TEMPLATE)
            return original(project, names)

        team._create = created
        try:
            self.assertEqual(team.setup(self.project, ask=ask, say=self.said.append), TEAM)
        finally:
            team._create = original

    def test_charter_warnings_follow_the_files_claude_really_loads(self):
        cases = {
            "import relatif depuis .claude": ({".claude/CLAUDE.md": "@../AGENTS.md\n"}, None),
            "import en ligne": ({"CLAUDE.md": "Voir @AGENTS.md pour la charte.\n"}, None),
            "import d'un autre fichier": ({".claude/CLAUDE.md": "@AGENTS.md\n", ".claude/AGENTS.md": "x\n"},
                                          ".claude/CLAUDE.md"),
            "import dans du code en ligne": ({"CLAUDE.md": "Écris `@AGENTS.md` pour importer.\n"}, "CLAUDE.md"),
            "CLAUDE.local.md du parent": ({"../CLAUDE.local.md": "# Parent\n"}, "dossier parent"),
            ".claude/CLAUDE.md du parent": ({"../.claude/CLAUDE.md": "# Parent\n"}, "dossier parent"),
        }
        for label, (files, warned) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as root:
                project = Path(root) / "projet"
                project.mkdir()
                (project / team.CHARTER).write_text(team.TEMPLATE, encoding="utf-8")
                for name, content in files.items():
                    path = (project / name).resolve()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                warnings = team.charter_warnings(project)
                if warned is None:
                    self.assertEqual(warnings, [])
                else:
                    self.assertEqual(len(warnings), 1)
                    self.assertIn(warned, warnings[0])

    def test_user_level_claude_memory_does_not_hide_the_charter(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".claude").mkdir()
            (Path(home) / ".claude" / "CLAUDE.md").write_text("@RTK.md\n", encoding="utf-8")
            project = Path(home) / "Dev" / "projet"
            project.mkdir(parents=True)
            (project / team.CHARTER).write_text(team.TEMPLATE, encoding="utf-8")
            with unittest.mock.patch("samdario.team.Path.home", return_value=Path(home)):
                self.assertEqual(team.charter_warnings(project), [])

    # --- Troisième relecture de Codex (tâches 9 et 10)

    def test_tilde_and_long_fences_hide_examples(self):
        section = "## Travail en binôme\nRègle du projet."
        for example in ("~~~md\n## Travail en binôme\nExemple.\n~~~\n\n",
                        "````markdown\n```md\n## Travail en binôme\nExemple.\n```\n````\n\n"):
            with self.subTest(example=example[:5]):
                self.file(team.CHARTER).write_text(example + section, encoding="utf-8")
                self.assertEqual(team.charter_rules(self.project), section)
        self.file(team.CHARTER).write_text(team.TEMPLATE, encoding="utf-8")
        self.file(team.CLAUDE_FILE).write_text("~~~md\n@AGENTS.md\n~~~\n", encoding="utf-8")
        self.assertEqual(len(team.charter_warnings(self.project)), 1)  # import cité entre tildes

    def test_update_replaces_only_the_real_previous_section(self):
        example = "```md\n" + team.PREVIOUS_SECTION + "```\n\n"
        self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
        self.file(team.CHARTER).write_text(example + team.PREVIOUS_SECTION, encoding="utf-8")
        self.setup_with("o")
        charter = self.file(team.CHARTER).read_text(encoding="utf-8")
        self.assertTrue(charter.startswith(example))
        self.assertEqual(team.charter_rules(self.project), team.SECTION.strip())

    def test_customized_previous_section_is_never_offered(self):
        own = team.PREVIOUS_SECTION + "4. Le PO valide le texte.\n"
        self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
        self.file(team.CHARTER).write_text(own, encoding="utf-8")
        self.assertEqual(self.setup_with()[1], [])

    def test_update_keeps_mixed_line_endings_around_the_section(self):
        before, after = b"# Charte\r\n\r\n", b"\r\n## Annexe\r\nFin\r\n"
        self.file(team.TEAM_FILE).write_text(json.dumps(TEAM), encoding="utf-8")
        self.file(team.CHARTER).write_bytes(before + team.PREVIOUS_SECTION.encode() + after)
        self.setup_with("o")
        charter = self.file(team.CHARTER).read_bytes()
        self.assertTrue(charter.startswith(before) and charter.endswith(after))
        self.assertEqual(team.charter_rules(self.project), team.SECTION.strip())


if __name__ == "__main__":
    unittest.main()

