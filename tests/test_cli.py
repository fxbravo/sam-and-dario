from contextlib import redirect_stdout
import fcntl
import io
import json
import os
import pty
import select
import shlex
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from samdario import cli as samdario
from samdario import rules as rules
from samdario import state as state
from samdario import bar as bar
from samdario import team as team


def claude_fixture(folder: Path, text: str) -> dict:
    """Un flux API local lu par le vrai Claude Code, dans une configuration jetable."""
    message = {"id": "msg_local", "type": "message", "role": "assistant", "model": "local",
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 5, "output_tokens": 0}}
    events = [
        ("message_start", {"message": message}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 8}}),
        ("message_stop", {}),
    ]
    api = folder / "api"
    (api / "v1").mkdir(parents=True)
    stream = "".join(f"event: {name}\ndata: {json.dumps({'type': name, **fields})}\n\n"
                     for name, fields in events)
    (api / "v1" / "messages").write_text(stream)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "TMPDIR") if k in os.environ}
    env.update(SAMDARIO_STATE=str(folder), CLAUDE_CONFIG_DIR=str(folder / "config"),
               ANTHROPIC_BASE_URL=api.as_uri(),
               ANTHROPIC_API_KEY="fixture-locale", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return env



ROOT = Path(__file__).resolve().parent.parent
RUN = [sys.executable, "-m", "samdario"]  # sans installation : la racine du dépôt passe par PYTHONPATH

class LaunchQuestionTest(unittest.TestCase):
    def setUp(self):
        # Tâche 9 : les prénoms sont demandés avant la question ; ces tests portent sur la question seule.
        names = patch("samdario.team.setup", return_value={"claude": "Claude", "codex": "Codex"})
        names.start()
        self.addCleanup(names.stop)
        # Tâche 21 : la vérification d'installation a son propre test (test_package) ; ici, le paquet est supposé
        # importable, même quand subprocess.run est simulé.
        installed = patch("samdario.cli.check_installation")
        installed.start()
        self.addCleanup(installed.stop)

    def test_instructions_name_the_other_assistant_and_the_first_message_is_the_question(self):
        question = "Compare ces deux idées.\nPuis donne ton avis."
        for agent, partner in (("claude", "Codex"), ("codex", "Claude")):
            with self.subTest(agent=agent):
                self.assertEqual(samdario.prompt(question, agent), "[Demande commune #1]\n\n" + question)
                text = samdario.instructions(agent)
                self.assertIn(f"en binôme avec {partner}", text)
                self.assertIn(f"« [Réponse de {partner} #7] »", text)
                self.assertIn("numéro identifie le message", text)
                self.assertIn("Stop hook feedback", text)
                self.assertIn("[Demande commune #n]", text)
                self.assertIn(f"avant de lire l'avis de {partner}", text)
                self.assertIn("exactement [ACCORD]", text)
                self.assertNotIn("AGENTS.md", text)  # sans prénoms, la consigne reste celle d'avant
                self.assertIn("Objection la plus forte : …", text)  # tâche 10 : un accord mis à l'épreuve
                self.assertIn("exactement [DÉCISION], à la place de [ACCORD]", text)

    def test_max_option(self):
        self.assertEqual(samdario.parse(["--max", "3", "question"]).max, 3)
        self.assertEqual(samdario.parse(["question"]).max, 8)
        self.assertEqual(samdario.parse(["question", "--max", "1"]).max, 1)
        for invalid in ("0", "-2", "abc"):
            with self.subTest(invalid=invalid), patch("builtins.input") as read, patch("sys.stderr"):
                with self.assertRaises(SystemExit) as stopped:
                    samdario.parse(["--max", invalid])
                self.assertEqual(stopped.exception.code, 2)
                read.assert_not_called()

    def test_launch_registers_one_number_before_either_pane_starts(self):
        question = "Même question\navec #7 et 'deux options'."
        project = Path("/tmp/projet été #1")
        names = {"claude": "Al#ma", "codex": "Milo"}
        launched, titles, settings = {}, {}, []

        def pane_command(directory, command, *args, data=None):
            settings.append((command, *args))
            if command in ("new-session", "split-window"):
                self.assertEqual(args[args.index("-c") + 1], str(project).replace("#", "##"))
                with state.transaction(directory) as db:
                    self.assertEqual(db.execute("SELECT round, agent, text FROM shared ORDER BY agent").fetchall(),
                                     [(1, "claude", question), (1, "codex", question)])
                    self.assertEqual((state.get(db, "max"), state.get(db, "cwd"), state.get(db, "auto")),
                                     ("3", str(project), "off"))
                    self.assertTrue(rules.autostart_pending(db))  # l'auto démarre après les deux réponses
                    self.assertEqual(team.names_of(db), names)  # prénoms enregistrés avant les panneaux
                with patch("samdario.cli.os.execvpe") as launch:
                    samdario.main(list(args[args.index("_pane"):]))
                agent, argv, env = launch.call_args.args
                self.assertEqual(env["SAMDARIO_STATE"], str(directory))
                self.assertEqual(argv[-2], "--")
                launched[agent] = argv
                return "%0" if command == "new-session" else "%1"
            if command == "set-option" and "@samdario" in args:
                titles[args[args.index("-t") + 1]] = args[-1]
            return ""

        with patch("samdario.cli.tmux", side_effect=pane_command), \
                patch("samdario.cli.shutil.which", return_value="présent"), patch("samdario.cli.subprocess.run"), \
                patch("samdario.cli.Path.cwd", return_value=project), patch("samdario.team.setup", return_value=names), \
                patch("samdario.auto.codex_developer_instructions", return_value="Règle témoin.") as existing:
            samdario.main(["--max", "3", question])
        existing.assert_called_once_with(str(project))  # lue pour Codex seulement, dans le dossier du projet
        option = next(arg for arg in launched["codex"] if arg.startswith("developer_instructions="))
        self.assertTrue(json.loads(option.split("=", 1)[1]).startswith("Règle témoin.\n\n[Sam & Dario] Tu es Milo"))
        for agent in ("claude", "codex"):
            self.assertEqual(launched[agent][-1], state.common(1, question))  # la consigne n'est plus dans le message
            self.assertIn("Tu es Al#ma (Claude)" if agent == "claude" else "Tu es Milo (Codex)",
                          " ".join(launched[agent]))
        self.assertEqual(titles, {"%0": "Al#ma · Claude", "%1": "Milo · Codex"})  # tmux affiche le # tel quel
        # Tâche 11 : la barre « tableau de bord ».
        for expected in (("set-option", "status", "2"), ("set-option", "-w", "pane-border-lines", "heavy"),
                         ("set-option", "-w", "pane-active-border-style", "fg=#{@samdario_colour}"),
                         ("set-option", "-p", "-t", "%0", "@samdario_colour", "colour166"),
                         ("set-option", "-p", "-t", "%1", "@samdario_colour", "colour25"),
                         ("set-option", "-w", "pane-border-format", bar.border_format())):
            self.assertIn(expected, settings)
        second = next(item for item in settings if item[:2] == ("set-option", "status-format[1]"))
        self.assertEqual(second[2], bar.BUTTONS)
        export_key = next(item for item in settings if item[:2] == ("bind-key", "e"))
        self.assertEqual(export_key[2:4], ("run-shell", "-b"))
        self.assertIn("_export_accord", export_key[-1])
        self.assertTrue(export_key[-1].endswith(" #{q:client_name}"))

    def test_codex_keeps_its_native_instructions_when_they_cannot_be_read(self):
        with tempfile.TemporaryDirectory(prefix="samdario-", dir="/tmp") as directory:
            folder = Path(directory)
            state.initialize(folder, cwd=directory)
            (folder / "question").write_text("Question", encoding="utf-8")
            with state.transaction(folder) as db:
                team.store(db, {"claude": "Alma", "codex": "Milo"}, team.SECTION.strip())
            for reads, expected in (([RuntimeError("panne"), "Règle témoin."], True), ([RuntimeError("panne")] * 2, False)):
                with self.subTest(expected=expected), patch("samdario.cli.os.execvpe") as launch, \
                        patch("samdario.auto.codex_developer_instructions", side_effect=reads), \
                        redirect_stdout(io.StringIO()) as out:
                    samdario.main(["_pane", directory, "codex", "1"])
                argv = launch.call_args.args[1]
                options = [arg for arg in argv if arg.startswith("developer_instructions=")]
                if expected:  # une seconde lecture réussit : consigne native et consigne de Sam & Dario ensemble
                    self.assertTrue(json.loads(options[0].split("=", 1)[1]).startswith("Règle témoin.\n\n"))
                else:  # la consigne native n'est jamais remplacée sans avoir été lue
                    self.assertEqual(options, [])
                    self.assertIn("consigne native", out.getvalue())

    def test_incomplete_config_answer_counts_as_unreadable(self):
        with patch("samdario.auto.app_server", return_value={}) as read, redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(samdario.codex_existing("/tmp"))
        self.assertEqual(read.call_count, 2)
        self.assertIn("consigne native", out.getvalue())
        with patch("samdario.auto.app_server", return_value={"config": {}}):
            self.assertEqual(samdario.codex_existing("/tmp"), "")  # configuration lue, sans consigne native

    def test_agent_commands_carry_the_names_and_instructions(self):
        names = {"claude": "Alma", "codex": "Milo"}
        question = "Quel export ?"
        claude = samdario.agent_command(Path("/tmp/etat"), "claude", state.common(1, question), names,
                                       rules=team.SECTION.strip())
        instructions = claude[claude.index("--append-system-prompt") + 1]
        self.assertIn("Tu es Alma (Claude). Tu travailles en binôme avec Milo (Codex).", instructions)
        self.assertIn("Votre charte commune est dans AGENTS.md.", instructions)
        self.assertLess(claude.index("--append-system-prompt"), claude.index("--"))
        self.assertEqual(claude[-1], state.common(1, question))
        codex = samdario.agent_command(Path("/tmp/etat"), "codex", state.common(1, question), names)
        overrides = samdario.codex_hook_settings()["overrides"]
        position = codex.index(overrides[1]) - 1  # le premier « -c » de la liste est celui de notify
        self.assertEqual(codex[position:position + len(overrides)], overrides)  # hooks inchangés
        option = codex[codex.index("-c", position + len(overrides)) + 1]
        key, value = option.split("=", 1)
        self.assertEqual(key, "developer_instructions")
        self.assertIn("Tu es Milo (Codex). Tu travailles en binôme avec Alma (Claude).", json.loads(value))
        self.assertEqual(codex[-2:], ["--", state.common(1, question)])
        self.assertIn("Pour chaque ticket, un membre du binôme modifie le code et l'autre le relit", instructions)
        kept = samdario.agent_command(Path("/tmp/etat"), "codex", "q", names, codex_existing="Règle témoin.")
        merged = json.loads(next(a for a in kept if a.startswith("developer_instructions=")).split("=", 1)[1])
        self.assertTrue(merged.startswith("Règle témoin.\n\n"))  # consigne Codex existante conservée
        self.assertIn("Tu es Milo (Codex).", merged)
        odd = {"claude": 'Al"ma', "codex": "Mi#lo l'Été"}  # guillemets, dièse, apostrophe, accent
        codex = samdario.agent_command(Path("/tmp/etat"), "codex", state.common(1, question), odd)
        option = next(arg for arg in codex if arg.startswith("developer_instructions="))
        self.assertIn("Tu es Mi#lo l'Été (Codex). Tu travailles en binôme avec Al\"ma (Claude).",
                      json.loads(option.split("=", 1)[1]))

    def test_launch_creates_the_scene_window(self):
        settings = []

        def pane_command(directory, command, *args, data=None):
            settings.append((command, *args) + ((data,) if data is not None else ()))
            return {"new-session": "%0", "split-window": "%1"}.get(command, "")

        with patch("samdario.cli.tmux", side_effect=pane_command), \
                patch("samdario.cli.shutil.which", return_value="présent"), patch("samdario.cli.subprocess.run"), \
                patch("samdario.team.setup", return_value={"claude": "Claude", "codex": "Codex"}):
            samdario.main(["Question"])
        commands = [item[0] for item in settings]
        renamed = settings.index(("rename-window", "-t", "samdario:", "terminaux"))
        self.assertGreater(renamed, commands.index("split-window"))  # après la création des deux panneaux
        window = next(item for item in settings if item[0] == "new-window")
        self.assertEqual(window[1:6], ("-d", "-t", "samdario:", "-n", "scene"))
        self.assertIn("_scene", window)
        self.assertIn(("set-option", "-w", "-t", "samdario:scene", "remain-on-exit", "on"), settings)
        toggle = next(item for item in settings if item[:2] == ("bind-key", "v"))
        self.assertIn("select-window -t :terminaux", " ".join(toggle))
        self.assertIn("select-window -t :scene", " ".join(toggle))
        block = b"".join(item[-1] for item in settings if item[:2] == ("source-file", "-")).decode()
        self.assertIn("#{==:#{mouse_status_range},vue}", block)
        self.assertIn("select-window -t :terminaux", block)
        hook = next(item for item in settings if item[0] == "set-hook")
        self.assertEqual(hook[1:4], ("-t", "samdario", "window-unlinked"))
        self.assertIn("@samdario-ended", hook[4])
        ended = next(item for item in settings if item[:3] == ("set-option", "-g", "@samdario-ended"))
        self.assertIn("_ended", ended[3])

    def test_ended_closes_the_session_only_without_the_assistants(self):
        with tempfile.TemporaryDirectory(prefix="samdario état #", dir="/tmp") as directory:
            folder = Path(directory)
            (folder / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
            for live, closed in (("%0\n%1\n%2", False), ("%1\n%2", False), ("%2", True), ("", True)):
                with self.subTest(live=live), patch("samdario.cli.tmux", return_value=live) as mux:
                    samdario.main(["_ended", str(folder)])
                    self.assertEqual(any(call.args[1] == "kill-session" for call in mux.call_args_list), closed)

    def test_ended_keeps_the_session_when_tmux_cannot_list_panes(self):
        with tempfile.TemporaryDirectory(prefix="samdario état #", dir="/tmp") as directory:
            folder = Path(directory)
            (folder / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
            with patch("samdario.cli.tmux", side_effect=RuntimeError("no server running")) as mux:
                samdario.main(["_ended", str(folder)])
            self.assertEqual([call.args[1] for call in mux.call_args_list], ["list-panes"])

    def test_relay_from_the_scene_pane_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="samdario état #", dir="/tmp") as directory:
            folder = Path(directory)
            (folder / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
            with patch("samdario.cli.tmux") as mux, self.assertRaisesRegex(ValueError, "Choisis le panneau"):
                samdario.relay(folder, "%2")
            mux.assert_not_called()

    def test_rejected_tmux_option_does_not_stop_the_launch(self):
        def pane_command(directory, command, *args, data=None):
            if command in ("new-session", "split-window"):
                return "%0" if command == "new-session" else "%1"
            if command == "set-option" and "pane-border-lines" in args:
                raise RuntimeError("invalid option: pane-border-lines")  # tmux plus ancien
            return ""

        with patch("samdario.cli.tmux", side_effect=pane_command), \
                patch("samdario.cli.shutil.which", return_value="présent"), patch("samdario.cli.subprocess.run") as run, \
                patch("samdario.team.setup", return_value={"claude": "Claude", "codex": "Codex"}):
            samdario.main(["Question"])
        run.assert_called()  # le terminal s'est bien attaché à la session

    def test_deletion_key_reprompts_before_starting(self):
        question = "Si tu es Codex, réponds « Codex prêt »."
        with patch("builtins.input", side_effect=["\x7f", " \t\x08 ", question]) as read, \
                patch("samdario.cli.start") as start:
            samdario.main([])
        self.assertEqual(read.call_count, 3)
        start.assert_called_once_with(question, max_relays=8, names={"claude": "Claude", "codex": "Codex"}, rules="")

    def test_control_only_argument_is_rejected_without_prompting(self):
        with patch("builtins.input") as read, patch("samdario.cli.start") as start, patch("sys.stderr"):
            with self.assertRaises(SystemExit) as stopped:
                samdario.main(["\x7f"])
        self.assertEqual(stopped.exception.code, 2)
        read.assert_not_called()
        start.assert_not_called()

    def test_bindings_and_background_status_on_the_private_server(self):
        def pane_command(directory, command, *args, data=None):
            return {"new-session": "%0", "split-window": "%1"}.get(command, "")

        with patch("samdario.cli.tmux", side_effect=pane_command) as mux, \
                patch("samdario.cli.shutil.which", return_value="présent"), patch("samdario.cli.subprocess.run"), \
                patch("samdario.cli.COMMAND", [sys.executable, "/tmp/l'équipe #1/samdario.py"]):
            samdario.start("Question")
            folder = mux.call_args_list[0].args[0]
            bindings = {call.args[2]: call.args[3:] for call in mux.call_args_list if call.args[1] == "bind-key"}
            for key, expected in (("r", samdario.helper("_relay", folder, "#{pane_id}")),
                                  ("R", samdario.helper("_relay", folder, "#{pane_id}", "force")),
                                  ("a", samdario.helper("_toggle", folder))):
                action, background, shell_command = bindings[key]
                self.assertEqual((action, background), ("run-shell", "-b"))
                self.assertIn("##1", shell_command)
                self.assertEqual(shlex.split(shell_command.replace("##", "#")), expected)
            options = {call.args[-2]: call.args[-1] for call in mux.call_args_list if call.args[1] == "set-option"}
            self.assertEqual(options["status-interval"], "1")
            status = shlex.join(samdario.helper("_status", folder)).replace("#", "##")
            self.assertIn(f"#({status})", options["status-format[0]"])
            # Tâche 15 : la seconde ligne de la barre est une rangée de boutons.
            self.assertEqual(options["status-format[1]"], bar.BUTTONS)
            self.assertEqual(options["status"], "2")
            binding = b"".join(call.kwargs["data"] for call in mux.call_args_list if call.args[1:] == ("source-file", "-"))
            self.assertIn(b"bind-key b command-prompt -p 'Pour les deux :'", binding)
            self.assertIn(b"run-shell -b", binding)
            self.assertIn("_both", options["@samdario-both"])

    def test_both_subcommand_preserves_the_whole_argument_including_empty(self):
        question = """  Qu'en dit "l'équipe" ? #1 #{pane_id} %1 `date` $(date) ; fin  """
        folder = Path("/tmp/une session #")
        with patch("samdario.auto.broadcast", return_value="Tour #3") as send, patch("samdario.cli.tmux") as mux:
            for text in (question, ""):
                samdario.main(["_both", str(folder), text])
                send.assert_called_with(folder, text)
                mux.assert_called_with(folder, "display-message", "Tour ##3")


class RelayTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="samdario état #", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        samdario.initialize(self.state)
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        self.codex_home = self.state / "codex"
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), "SAMDARIO_STATE": str(self.state)})
        env.start()
        self.addCleanup(env.stop)

    def register_codex_thread(self, **metadata):
        thread = str(uuid.uuid4())
        day = self.codex_home / "sessions" / "2026" / "11" / "18"
        day.mkdir(parents=True, exist_ok=True)
        rollout = day / f"rollout-2026-11-18T09-00-00-{thread}.jsonl"
        payload = {"id": thread, "source": "cli", "thread_source": "user", **metadata}
        rollout.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
        return thread

    def test_status_writes_one_line_and_toggle_displays_on_this_server(self):
        folder = str(self.state)
        with patch("samdario.auto.alive", return_value={"claude": True, "codex": True}), \
                patch("samdario.auto.codex_hooks_trusted", return_value=True), patch("samdario.cli.tmux") as mux:
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                samdario.main(["_status", folder])
                samdario.main(["_toggle", folder])
                mux.assert_called_once_with(self.state, "display-message", "AUTO 0/8")
                samdario.main(["_status", folder])
                self.assertEqual(output.getvalue(), "OFF\nAUTO 0/8\n")
            samdario.main(["_toggle", folder])
            self.assertEqual(mux.call_args.args, (self.state, "display-message", "OFF"))
            with state.transaction(self.state) as db:
                state.put(db, unidentified=7)
            samdario.main(["_toggle", folder])
            self.assertIn("##7", mux.call_args.args[-1])

    def test_callbacks_and_delivery(self):
        body = "Été, 東京\n\n`$(date)`\t« oui »\r\n\x1b[201~"
        claude = samdario.agent_command(self.state, "claude", "question")
        codex = samdario.agent_command(self.state, "codex", "question")
        hook = json.loads(claude[claude.index("--settings") + 1])["hooks"]["Stop"][0]["hooks"][0]
        notify = json.loads(codex[codex.index("-c") + 1].split("=", 1)[1])
        stop = {"hook_event_name": "Stop", "last_assistant_message": body, "session_id": "a"}
        done = {"type": "agent-turn-complete", "last-assistant-message": "Second avis.",
                "thread-id": self.register_codex_thread(), "turn-id": "1"}
        for event in (dict(stop, hook_event_name="PermissionRequest"), dict(stop, agent_id="child"),
                      dict(stop, last_assistant_message=""),
                      dict(stop, last_assistant_message="Premier avis."), stop):
            subprocess.run(["/bin/sh", "-c", hook["command"]], input=json.dumps(event),
                           text=True, check=True)
        following = {**done, "turn-id": "2", "last-assistant-message": "Suite."}
        for event in (done, done, following):
            subprocess.run([*notify, json.dumps(event)], check=True)
        with closing(sqlite3.connect(self.state / "replies.db")) as db:
            self.assertEqual(db.execute("SELECT body FROM replies ORDER BY id").fetchall(),
                             [("Premier avis.",), (body,), ("Second avis.",), ("Suite.",)])
        expected = [("%0", "Codex", 4, "Second avis.", "Suite."),
                    ("%1", "Claude", 2, "Premier avis.", "Été, 東京\n\n`$(date)`\t« oui »\n\\x1b[201~")]
        for pane, name, number, previous, text in expected:
            with patch("samdario.cli.tmux", side_effect=["0/0", "", ""]) as mux:
                samdario.relay(self.state, pane)
            check, load, paste = mux.call_args_list
            self.assertEqual(check.args[1], "display-message")
            self.assertEqual(load.args[1:3], ("load-buffer", "-b"))
            message = (f"[Réponse de {name} #{number}]\n\nAvant cette réponse, depuis le dernier relais :\n\n"
                       f"— Réponse de {name} :\n{previous}\n\n— Réponse la plus récente de {name} :\n{text}\n\n")
            self.assertEqual(load.kwargs["data"], message.encode())
            self.assertEqual(paste.args[1:], ("paste-buffer", "-p", "-r", "-d", "-b",
                                              load.args[3], "-t", pane))
        # Le rejeu d'un ancien tour déjà écarté ne le remet pas en attente.
        subprocess.run([*notify, json.dumps(done)], check=True)
        for pane, *_ in expected:
            with patch("samdario.cli.tmux", return_value="0/0") as mux:
                self.assertIn("Ctrl-b R", samdario.relay(self.state, pane))
                mux.assert_called_once()

    def test_codex_title_thread_before_and_after_real_answer(self):
        title = '{"title":"Citer un mot français"}'
        real = {"type": "agent-turn-complete", "thread-id": self.register_codex_thread(),
                "turn-id": "1", "last-assistant-message": title}
        naming = {**real, "thread-id": str(uuid.uuid4())}
        child = {**real, "thread-id": self.register_codex_thread(source={"subAgent": "review"})}
        command = samdario.agent_command(self.state, "codex", "Réponds en JSON.")
        notify = json.loads(command[command.index("-c") + 1].split("=", 1)[1])
        subprocess.run([*notify, json.dumps(naming)], check=True)
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertIn("Aucune nouvelle réponse", samdario.relay(self.state, "%0"))
            mux.assert_called_once()
        for event in (real, {**naming, "turn-id": "2"}, child):
            subprocess.run([*notify, json.dumps(event)], check=True)
        with closing(sqlite3.connect(self.state / "replies.db")) as db:
            self.assertEqual(db.execute("SELECT session, body FROM replies").fetchall(),
                             [(real["thread-id"], title)])
        with patch("samdario.cli.tmux", side_effect=["0/0", "", ""]) as mux:
            samdario.relay(self.state, "%0")
        self.assertEqual(mux.call_args_list[1].kwargs["data"], f"[Réponse de Codex #1]\n\n{title}\n\n".encode())

    def test_codex_hook_command_is_fixed(self):
        args = samdario.agent_command(self.state, "codex", "question")
        self.assertEqual(args, samdario.agent_command(self.state / "autre lancement", "codex", "question"))
        self.assertFalse(any(str(self.state) in arg for arg in args))
        command = [*samdario.COMMAND, "_event", "codex"]
        self.assertEqual(json.loads(args[args.index("-c") + 1].split("=", 1)[1]), command)
        fixed = json.dumps(shlex.join(command), ensure_ascii=False)
        for event in ("UserPromptSubmit", "PermissionRequest"):
            self.assertIn(f'hooks.{event}=[{{hooks=[{{type="command",command={fixed}}}]}}]', args)

    def test_event_without_state_is_silent(self):
        env = {key: value for key, value in os.environ.items() if key != "SAMDARIO_STATE"}
        for args in (["claude"], ["codex", "invalid JSON"]):
            result = subprocess.run([*RUN, "_event", *args], env=dict(env, PYTHONPATH=str(ROOT)),
                                    input="invalid JSON", text=True, capture_output=True)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_fixed_hook_supplies_context_only_to_a_new_user_thread(self):
        original, new = self.register_codex_thread(), self.register_codex_thread()
        child = self.register_codex_thread(thread_source="subagent")
        with state.transaction(self.state) as db:
            number = state.share(db, dict.fromkeys(state.NAMES, "Choisir un nom"))
            state.record_reply(db, state.Event("codex", "reply", original, "t1", "Safran.",
                                               state.common(number, "Choisir un nom"), "t1"))
        command = shlex.split(samdario.codex_hook_settings()["command"])

        def run(thread):
            raw = {"hook_event_name": "UserPromptSubmit", "session_id": thread, "turn_id": "t2", "prompt": "Suite ?"}
            result = subprocess.run(command, input=json.dumps(raw), text=True, capture_output=True, timeout=5, check=True)
            self.assertEqual(result.stderr, "")
            return result.stdout

        self.assertEqual(run(child), "")
        output = json.loads(run(new))["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        for expected in ("exactement [ACCORD]", "Choisir un nom", "Safran."):
            self.assertIn(expected, output["additionalContext"])
        self.assertEqual(run(new), "")

    def test_pane_sets_state_and_observer_records_codex_permission(self):
        (self.state / "question").write_text("observer", encoding="utf-8")
        bin_dir = self.state / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "codex"
        # Un chemin d’interpréteur avec espace (pipx : « Application Support ») ne tient pas dans un shebang.
        stub.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        'print(json.dumps([os.environ.get("SAMDARIO_STATE"), sys.argv[1:]]))\n')
        stub.chmod(0o755)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], SAMDARIO_STATE="ancien lancement")
        pane = subprocess.run([*RUN, "_pane", str(self.state), "codex"],
                              cwd=self.state, env=dict(env, PYTHONPATH=str(ROOT)), check=True, capture_output=True, text=True)
        directory, args = json.loads(pane.stdout)
        self.assertEqual(Path(directory).resolve(), self.state.resolve())
        self.assertEqual(args[-2:], ["--", "observer"])
        env["SAMDARIO_STATE"] = directory
        callback = json.loads(args[args.index("-c") + 1].split("=", 1)[1])
        for name in ("UserPromptSubmit", "PermissionRequest"):
            raw = {"hook_event_name": name, "session_id": "s", "turn_id": "t", "prompt": "question"}
            result = subprocess.run(callback, input=json.dumps(raw), env=env, text=True, capture_output=True)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            with state.transaction(self.state) as db:
                expected = "busy" if name == "UserPromptSubmit" else "permission"
                self.assertEqual(state.get(db, "codex_state"), expected)
        with closing(sqlite3.connect(self.state / "replies.db")) as db:
            self.assertEqual(db.execute("SELECT kind, turn, text FROM events ORDER BY id").fetchall(),
                             [("submitted", "t", "question"), ("permission", "t", "")])
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone(), (0,))

    def test_open_prototype_session_keeps_its_drafts(self):
        old = self.state / "ancienne session"
        old.mkdir()
        (old / "panes.json").write_text('{"%1": "codex"}')
        with closing(sqlite3.connect(old / "replies.db")) as db, db:
            db.execute("CREATE TABLE replies(id INTEGER PRIMARY KEY, sender TEXT NOT NULL, session TEXT NOT NULL, "
                       "turn TEXT, body TEXT NOT NULL, pasted INTEGER NOT NULL DEFAULT 0, UNIQUE(sender, session, turn))")
            db.execute("INSERT INTO replies VALUES(7, 'claude', 's', NULL, 'déjà collé', 1)")
        with patch("samdario.cli.tmux", return_value="0/0"):
            self.assertIn("Ctrl-b R", samdario.relay(old, "%1"))
        stop = {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "suite"}
        subprocess.run([*RUN, "_capture", str(old), "claude"], env=dict(os.environ, PYTHONPATH=str(ROOT)),
                       input=json.dumps(stop), check=True, text=True)
        with patch("samdario.cli.tmux", side_effect=["0/0", "", ""]) as mux:
            samdario.relay(old, "%1")
        expected = ("[Réponse de Claude #8]\n\nAvant cette réponse, depuis le dernier relais :\n\n"
                    "— Réponse de Claude :\ndéjà collé\n\n— Réponse la plus récente de Claude :\nsuite\n\n")
        self.assertEqual(mux.call_args_list[1].kwargs["data"], expected.encode())

    def test_failed_paste_keeps_reply(self):
        for text in ("dépassé", "encore disponible"):
            samdario.capture(self.state, "claude", {"hook_event_name": "Stop", "session_id": "a",
                                                   "last_assistant_message": text})
        with patch("samdario.cli.tmux", side_effect=["0/0", "", RuntimeError("panneau fermé"), ""]):
            with self.assertRaisesRegex(RuntimeError, "panneau fermé"):
                samdario.relay(self.state, "%1")
        with patch("samdario.cli.tmux", side_effect=["0/0", "", "", "0/0"]) as mux:
            self.assertIn("collée", samdario.relay(self.state, "%1"))
            self.assertIn("Ctrl-b R", samdario.relay(self.state, "%1"))
        self.assertIn(b"encore disponible", mux.call_args_list[1].kwargs["data"])

    def test_manual_relay_statuses(self):
        thread = self.register_codex_thread()
        samdario.capture(self.state, "claude", {"hook_event_name": "Stop", "session_id": "s",
                                               "last_assistant_message": "Proposition"})
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertIn("collée", samdario.relay(self.state, "%1"))
            first_paste = mux.call_args_list[1].kwargs["data"]
            mux.reset_mock()
            self.assertIn("Ctrl-b R", samdario.relay(self.state, "%1"))
            mux.assert_called_once()
            mux.reset_mock()
            samdario.main(["_relay", str(self.state), "%1", "force"])
            self.assertEqual(mux.call_args_list[1].kwargs["data"], first_paste)
            self.assertEqual(mux.call_args_list[2].args[1], "paste-buffer")
        with state.transaction(self.state) as db:
            self.assertEqual(db.execute("SELECT status FROM replies").fetchone(), (state.DRAFTED,))
            state.mark(db, 1, state.QUEUED, "q1")
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertIn("déjà en file", samdario.relay(self.state, "%1", force=True))
            mux.assert_called_once()
        prompt = first_paste.decode() + "Et les tests ?"
        samdario.capture(self.state, "codex", {"hook_event_name": "UserPromptSubmit", "session_id": thread,
                                              "turn_id": "t", "prompt": prompt})
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertIn("Aucune nouvelle réponse", samdario.relay(self.state, "%1", force=True))
            mux.assert_called_once()
        samdario.capture(self.state, "codex", {"type": "agent-turn-complete", "thread-id": thread, "turn-id": "t",
                                              "last-assistant-message": "D'accord", "input-messages": [prompt]})
        with state.transaction(self.state) as db:
            self.assertEqual(db.execute("SELECT answers FROM replies WHERE sender = 'codex'").fetchone(), (1,))
            self.assertEqual((state.get(db, "awaiting"), state.get(db, "codex_state")), ("", "idle"))

    def test_uncertain_delivery_can_be_pasted_again_on_request(self):
        self.register_codex_thread()
        samdario.capture(self.state, "claude", {"hook_event_name": "Stop", "session_id": "s",
                                               "last_assistant_message": "Proposition"})
        with state.transaction(self.state) as db:
            db.execute("UPDATE replies SET status = ?", (state.UNCERTAIN,))
        with patch("samdario.cli.tmux", return_value="0/0") as mux:
            self.assertIn("Remise incertaine. Ctrl-b R", samdario.relay(self.state, "%1"))
            mux.assert_called_once()  # aucun collage sans demande explicite
            self.assertIn("collée", samdario.relay(self.state, "%1", force=True))
        with state.transaction(self.state) as db:
            self.assertEqual(db.execute("SELECT status FROM replies").fetchone(), (state.DRAFTED,))

    def test_claude_stop_hook_runs_in_the_background_and_can_wake_claude(self):
        command = samdario.agent_command(self.state, "claude", "question")
        hooks = json.loads(command[command.index("--settings") + 1])["hooks"]
        fixed = shlex.join([*samdario.COMMAND, "_event", "claude"])
        self.assertEqual(hooks["Stop"], [{"hooks": [{"type": "command", "command": fixed, "asyncRewake": True,
                                                    "timeout": 3660}]}])
        self.assertEqual(hooks["UserPromptSubmit"], [{"hooks": [{"type": "command", "command": fixed}]}])
        # Un tour qui échoue (limite d'usage, erreur de l'API) ne passe pas par Stop : il faut l'entendre aussi.
        self.assertEqual(hooks["StopFailure"], [{"hooks": [{"type": "command", "command": fixed}]}])

    def test_end_of_turn_waits_for_a_relay_and_wakes_claude_with_exit_code_two(self):
        stop = json.dumps({"hook_event_name": "Stop", "session_id": "s", "prompt_id": "p",
                           "last_assistant_message": "Proposition"})
        (self.state / samdario.CHANNELS).write_text("{}")
        with patch.dict(os.environ, {"SAMDARIO_STATE": str(self.state)}), patch("sys.stdin", io.StringIO(stop)), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                patch("samdario.auto.wait", return_value="[Réponse de Codex #2]\n\nR1") as wait:
            with self.assertRaises(SystemExit) as stopped:
                samdario.main(["_event", "claude"])
        self.assertEqual((stopped.exception.code, stderr.getvalue()), (2, "[Réponse de Codex #2]\n\nR1"))
        wait.assert_called_once_with(self.state, 1)

    def test_session_launched_before_the_channels_never_waits(self):
        stop = json.dumps({"hook_event_name": "Stop", "session_id": "s", "prompt_id": "p",
                           "last_assistant_message": "Proposition"})
        with patch.dict(os.environ, {"SAMDARIO_STATE": str(self.state)}), patch("sys.stdin", io.StringIO(stop)), \
                patch("samdario.auto.wait") as wait:
            samdario.main(["_event", "claude"])  # hook ordinaire d'une ancienne session : capter, puis rendre la main
        wait.assert_not_called()
        with state.transaction(self.state) as db:
            self.assertEqual(db.execute("SELECT body FROM replies").fetchall(), [("Proposition",)])

    @unittest.skipUnless(shutil.which("claude"), "Claude Code requis")
    def test_native_claude_stop(self):
        # Ce flux local fait terminer un vrai tour du CLI avec une réponse déterministe.
        text = "Un premier paragraphe.\n\nUn deuxième."
        env = claude_fixture(self.state, text)
        # Le prompt_id émis par le vrai CLI doit rattacher son Stop à la remise du brouillon.
        source = state.Event("codex", "reply", self.register_codex_thread(), "t1", "Donne la réponse de test.", "", "t1")
        with state.transaction(self.state) as db:
            reply_id = state.record_reply(db, source)
            draft = state.draft(db, state.Relay(reply_id, "codex", "claude", source.text))
        prompt = draft + "\n\nPrécise le résultat."
        command = samdario.agent_command(self.state, "claude", prompt)
        result = subprocess.run([command[0], "-p", "--setting-sources", "", "--strict-mcp-config",
                                 "--tools", "", "--no-session-persistence", *command[1:]],
                                cwd=self.state, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.state / "replies.db")) as db:
            self.assertEqual(db.execute("SELECT body, answers FROM replies WHERE sender = 'claude'").fetchall(),
                             [(text, reply_id)])
            status, request = db.execute("SELECT status, confirm_request FROM replies WHERE id = ?", (reply_id,)).fetchone()
            self.assertEqual(status, state.SENT)
            self.assertTrue(request)
            self.assertEqual(db.execute("SELECT kind, text FROM events WHERE agent = 'claude' ORDER BY id").fetchall(),
                             [("relay", prompt), ("submitted", "Précise le résultat."), ("reply", text)])
            claude_id, = db.execute("SELECT id FROM replies WHERE sender = 'claude'").fetchone()
            relayed = state.compose(db, state.Relay(claude_id, "claude", "codex", text))
            self.assertIn("— Demande de l'utilisateur à Claude :\nPrécise le résultat.", relayed)
            self.assertNotIn(source.text, relayed)

    @unittest.skipUnless(shutil.which("tmux"), "tmux requis")
    def test_real_tmux_paste(self):
        # Le terminal récepteur enregistre tous les octets, y compris une éventuelle touche Entrée.
        receiver = """
import os, select, sys, time, tty
from pathlib import Path
tty.setraw(0)
os.write(1, b'\\x1b[?2004hREADY')
data = bytearray()
while data.count(b'\\x1b[201~') < 2:
    data.extend(os.read(0, 65536))
while select.select([0], [], [], 0.3)[0]:
    data.extend(os.read(0, 65536))
Path(sys.argv[1]).write_bytes(data)
time.sleep(10)
"""
        output = self.state / "received"
        pane = self.start_private_tmux(sys.executable, "-c", receiver, str(output))
        (self.state / "panes.json").write_text(json.dumps({pane: "codex"}))
        self.wait_until(lambda: "READY" in samdario.tmux(self.state, "capture-pane", "-p", "-t", pane))
        for text in ("Prêt ?\n\nOui.", "Second message."):
            samdario.capture(self.state, "claude", {"hook_event_name": "Stop", "session_id": "a",
                                                   "last_assistant_message": text})
            samdario.relay(self.state, pane)
        self.wait_until(output.exists)
        expected = ("\x1b[200~[Réponse de Claude #1]\n\nPrêt ?\n\nOui.\n\n\x1b[201~"
                    "\x1b[200~[Réponse de Claude #2]\n\nAvant cette réponse, depuis le dernier relais :\n\n"
                    "— Réponse de Claude :\nPrêt ?\n\nOui.\n\n— Réponse la plus récente de Claude :\n"
                    "Second message.\n\n\x1b[201~")
        self.assertEqual(output.read_bytes(), expected.encode())

    def start_private_tmux(self, *command):
        try:
            pane = samdario.tmux(self.state, "new-session", "-d", "-P", "-F", "#{pane_id}", *command)
        except RuntimeError as exc:
            if "error creating" in str(exc) and "Operation not permitted" in str(exc):
                self.skipTest(str(exc))
            raise
        self.addCleanup(subprocess.run, samdario.tmux_command(self.state, "kill-server"), capture_output=True)
        return pane

    def test_launch_binds_each_button(self):
        sent = []
        with patch("samdario.cli.tmux", side_effect=lambda directory, *args, data=None: sent.append((args, data)) or ""):
            samdario.bind_buttons(self.state)
        block = b"".join(data for _, data in sent if data).decode()
        options = {args[2]: args[3] for args, _ in sent if args[:2] == ("set-option", "-g")}
        for name, expected in (("vue", "select-window -t :terminaux"), ("auto", "#{E:@samdario-cmd-auto}"),
                               ("deux", "command-prompt -p 'Pour les deux :'"),
                               ("coller", "#{E:@samdario-cmd-coller}"), ("recoller", "#{E:@samdario-cmd-recoller}"),
                               ("exporter", "#{E:@samdario-cmd-exporter}"),
                               ("fermer", 'confirm-before -c o -p "Fermer Sam & Dario ? (o/n)" detach-client')):
            with self.subTest(button=name):
                branch = block.index(f"#{{==:#{{mouse_status_range}},{name}}}")
                self.assertIn(expected, block[branch:branch + 400])
        for event in ("MouseDown1Status", "DoubleClick1Status", "TripleClick1Status"):
            self.assertIn(f"bind-key -n {event} {{", block)  # un clic rapide n'est jamais perdu
        self.assertIn("_toggle", options["@samdario-cmd-auto"])
        self.assertTrue(options["@samdario-cmd-coller"].endswith('"#{pane_id}"'))
        self.assertTrue(options["@samdario-cmd-recoller"].endswith('"#{pane_id}" force'))
        self.assertIn("_export_accord", options["@samdario-cmd-exporter"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux requis")
    def test_real_tmux_clicks_on_the_auto_and_both_buttons(self):
        self.start_private_tmux("sleep", "60")
        log = self.state / "inputs"
        recorder = self.state / "enregistreur.py"
        recorder.write_text("import json, sys\nfrom pathlib import Path\n"
                            "with (Path(sys.argv[2]) / 'inputs').open('a') as f:\n"
                            "    print(json.dumps(sys.argv[1:]), file=f)\n")
        with patch("samdario.cli.COMMAND", [sys.executable, str(recorder)]):
            samdario.bind_broadcast(self.state)
            samdario.bind_buttons(self.state)
        for option, value in (("mouse", "on"), ("status", "2"), ("status-format[0]", "SAMDARIO TEST"),
                              ("status-format[1]", bar.BUTTONS), ("@samdario-auto", "off")):
            samdario.tmux(self.state, "set-option", option, value)
        master, screen = self.attach_private_client()
        screen(b"Pour les deux")
        os.write(master, b"\x1b[<0;12;30M\x1b[<0;12;30m")  # « ▶ Auto », second bouton
        self.wait_until(lambda: log.exists() and "_toggle" in log.read_text())
        # Clic rapproché du précédent : tmux le traite en double-clic, lié lui aussi aux boutons.
        os.write(master, b"\x1b[<0;22;30M\x1b[<0;22;30m")  # « ✉ Pour les deux », troisième bouton
        screen(b"Pour les deux :")
        os.write(master, "Une question".encode() + b"\r")
        self.wait_until(lambda: "Une question" in log.read_text())
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["_toggle", str(self.state)], ["_both", str(self.state), "Une question"]])

    @unittest.skipUnless(shutil.which("tmux"), "tmux requis")
    def test_real_tmux_both_prompt_preserves_text_empty_enter_and_ctrl_c(self):
        self.start_private_tmux("sleep", "60")
        log = self.state / "inputs"
        recorder = self.state / "l'avis %1 #1.py"
        recorder.write_text("import json, sys\nfrom pathlib import Path\n"
                            "with (Path(sys.argv[2]) / 'inputs').open('a') as f:\n"
                            "    print(json.dumps(sys.argv[1:]), file=f)\n")
        with patch("samdario.cli.COMMAND", [sys.executable, str(recorder)]):
            samdario.bind_broadcast(self.state)
        samdario.tmux(self.state, "set-option", "status-format[0]", "SAMDARIO TEST")
        samdario.tmux(self.state, "set-option", "assume-paste-time", "0")
        master, screen = self.attach_private_client()
        screen(b"SAMDARIO TEST")
        question = """  Qu'en dit "l'équipe" ? \\ #1 #{pane_id} %1 ; $(date)  """
        for text, key in ((question, b"\r"), ("Je garde le tour", b"\x03"), ("", b"\r")):
            os.write(master, b"\x02b")
            screen(b"Pour les deux")
            os.write(master, text.encode() + key)
            screen(b"SAMDARIO TEST")
            if text == question:
                self.wait_until(lambda: log.exists() and log.read_text().endswith("\n"))
        self.wait_until(lambda: len(log.read_text().splitlines()) >= 2)
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["_both", str(self.state), text] for text in (question, "")])

    @unittest.skipUnless(shutil.which("tmux") and shutil.which("claude"), "tmux et Claude Code requis")
    def test_real_tmux_exports_a_resumed_agreement_and_keeps_the_edited_title(self):
        proposed = """Valider l'avis, #1 / #{pane_id} %Y \\ $HOME"""
        with patch.dict(os.environ, claude_fixture(self.state, proposed), clear=True):
            self.start_private_tmux("sleep", "90")
        directory = tempfile.TemporaryDirectory(prefix="samdario projet ", dir="/tmp")
        self.addCleanup(directory.cleanup)
        project = Path(directory.name)
        state.initialize(self.state, cwd=str(project))
        thread = self.register_codex_thread()
        question = "Demander confirmation avant de supprimer une liste ?"
        parent = {"type": "agent-turn-complete", "thread-id": thread, "turn-id": "1",
                  "input-messages": [question], "last-assistant-message": "Oui, la suppression est définitive.\n[ACCORD]"}
        samdario.capture(self.state, "codex", parent)
        with state.transaction(self.state) as db:
            text = state.draft(db, state.Relay(1, "codex", "claude", parent["last-assistant-message"]))
        turn = dict(session_id="session-claude", prompt_id="demande-1")
        samdario.capture(self.state, "claude", dict(turn, hook_event_name="UserPromptSubmit", prompt=text))
        samdario.capture(self.state, "claude", dict(turn, hook_event_name="Stop",
                                                  last_assistant_message="Oui, pour éviter une perte.\n[ACCORD]"))
        samdario.capture(self.state, "codex", dict(parent, **{"turn-id": "2", "input-messages": ["La suite ?"],
                                                            "last-assistant-message": "Autre discussion."}))
        samdario.bind_export(self.state)
        samdario.tmux(self.state, "set-option", "status-format[0]", "SAMDARIO TEST")
        samdario.tmux(self.state, "set-option", "assume-paste-time", "0")
        master, screen = self.attach_private_client()
        screen(b"SAMDARIO TEST")
        dest = project / "docs" / "accords"
        if not samdario.title_editable(self.state):
            # Tâche 21 : avant tmux 3.6 (Ubuntu 24.04), pas de champ de titre ; l'accord est enregistré dès l'export,
            # sous le titre proposé, gardé tel quel.
            os.write(master, b"\x02e")
            self.wait_until(lambda: dest.exists() and list(dest.glob("*.md")))
            samdario.tmux(self.state, "kill-server")
            path, = dest.glob("*.md")
            md = path.read_text(encoding="utf-8")
            self.assertTrue(md.startswith("# " + proposed + "\n"))
            self.assertIn(question, md)
            self.assertNotIn("Autre discussion", md)
            return
        for cancel in (b"\x03", b"\x15\r"):
            os.write(master, b"\x02e")
            screen(proposed.encode())
            os.write(master, cancel)
            screen(b"SAMDARIO TEST")
            self.assertFalse(dest.exists())
        os.write(master, b"\x02e")
        screen(proposed.encode())
        suffix = """ · L’équipe "A" %1 ; $(date)"""
        os.write(master, suffix.encode() + b"\r")
        self.wait_until(lambda: dest.exists() and list(dest.glob("*.md")))
        samdario.tmux(self.state, "kill-server")
        path, = dest.glob("*.md")
        md = path.read_text(encoding="utf-8")
        self.assertTrue(md.startswith("# " + proposed + suffix + "\n"))
        self.assertIn(question, md)
        self.assertIn("la suppression est définitive", md)
        self.assertIn("pour éviter une perte", md)
        self.assertNotIn("Autre discussion", md)

    def attach_private_client(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        client = subprocess.Popen(samdario.tmux_command(self.state, "attach-session"), stdin=slave, stdout=slave,
                                  stderr=slave, env=env)
        self.addCleanup(client.wait, 5)
        self.addCleanup(client.terminate)

        def screen(expected):
            data = bytearray()
            deadline = time.monotonic() + 5
            while expected not in data:
                if not select.select([master], [], [], max(0, deadline - time.monotonic()))[0]:
                    self.fail(f"Le terminal tmux n'a pas affiché {expected!r}")
                data.extend(os.read(master, 16384))

        return master, screen

    def wait_until(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Délai dépassé dans le test du terminal.")
            time.sleep(0.03)


if __name__ == "__main__":
    unittest.main()

