"""Canaux du mode auto pris un à un : `codex queue`, `codex app-server`, tmux, hooks validés."""

import gc
import json
import subprocess
import sys
import time
import unittest
import warnings
from unittest.mock import patch

from samdario import auto as auto
from samdario import state as state
from tests.test_auto import THREAD, temporary_state


class ChannelTest(unittest.TestCase):
    def test_queue_send_reads_the_message_id(self):
        accepted = subprocess.CompletedProcess([], 0, "Queued message 01a1f07a-6151 for thread fil-codex.\n", "")
        with patch("samdario.auto.subprocess.run", return_value=accepted) as run:
            self.assertEqual(auto.queue_send(THREAD, "texte", "/tmp/projet à #moi"), "01a1f07a-6151")
            self.assertEqual(run.call_args.args[0], ["codex", "queue", "--thread", THREAD, "--message", "texte"])
            self.assertEqual(run.call_args.kwargs["cwd"], "/tmp/projet à #moi")

    def test_queue_send_tells_a_refusal_from_an_unreadable_answer(self):
        refused = subprocess.CompletedProcess([], 1, "", "erreur")
        with patch("samdario.auto.subprocess.run", return_value=refused):
            with self.assertRaises(RuntimeError) as failure:
                auto.queue_send(THREAD, "texte", "/tmp")
            self.assertNotIsInstance(failure.exception, auto.UncertainSend)  # rien n'est parti
        unreadable = subprocess.CompletedProcess([], 0, "sortie inattendue", "")
        with patch("samdario.auto.subprocess.run", return_value=unreadable):
            with self.assertRaises(auto.UncertainSend):  # le message est peut-être parti
                auto.queue_send(THREAD, "texte", "/tmp")
        with patch("samdario.auto.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 60)):
            with self.assertRaises(auto.UncertainSend):
                auto.queue_send(THREAD, "texte", "/tmp")

    def test_queue_cancel_reports_what_the_app_server_answers(self):
        with patch("samdario.auto.app_server", return_value={"deleted": True}) as call:
            self.assertTrue(auto.queue_cancel(THREAD, "q1", "/tmp"))
            call.assert_called_once_with("thread/queue/delete", {"threadId": THREAD, "queuedSubmissionId": "q1"},
                                         cwd="/tmp")
        with patch("samdario.auto.app_server", return_value={"deleted": False}):
            self.assertFalse(auto.queue_cancel(THREAD, "q1", "/tmp"))
        with patch("samdario.auto.app_server", side_effect=RuntimeError("app-server muet")):
            self.assertIsNone(auto.queue_cancel(THREAD, "q1", "/tmp"))  # ni retrait ni réception prouvés

    def test_queue_holds_checks_the_identifier_and_the_relay_header(self):
        header = "[Réponse de Claude #7]"
        mine = {"id": "q1", "clientUserMessageId": "m", "input": [{"type": "text", "text": header + "\n\nC1"}]}
        foreign = dict(mine, input=[{"type": "text", "text": "Un message de l'utilisateur"}])
        pages = [{"data": [dict(mine, id="q0")], "nextCursor": "c2"}, {"data": [mine], "nextCursor": None}]
        with patch("samdario.auto.app_server", side_effect=pages) as call:
            self.assertTrue(auto.queue_holds("fil-B", "q1", header, "/tmp"))
            self.assertEqual(call.call_args_list[1].args, ("thread/queue/list", {"threadId": "fil-B", "cursor": "c2"}))
        for answer in ({"data": [foreign]}, {"data": []}, {"data": "illisible"}, RuntimeError("app-server muet")):
            with self.subTest(answer=answer), patch("samdario.auto.app_server", side_effect=[answer]):
                self.assertFalse(auto.queue_holds("fil-B", "q1", header, "/tmp"))

    def test_app_server_reads_an_answer_sent_with_the_previous_line(self):
        # L'accusé d'initialisation et la réponse arrivent en une seule écriture.
        lines = [json.dumps({"id": 0, "result": {}}), json.dumps({"id": 1, "result": {"deleted": True}})]
        server = ("import sys, time\nsys.stdout.write(" + repr("\n".join(lines) + "\n") + ")\n"
                  "sys.stdout.flush()\ntime.sleep(30)\n")
        with patch("samdario.auto.APP_SERVER", [sys.executable, "-c", server]), \
                patch("samdario.auto.COMMAND_SECONDS", 5):
            started = time.monotonic()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                self.assertEqual(auto.app_server("thread/queue/delete", {}), {"deleted": True})
                gc.collect()
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_alive_reads_the_panes_from_tmux(self):
        with temporary_state() as folder:
            (folder / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
            with patch("samdario.auto.tmux", return_value="%0 0\n%1 1"):
                self.assertEqual(auto.alive(folder), {"claude": True, "codex": False})  # panneau mort
            with patch("samdario.auto.tmux", return_value="%0 0"):
                self.assertEqual(auto.alive(folder), {"claude": True, "codex": False})  # panneau fermé
            with patch("samdario.auto.tmux", side_effect=RuntimeError("pas de serveur")):
                self.assertEqual(auto.alive(folder), {"claude": False, "codex": False})

    def test_both_codex_hooks_must_be_trusted_with_the_command_samdario_launched(self):
        command = "python3 /chemin/samdario.py _event codex"

        def listing(first, second, used=command, enabled=True):
            return {"data": [{"hooks": [{"eventName": "userPromptSubmit", "trustStatus": first, "command": used,
                                         "enabled": enabled},
                                        {"eventName": "permissionRequest", "trustStatus": second, "command": used,
                                         "enabled": True}]}]}

        with temporary_state() as folder:
            (folder / "codex-hooks.json").write_text(json.dumps({"command": command, "overrides": ["-c", "x=1"]}))
            state.initialize(folder, cwd="/tmp/projet")
            for answer, expected in ((listing("trusted", "trusted"), True),
                                     (listing("trusted", "untrusted"), False),
                                     (listing("trusted", "trusted", enabled=False), False),  # validé mais désactivé
                                     (listing("trusted", "trusted", used="autre commande"), False),
                                     ({"data": []}, False)):
                with patch("samdario.auto.app_server", return_value=answer) as call:
                    self.assertEqual(auto.codex_hooks_trusted(folder), expected)
                    call.assert_called_once_with("hooks/list", {"cwds": ["/tmp/projet"]}, cwd="/tmp/projet",
                                                 overrides=["-c", "x=1"])
            with patch("samdario.auto.app_server", side_effect=RuntimeError("app-server muet")):
                self.assertFalse(auto.codex_hooks_trusted(folder))


if __name__ == "__main__":
    unittest.main()
