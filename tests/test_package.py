"""Tâche 21 : Sam & Dario en paquet installable (plan : docs/superpowers/plans/2026-11-25-tache-21-depot-github.md).

Plusieurs tests lancent `python -I -m samdario` : le paquet doit être installé dans l'environnement qui lance les
tests (`python3 -m pip install -e .` dans un environnement virtuel, voir CONTRIBUTING.md).
"""

import json
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import samdario
from samdario import cli, state

ROOT = Path(__file__).resolve().parent.parent
ISOLATED = [sys.executable, "-I", "-m", "samdario"]
INSTALL_HINT = "paquet non installé dans cet environnement : python3 -m pip install -e . (voir CONTRIBUTING.md)"


def isolated_import_works() -> bool:
    return subprocess.run([sys.executable, "-I", "-c", "import samdario"], capture_output=True).returncode == 0


class PackageTest(unittest.TestCase):
    def test_version_is_the_one_of_pyproject(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
        self.assertIsNotNone(declared)
        self.assertEqual(samdario.__version__, declared.group(1))
        self.assertIn('packages = ["samdario"]', text)  # sans cela, setuptools voit aussi essais/ et work/
        self.assertIn('samdario = "samdario.cli:run"', text)
        self.assertIn('requires-python = ">=3.9"', text)

    def test_hooks_and_buttons_call_the_isolated_module(self):
        self.assertTrue(cli.codex_hook_settings()["command"].startswith(shlex.join(ISOLATED) + " _event codex"))
        self.assertEqual(cli.helper("_scene", Path("/tmp/x"))[:4], ISOLATED)
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                command = " ".join(cli.agent_command(Path("/tmp/x"), agent, "Question"))
                self.assertIn(shlex.join(ISOLATED + ["_event", agent]), command)

    def test_run_reads_the_arguments_and_keeps_the_exit_codes(self):
        with patch.object(sys, "argv", ["samdario", "_status", "/dossier/inexistant"]), \
                patch.object(cli, "main", side_effect=ValueError("panne")) as main, \
                self.assertRaises(SystemExit) as raised:
            cli.run()
        main.assert_called_once_with(["_status", "/dossier/inexistant"])
        self.assertEqual(str(raised.exception.code), "Sam & Dario — panne")
        with patch.object(sys, "argv", ["samdario"]), patch.object(cli, "main", side_effect=KeyboardInterrupt), \
                self.assertRaises(SystemExit) as raised:
            cli.run()
        self.assertEqual(raised.exception.code, 130)

    def test_launch_stops_when_the_isolated_import_fails(self):
        failed = subprocess.CompletedProcess([], 1)
        with patch.object(cli.subprocess, "run", return_value=failed), patch.object(cli, "start") as start, \
                self.assertRaises(SystemExit) as raised:
            cli.main(["--max", "4", "Question"])
        start.assert_not_called()
        self.assertIn("pipx install", str(raised.exception.code))


@unittest.skipUnless(isolated_import_works(), INSTALL_HINT)
class InstalledCommandTest(unittest.TestCase):
    """Le paquet installé, lancé comme le lancent les hooks : en mode isolé, depuis un autre dossier."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="samdario-projet-")
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        # Leurres : un projet qui contient lui-même un samdario.py et un dossier samdario/.
        (self.project / "samdario.py").write_text("open('leurre-lance', 'w').write('oui')\n", encoding="utf-8")
        (self.project / "samdario").mkdir()
        (self.project / "samdario" / "__init__.py").write_text("raise SystemExit('leurre importé')\n")

    def test_version_and_help_answer_from_another_folder(self):
        version = subprocess.run(ISOLATED + ["--version"], cwd=self.project, capture_output=True, text=True)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn(samdario.__version__, version.stdout)
        helped = subprocess.run(ISOLATED + ["--help"], cwd=self.project, capture_output=True, text=True)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("--max", helped.stdout)
        self.assertFalse((self.project / "leurre-lance").exists())

    def test_a_real_hook_records_its_event_despite_the_decoys(self):
        session = Path(tempfile.mkdtemp(prefix="samdario-etat-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(session, ignore_errors=True))
        with state.transaction(session):
            pass  # crée la base
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "fil-1", "turn_id": "t1",
                 "prompt": "Bonjour depuis le hook"}
        result = subprocess.run(ISOLATED + ["_event", "codex"], cwd=self.project, input=json.dumps(event),
                                capture_output=True, text=True, env={"SAMDARIO_STATE": str(session), "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(session / "replies.db")) as db:
            texts = [row[0] for row in db.execute("SELECT text FROM events WHERE agent = 'codex'")]
        self.assertIn("Bonjour depuis le hook", texts)
        self.assertFalse((self.project / "leurre-lance").exists())


class WorkflowTest(unittest.TestCase):
    def test_ci_pins_the_verified_actions_and_covers_both_pythons_and_systems(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for expected in ("actions/checkout@v7", "actions/setup-python@v7", "ubuntu-24.04", "macos-",
                         '"3.9"', '"3.12"', '"3.15"', "tmux", "Pillow>=10.1", "pip install -e .", "ruff check",
                         "unittest discover -s tests -t ."):
            with self.subTest(expected=expected):
                self.assertIn(expected, workflow)
        self.assertNotIn("ubuntu-latest", workflow)  # Ubuntu 26.04 ne fournit plus CPython 3.9


class LinksTest(unittest.TestCase):
    DOCUMENTS = ("README.md", "README.fr.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md",
                 "docs/getting-started.md", "docs/premiers-pas.md")

    @staticmethod
    def anchors(path: Path) -> set:
        found = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            heading = re.match(r"#{1,6} (.+)", line)
            if heading:
                found.add(re.sub(r"[^\w\- ]", "", heading.group(1).strip().lower()).replace(" ", "-"))
        return found

    def test_every_local_link_and_image_resolves(self):
        for name in self.DOCUMENTS:
            document = ROOT / name
            self.assertTrue(document.is_file(), name)
            for target in re.findall(r"\]\(([^)\s]+)\)", document.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                file, _, anchor = target.partition("#")
                destination = (document.parent / file).resolve() if file else document
                with self.subTest(document=name, link=target):
                    self.assertTrue(destination.exists())
                    if anchor:
                        self.assertIn(anchor, self.anchors(destination))


class OldSqliteTest(unittest.TestCase):
    """Le Python 3.9 de python.org pour macOS embarque un SQLite antérieur à 3.39, qui refuse HAVING sans GROUP BY
    (« a GROUP BY clause is required before HAVING »). Le SQLite récent des autres environnements ne le voit pas."""

    def test_no_query_uses_having_without_group_by(self):
        import ast
        offenders = []
        for module in sorted((ROOT / "samdario").glob("*.py")):
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    query = " ".join(node.value.upper().split())
                    if "HAVING " in query and "GROUP BY " not in query:
                        offenders.append(f"{module.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
