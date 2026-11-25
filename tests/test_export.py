import json
import os
import shutil
import sqlite3
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from samdario import export as export
from samdario import cli as samdario
from samdario import rules as rules
from samdario import state as state
from samdario import team as team
from tests.test_state import Base
from tests.test_cli import claude_fixture


REAL_TITLE_EDITABLE = samdario.title_editable  # avant toute simulation (tâche 21)


class ExportBase(Base):
    def setUp(self):
        super().setUp()
        self.project = self.state / "mon projet #1"
        self.project.mkdir()
        self.target = self.project / "docs" / "accords"
        with self.db() as db:
            state.put(db, cwd=self.project)
            team.store(db, {"claude": "Alma", "codex": "Milo"})
        # Tâche 21 : ces tests portent sur la saisie du titre, disponible à partir de tmux 3.6 ; la version
        # réellement installée (3.4 sous Ubuntu 24.04) a son propre test, TitleWithoutPromptTest.
        editable = patch("samdario.cli.title_editable", return_value=True)
        editable.start()
        self.addCleanup(editable.stop)

    def agree(self, question="Que faut-il garder ?", first="Le PDF.", second="Oui pour le PDF."):
        parent = self.turn("codex", question, first + "\n[ACCORD]")
        return self.reply("claude", second + "\n[ACCORD]", self.deliver(parent, "claude"))


class ExportTest(ExportBase):
    def test_manual_agreement_survives_resume_and_still_causes_first_pause(self):
        last = self.agree()
        accord = export.latest(self.state)
        self.assertEqual(accord.question, "Que faut-il garder ?")
        self.assertEqual(UUID(accord.token).version, 4)
        with self.db() as db:
            self.assertEqual(state.get(db, "accord_seen"), str(last))
            self.assertEqual(state.get(db, "accord_pause_seen"), "0")
            rules.toggle(db, 1, True)
            self.assertEqual(state.get(db, "reason"), rules.REASONS["accord"])
            rules.toggle(db, 2, True)
        self.turn("claude", "Autre chose ?", "Nouvelle discussion")
        self.assertEqual(export.latest(self.state), accord)

    def test_only_auto_pause_confirms_and_two_independent_markers_do_not(self):
        self.turn("claude", "Un ?", "Oui\n[ACCORD]")
        self.turn("codex", "Deux ?", "D’accord\n[ACCORD]")
        with self.db() as db:
            state.put(db, auto=rules.ON)
            rules.settle(db, 0)
        self.assertIsNone(export.latest(self.state))
        self.agree()
        self.assertIsNone(export.latest(self.state))
        with self.db() as db:
            rules.settle(db, 1)
        first = export.latest(self.state)
        self.assertIsNotNone(first)
        with self.db() as db:
            state.put(db, auto=rules.ON)
        b = self.agree("Décision encore en discussion ?")
        with self.db() as db:
            state.put(db, accord_dropped=b)
        self.assertEqual(export.latest(self.state), first)

    def test_freezes_first_answer_origin_through_relays_ignoring_unrelated_questions(self):
        with self.db() as db:
            round_id = state.share(db, dict.fromkeys(state.NAMES, "Quel format ?"))
        shared = state.common(round_id, "Quel format ?")
        self.turn("claude", shared, "Voyons les contraintes.")
        start = self.turn("codex", shared, "Commençons par comparer.")
        req = self.deliver(start, "claude")
        self.submit("codex", "Question indépendante")
        parent = self.reply("claude", "Un PDF convient.\n[ACCORD]", req)
        self.reply("codex", "Il s’imprime bien.\n[ACCORD]", self.deliver(parent, "codex"))
        self.assertEqual(export.latest(self.state).question, "Quel format ?")

    def test_annotation_is_the_request_and_unknown_origin_is_explicit(self):
        parent = self.turn("codex", "Question initiale", "Premier avis")
        with self.db() as db:
            draft = state.draft(db, state.Relay(parent, "codex", "claude", "Premier avis"))
        req = self.submit("claude", "Et si le réseau tombe ?\n" + draft)
        parent = self.reply("claude", "Cache local.\n[ACCORD]", req)
        self.reply("codex", "Oui.\n[ACCORD]", self.deliver(parent, "codex"))
        self.assertEqual(export.latest(self.state).question, "Et si le réseau tombe ?")
        missing = self.reply("codex", "On imprime.\n[ACCORD]", self.fresh())
        self.reply("claude", "D’accord.\n[ACCORD]", self.deliver(missing, "claude"))
        self.assertEqual(export.latest(self.state).question, "Demande d’origine non identifiée")

    def test_freezes_two_clients_and_keeps_markdown(self):
        body = "Garder **ceci** et [ACCORD] cité.\n\n- raison\n\n    print(42)"
        self.agree("Pourquoi ?", body, "Ma réserve est le coût.")
        a = export.latest(self.state)
        self.agree("Et demain ?", "Changer", "Très bien")
        b = export.latest(self.state)
        title = "L’équipe #1 / précise l’accord"
        path_b = export.save(self.state, b.token, title)
        path_a = export.save(self.state, a.token, title)
        self.assertEqual((path_b.name, path_a.name), ("l-equipe-1-precise-l-accord.md", "l-equipe-1-precise-l-accord-2.md"))
        self.assertEqual(path_a.parent, self.target)
        md = path_a.read_text()
        self.assertTrue(md.startswith("# " + title + "\n"))
        self.assertIn("## Demande\n\nPourquoi ?", md)
        self.assertIn("## Milo · Codex\n\n" + body, md)
        self.assertIn("## Alma · Claude\n\nMa réserve est le coût.", md)
        self.assertEqual(md.count("[ACCORD]"), 1)
        self.assertNotIn("Et demain ?", md)
        self.assertIn("Et demain ?", path_b.read_text())

    def test_rename_retouched_header_removed_marker_and_concurrent_publication(self):
        self.agree()
        a = export.latest(self.state)
        def save(_):
            return export.save(self.state, a.token, "☕")
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(save, range(4)))
        self.assertEqual(set(paths), {self.target / "accord.md"})
        old = paths[0].rename(self.target / "renommé.md")
        marker = f"<!-- samdario-accord: {a.token} -->"
        old.write_text("## Note manuscrite\n\n" + old.read_text().replace("# ☕", "# Retouché"))
        self.assertEqual(export.existing(a), old)
        old.write_text("~~~html\n" + marker + "\n~~~\n" + old.read_text().replace(marker, "") + "\n" + marker + "\n")
        occupied = self.target / "accord.md"
        occupied.write_text("À moi.")
        self.assertEqual(save(0).name, "accord-2.md")
        self.assertEqual(occupied.read_text(), "À moi.")
        self.assertIn("Note manuscrite", old.read_text())

    def test_isolated_title_turn_has_only_the_frozen_content_and_fails_to_manual(self):
        self.agree()
        a = export.latest(self.state)
        def run(argv, **kw):
            self.assertEqual(argv[0], "claude")
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
            self.assertIn("--safe-mode", argv)
            self.assertIn("--no-session-persistence", argv)
            self.assertIn("--strict-mcp-config", argv)
            self.assertNotIn("SAMDARIO_STATE", kw["env"])
            self.assertNotEqual(Path(kw["cwd"]), self.project)
            self.assertIn("Le PDF.", kw["input"])
            self.assertNotIn(a.token, kw["input"])
            json.loads(kw["input"])
            with self.db() as db:
                self.assertEqual(state.get(db, "auto"), "off")
            return subprocess.CompletedProcess(argv, 0, stdout="Garder le PDF\n")
        with patch.dict(os.environ, SAMDARIO_STATE=str(self.state)), patch("samdario.export.subprocess.run", side_effect=run) as p:
            self.assertEqual(export.suggest(a), "Garder le PDF")
            p.assert_called_once()
        for err in (OSError("absent"), subprocess.TimeoutExpired("claude", 1)):
            with patch("samdario.export.subprocess.run", side_effect=err):
                self.assertEqual(export.suggest(a), "")
        self.assertFalse(self.target.exists())

    def test_actions_target_the_requesting_client_and_freeze_before_the_title_turn(self):
        with patch("samdario.cli.tmux") as mux, patch("samdario.export.suggest") as suggest:
            samdario.main(["_export_accord", str(self.state), "/dev/ttys001"])
            self.assertEqual(mux.call_args.args[-1], "Aucun accord à exporter")
            suggest.assert_not_called()
        self.assertFalse(self.target.exists())
        self.agree("Première question")
        client_a, client_b = "/dev/ttys001", "/dev/ttys002"
        seen = []

        def prompt(folder, client, title):
            self.assertEqual(title, "Titre proposé")
            seen.append(client)
            if client == client_a:
                self.agree("Seconde question")
                samdario.main(["_export_accord", str(folder), client_b])
            return "Titre de " + client[-3:]

        with patch("samdario.cli.tmux") as mux, patch("samdario.cli.title_prompt", side_effect=prompt), \
                patch("samdario.export.suggest", return_value="Titre proposé") as suggest:
            samdario.main(["_export_accord", str(self.state), client_a])
            self.assertEqual(seen, [client_a, client_b])
            self.assertEqual(suggest.call_count, 2)
            for client, question in ((client_a, "Première question"), (client_b, "Seconde question")):
                path = self.target / f"titre-de-{client[-3:]}.md"
                self.assertIn(f"## Demande\n\n{question}\n", path.read_text())
                self.assertIn((self.state, "display-message", "-l", "-c", client, "--", str(path)),
                              [call.args for call in mux.call_args_list])
            samdario.main(["_export_accord", str(self.state), client_b])
            self.assertEqual(suggest.call_count, 2)

    def test_cancellation_bad_or_empty_title_and_no_spontaneous_export(self):
        with self.db() as db:
            state.put(db, auto=rules.ON, max=1, count=1)
        self.opening()
        with patch("samdario.export.suggest") as model, self.db() as db:
            self.assertIn("limite", rules.status_line(db, 0))
            self.assertEqual(state.get(db, "accord_seen"), "0")
            model.assert_not_called()
        self.agree()
        a = export.latest(self.state)
        with patch("samdario.cli.tmux"), patch("samdario.cli.title_prompt", return_value="") as prompt:
            for output in ("", "deux\nlignes", "a" * 161, "\x1b[1mTitre"):
                result = subprocess.CompletedProcess([], 0, stdout=output)
                with patch("samdario.export.subprocess.run", return_value=result):
                    self.assertEqual(samdario.export_accord(self.state, "client"), "")
                    prompt.assert_called_with(self.state, "client", "")
        with patch("samdario.cli.tmux") as mux:
            samdario.main(["_save_accord", str(self.state), "client", a.token, "   "])
            mux.assert_not_called()
            samdario.main(["_save_accord", str(self.state), "client", a.token, "Deux\nlignes"])
            self.assertIn("une seule ligne", mux.call_args.args[-1])
        self.assertFalse(self.target.exists())
        with patch("samdario.cli.tmux"), patch("samdario.export.subprocess.run", side_effect=OSError("Claude indisponible")), \
                patch("samdario.cli.title_prompt", return_value="Choisir le PDF") as prompt:
            path = Path(samdario.export_accord(self.state, "client"))
            prompt.assert_called_once_with(self.state, "client", "")
            self.assertTrue(path.read_text().startswith("# Choisir le PDF\n"))

    def test_confirmed_manual_pair_respects_a_decision_and_rejects_an_inline_marker(self):
        last = self.agree("Avant")
        inline = self.turn("codex", "Exemple ?", "Il suffit d’écrire [ACCORD]")
        self.reply("claude", "Même avis\n[ACCORD]", self.deliver(inline, "claude"))
        with self.db() as db:
            self.assertEqual(state.get(db, "accord_seen"), str(last))
        parent = self.turn("codex", "Suite ?", "Oui\n[ACCORD]")
        req = self.deliver(parent, "claude")
        self.reply("codex", "PDF ou CSV ?\n[DÉCISION]", self.fresh())
        self.reply("claude", "Oui\n[ACCORD]", req)
        with self.db() as db:
            self.assertEqual(state.get(db, "accord_seen"), str(last))
        self.assertEqual(export.latest(self.state).question, "Avant")

    def test_migrating_an_open_session_preserves_the_displayed_pause_and_token(self):
        last = self.agree()
        with self.db() as db:
            db.execute("DROP TABLE agreements")
            db.execute("DELETE FROM meta WHERE key = 'accord_pause_seen'")
            db.execute("PRAGMA user_version = 11")
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.map(lambda _: export.latest(self.state), range(2))
        self.assertEqual(a, b)
        with self.db() as db:
            self.assertEqual(state.get(db, "accord_pause_seen"), str(last))
            self.assertEqual(db.execute("SELECT count(*) FROM agreements").fetchone()[0], 1)
            rules.toggle(db, 0, True)
            self.assertNotEqual(state.get(db, "reason"), rules.REASONS["accord"])

    def test_identical_pairs_in_two_sessions_and_two_agreements_have_distinct_files(self):
        last_a = self.agree()
        a = export.latest(self.state)
        self.agree()
        b = export.latest(self.state)
        first_state = self.state
        self.state = self.state / "autre session"
        self.state.mkdir()
        state.initialize(self.state, cwd=str(self.project))
        self.assertEqual(self.agree(), last_a)
        c = export.latest(self.state)
        self.assertEqual(len({a.token, b.token, c.token}), 3)
        with ThreadPoolExecutor(max_workers=3) as pool:
            paths = list(pool.map(lambda item: export.save(*item, "Un titre"),
                                  [(first_state, a.token), (first_state, b.token), (self.state, c.token)]))
        self.assertEqual({p.name for p in paths}, {"un-titre.md", "un-titre-2.md", "un-titre-3.md"})
        for path, accord in zip(paths, (a, b, c)):
            self.assertIn(accord.token, path.read_text())

    def test_shared_question_after_launch_keeps_its_own_text(self):
        self.opening()
        self.submit("codex", "Question sans rapport")
        with self.db() as db:
            number = state.share(db, dict.fromkeys(state.NAMES, "Comment répondre hors ligne ?"))
        prompt = state.common(number, "Comment répondre hors ligne ?")
        self.turn("claude", prompt, "Je compare.")
        first = self.turn("codex", prompt, "Cache.\n[ACCORD]")
        self.reply("claude", "Ça marche.\n[ACCORD]", self.deliver(first, "claude"))
        self.assertEqual(export.latest(self.state).question, "Comment répondre hors ligne ?")

    def test_a_failed_write_or_a_symlink_does_not_publish_a_partial_export(self):
        self.agree()
        a = export.latest(self.state)
        with patch("samdario.export.os.link", side_effect=OSError("Publication interrompue")), self.assertRaises(OSError):
            export.save(self.state, a.token, "Accord")
        self.assertEqual(list(self.target.iterdir()), [])
        self.target.rmdir()
        outside = self.state / "ailleurs"
        outside.mkdir()
        self.target.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "hors du projet"):
            export.save(self.state, a.token, "Accord")
        self.assertEqual(list(outside.iterdir()), [])
        self.target.unlink()
        inside = self.project / "décisions"
        inside.mkdir()
        self.target.symlink_to(inside, target_is_directory=True)
        path = export.save(self.state, a.token, "L’équipe 👩‍💻")
        self.assertTrue(path.samefile(inside / "l-equipe.md"))
        self.assertEqual(export.existing(a), path)

    @unittest.skipUnless(shutil.which("claude"), "Claude Code requis")
    def test_native_claude_title_cli_with_a_local_model_response(self):
        self.agree()
        a = export.latest(self.state)
        with closing(sqlite3.connect(self.state / "replies.db")) as db:
            before = "\n".join(db.iterdump())
        expected = "Conserver le PDF pour l’impression"
        with patch.dict(os.environ, claude_fixture(self.state, expected), clear=True):
            self.assertEqual(export.suggest(a), expected)
        with closing(sqlite3.connect(self.state / "replies.db")) as db:
            self.assertEqual("\n".join(db.iterdump()), before)
        self.assertFalse(self.target.exists())


class TitleWithoutPromptTest(ExportBase):
    """Tâche 21 : sous tmux 3.4 (Ubuntu 24.04, donc WSL), `command-prompt -l` n'existe pas. L'accord est alors
    enregistré dès l'export, et la barre dit d'où vient le titre et comment le renommer."""

    def test_old_tmux_saves_the_proposed_title_without_a_prompt(self):
        self.agree()
        with patch("samdario.cli.title_editable", return_value=False), \
                patch("samdario.cli.server_version", return_value="3.4"), patch("samdario.cli.tmux") as mux, \
                patch("samdario.cli.title_prompt") as prompt, \
                patch("samdario.export.suggest", return_value="Garder le PDF"):
            samdario.main(["_export_accord", str(self.state), "/dev/ttys001"])
        prompt.assert_not_called()
        path = self.target / "garder-le-pdf.md"
        self.assertTrue(path.read_text(encoding="utf-8").startswith("# Garder le PDF\n"))
        shown = mux.call_args.args[-1]
        self.assertTrue(shown.startswith("tmux 3.4"), shown)  # les versions d'abord : le chemin peut déborder
        self.assertLess(shown.index("3.6"), shown.index(str(path)))
        self.assertIn(str(path), shown)
        self.assertIn("proposé par Claude", shown)
        self.assertIn("tmux 3.4", shown)
        self.assertIn("3.6", shown)

    def test_old_tmux_without_a_proposal_uses_a_neutral_title_not_credited_to_claude(self):
        self.agree()
        with patch("samdario.cli.title_editable", return_value=False), \
                patch("samdario.cli.server_version", return_value="3.4"), patch("samdario.cli.tmux"), \
                patch("samdario.export.suggest", return_value=""):
            result = samdario.export_accord(self.state, "client")
        self.assertTrue((self.target / "accord.md").read_text(encoding="utf-8").startswith("# Accord\n"))
        self.assertIn("accord.md", result)
        self.assertIn("« Accord »", result)
        self.assertNotIn("proposé par Claude", result)

    def test_the_version_is_read_from_the_session_server(self):
        # Le serveur de la session fait foi : un client mis à jour en cours de session ne change rien.
        for output, editable in (("3.4", False), ("3.3a", False), ("3.6", True), ("3.6a", True), ("3.8", True),
                                 ("next-3.7", True), ("4.0", True), ("master", True)):
            with self.subTest(server=output), patch("samdario.cli.tmux", return_value=output) as mux, \
                    patch("samdario.cli.subprocess.run") as client:
                self.assertEqual(REAL_TITLE_EDITABLE(self.state), editable)
                self.assertEqual(mux.call_args.args, (self.state, "display-message", "-p", "#{version}"))
                client.assert_not_called()

    def test_without_an_answer_from_the_server_the_client_version_decides(self):
        for output, editable in (("tmux 3.4\n", False), ("tmux 3.8\n", True)):
            found = subprocess.CompletedProcess([], 0, stdout=output)
            with self.subTest(client=output), patch("samdario.cli.tmux", side_effect=RuntimeError("serveur absent")), \
                    patch("samdario.cli.subprocess.run", return_value=found):
                self.assertEqual(REAL_TITLE_EDITABLE(self.state), editable)
