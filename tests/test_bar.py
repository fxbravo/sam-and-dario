"""Tâche 11 : la barre « tableau de bord » (option B des maquettes)."""

import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch

from samdario import cli as samdario

from samdario import auto as auto
from samdario import bar as bar
from samdario import rules as rules
from samdario import state as state
from samdario import team as team
from tests.test_auto import LoopBase, taken, typed

RUN = "#[bg=colour28,fg=colour255,bold]"


def chip(colour, text):
    return f"#[bg={colour},fg=colour255,bold] {text} #[default]"


class MarkupTest(unittest.TestCase):
    def test_family_of_each_status(self):
        cases = {
            "OFF": "off",
            "AUTO 3/8": "run",
            "AUTO — démarre après les deux réponses": "run",
            f"PAUSE — {rules.REASONS['accord']}": "accord",
            f"PAUSE — {rules.REASONS['decision']}": "decision",
            f"PAUSE — {rules.REASONS['limite']}": "user",
            f"PAUSE — {rules.REASONS['hooks']}": "user",
            f"PAUSE — {rules.REASONS['manuel']}": "user",
            "PAUSE — brouillon en attente chez Claude : Entrée, ou Ctrl-b R pour le recoller": "user",
            f"PAUSE — {rules.REASONS['incertain']}": "risk",
            f"PAUSE — {rules.REASONS['suivi']}": "risk",
            f"PAUSE — {rules.REASONS['long']}": "risk",
            f"PAUSE — {rules.REASONS['echec']}": "risk",
            "PAUSE — réponse au relais #4 non identifiée": "risk",
            "PAUSE — raison d'une version plus ancienne": "user",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(bar.family(status), expected)

    def test_running_status_shows_chip_dots_last_relay_and_waiting(self):
        markup = bar.status_markup("AUTO 3/8", bar.Progress(3, 8, ("Milo", "Alma", 12), "Alma"))
        self.assertTrue(markup.startswith(chip("colour28", "AUTO")))
        self.assertIn("●●●", markup)
        self.assertIn("○○○○○", markup)
        self.assertIn("3/8", markup)
        self.assertIn("dernier relais : Milo → Alma (##12)", markup)
        self.assertIn("en attente : Alma", markup)

    def test_cues_become_amber_blocks(self):
        markup = bar.status_markup("AUTO 1/8 · Entrée chez Claude · tour de table : en file chez Codex",
                                   bar.Progress(1, 8, None, None))
        self.assertIn(chip("colour94", "Entrée chez Claude"), markup)
        self.assertIn(chip("colour94", "tour de table : en file chez Codex"), markup)

    def test_pause_shows_its_reason_in_its_colour(self):
        nothing = bar.Progress(0, 8, None, None)
        for reason, colour in ((rules.REASONS["decision"], "colour55"), (rules.REASONS["incertain"], "colour124"),
                               (rules.REASONS["accord"], "colour22"), (rules.REASONS["limite"], "colour94")):
            with self.subTest(reason=reason):
                markup = bar.status_markup(f"PAUSE — {reason}", nothing)
                self.assertTrue(markup.startswith(chip(colour, "PAUSE")))
                self.assertIn(reason, markup)

    def test_off_and_launch_states(self):
        nothing = bar.Progress(0, 8, None, None)
        off = bar.status_markup("OFF", nothing)
        self.assertTrue(off.startswith(chip("colour242", "OFF")))
        self.assertIn("auto coupé", off)
        launch = bar.status_markup("AUTO — démarre après les deux réponses · tour de table : en cours", nothing)
        self.assertTrue(launch.startswith(chip("colour28", "AUTO")))
        self.assertIn("démarre après les deux réponses", launch)
        self.assertIn(chip("colour94", "tour de table : en cours"), launch)

    def test_large_limit_counts_in_figures_only(self):
        markup = bar.status_markup("AUTO 5/20", bar.Progress(5, 20, None, None))
        self.assertIn("5/20", markup)
        self.assertNotIn("●", markup)

    def test_hash_in_texts_is_doubled(self):
        markup = bar.status_markup("PAUSE — réponse au relais #4 non identifiée",
                                   bar.Progress(0, 8, ("Al#ma", "Milo", 7), None))
        self.assertIn("relais ##4", markup)
        running = bar.status_markup("AUTO 1/8", bar.Progress(1, 8, ("Al#ma", "Milo", 7), None))
        self.assertIn("Al##ma → Milo (##7)", running)

    def test_border_format_takes_styles_from_pane_options(self):
        layout = bar.border_format()
        for part in ("#{@samdario_colour}", "#{@samdario_activity}", "#{?pane_active,●,○}", "#{@samdario}"):
            self.assertIn(part, layout)

    def test_pause_tells_what_to_do(self):
        nothing = bar.Progress(0, 8, None, None)
        for reason, hint in ((rules.REASONS["accord"], "tape ta prochaine demande pour relancer"),
                             (rules.REASONS["limite"], "tape ta demande ou ^b a"),
                             (rules.REASONS["decision"], "réponds dans l’un des panneaux")):
            with self.subTest(reason=reason):
                self.assertIn(f"{reason} │ {hint}", bar.status_markup(f"PAUSE — {reason}", nothing))

    def test_activity_text_keeps_the_terminal_colour(self):
        self.assertIn("#[fg=default,bg=default,nobold] #{@samdario_activity}", bar.border_format())

    def test_buttons_line_has_one_range_per_action(self):
        # Tâche 15 : la seconde ligne de la barre devient une rangée de boutons.
        for name in ("vue", "auto", "deux", "coller", "recoller", "exporter", "fermer"):
            with self.subTest(button=name):
                start = bar.BUTTONS.index(f"#[range=user|{name}]")
                self.assertIn("#[norange]", bar.BUTTONS[start:])
        self.assertIn("Pour les deux", bar.BUTTONS)
        self.assertNotIn("Aux deux", bar.BUTTONS)
        self.assertNotIn("panneau", bar.BUTTONS)
        self.assertLess(bar.BUTTONS.index("#[align=right]"), bar.BUTTONS.index("#[range=user|fermer]"))
        self.assertIn("#{?#{==:#{@samdario-auto},on}, ⏸ Pause , ▶ Auto }", bar.BUTTONS)

    def test_paste_buttons_are_hidden_in_the_scene(self):
        hidden = bar.BUTTONS.index("#{?#{==:#{window_name},scene},,")
        for name in ("coller", "recoller", "exporter"):
            with self.subTest(button=name):
                self.assertGreater(bar.BUTTONS.index(f"#[range=user|{name}]"), hidden)
        self.assertLess(bar.BUTTONS.index("#[range=user|vue]"), hidden)


class BaseReadingTest(LoopBase):
    def test_progress_reads_last_relay_and_waiting_from_the_base(self):
        waiter = self.launch()
        auto.toggle(self.state, now=3.0)
        woke = auto.wait(self.state, waiter, clock=lambda: 4.0, sleep=lambda seconds: None)  # R1 (n° 2) en route
        with self.db() as db:
            self.assertIsNone(bar.progress(db, team.names_of(db)).last)  # Claude ne l'a pas encore reçu
        auto.handle_event(self.state, "claude", typed('Stop hook blocking error from command "Stop": ' + woke, "k2"),
                          now=4.1)
        with self.db() as db:
            self.assertEqual(bar.progress(db, team.names_of(db)), bar.Progress(1, 8, ("Codex", "Claude", 2), "Claude"))
            names = {"claude": "Alma", "codex": "Milo"}
            team.store(db, names)
            self.assertEqual(bar.progress(db, team.names_of(db)), bar.Progress(1, 8, ("Milo", "Alma", 2), "Alma"))

    def test_activity_of_each_assistant(self):
        self.launch()
        with self.db() as db:
            self.assertEqual(bar.activity(db, 2.0), {"claude": "attend le relais", "codex": "attend le relais"})
        auto.handle_event(self.state, "claude", typed("Demande", "k5"), now=3.0)
        auto.handle_event(self.state, "codex", taken("Autre", "t5"), now=3.5)
        with self.db() as db:
            self.assertEqual(bar.activity(db, 4.0), {"claude": "rédige sa réponse", "codex": "rédige sa réponse"})
            state.record_permission(db, state.Event("codex", "permission", "fil-codex", "t5", "", "", "t5"))
            self.assertEqual(bar.activity(db, 4.0)["codex"], "autorisation demandée")

    def test_pasted_relay_shows_enter_expected(self):
        self.launch()
        auto.toggle(self.state, now=3.0)
        auto.toggle(self.state, now=3.5)  # OFF : l'attente de Claude est annulée
        with patch("samdario.auto.paste_draft"):
            auto.toggle(self.state, now=4.0)  # le relais est collé chez Claude
        with self.db() as db:
            self.assertEqual(bar.activity(db, 5.0)["claude"], "Entrée attendue")


class ButtonFlagsTest(LoopBase):
    """Tâche 15 : libellé du bouton d'auto et visibilité des boutons, tenus par le rafraîchissement de la barre."""

    def flags(self, now=5.0):
        status = auto.status(self.state, now)
        with self.db() as db:
            return bar.button_flags(db, status)

    def test_button_flags_follow_the_state(self):
        self.launch()
        auto.toggle(self.state, now=3.0)
        on = self.flags()
        self.assertEqual((on["@samdario-auto"], on["@samdario-coller"], on["@samdario-recoller"]), ("on", "", ""))
        auto.toggle(self.state, now=3.5)  # OFF
        off = self.flags()
        self.assertEqual((off["@samdario-auto"], off["@samdario-coller"]), ("off", "1"))
        with patch("samdario.auto.paste_draft"):
            auto.toggle(self.state, now=4.0)  # le relais est collé chez Claude et attend son Entrée
        self.assertEqual(self.flags()["@samdario-recoller"], "1")
        with self.db() as db:
            state.put(db, auto=state.PAUSE, reason=rules.REASONS["incertain"])
        uncertain = self.flags()
        self.assertEqual((uncertain["@samdario-auto"], uncertain["@samdario-recoller"]), ("pause", "1"))

    def test_export_button_follows_the_last_confirmed_agreement(self):
        self.launch()
        self.assertEqual(self.flags()["@samdario-exportable"], "")
        with self.db() as db:
            state.confirm_agreement(db, 2)
        self.assertEqual(self.flags()["@samdario-exportable"], "1")
        auto.toggle(self.state, now=6.0)  # la reprise de l'échange garde l'accord exportable
        self.assertEqual(self.flags()["@samdario-exportable"], "1")

    def test_click_on_auto_updates_its_label_at_once(self):
        # Essai de l'utilisateur (23 novembre) : « ⏸ Pause » ne s'affichait pas après le clic sur « ▶ Auto ».
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        (self.state / bar.MARKER).touch()  # session lancée avec la barre « tableau de bord »
        self.launch()
        sent = []
        with patch.object(bar, "tmux", side_effect=lambda folder, *args: sent.append(args) or ""), \
                patch("samdario.cli.tmux", side_effect=lambda folder, *args, **kwargs: sent.append(args) or ""), \
                patch("samdario.auto.paste_draft"):
            samdario.main(["_toggle", str(self.state)])
        self.assertIn(("set-option", "@samdario-auto", "on"), sent)  # sans attendre le rafraîchissement de la barre
        self.assertIn(("refresh-client", "-S"), sent)
        self.assertLess(sent.index(("set-option", "@samdario-auto", "on")), sent.index(("refresh-client", "-S")))

    def test_dashboard_updates_button_options_only_when_they_change(self):
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        self.launch()
        with patch.object(bar, "tmux") as mux:
            bar.dashboard(self.state, now=5.0)
            bar.dashboard(self.state, now=5.5)
        buttons = [call.args for call in mux.call_args_list if "@samdario-auto" in call.args]
        self.assertEqual(len(buttons), 1)
        self.assertFalse(any("command-prompt" in call.args for call in mux.call_args_list))  # aucun export lancé


class CodexReviewTest(LoopBase):
    """Relecture de la tâche 11 par Codex : quatre cas reproduits."""

    def setUp(self):
        super().setUp()
        (self.state / "panes.json").write_text(json.dumps({"%0": "claude", "%1": "codex"}))
        with self.db() as db:
            team.store(db, {"claude": "Alma", "codex": "Milo"})
        self.titles = {}

        def option(_folder, *args):
            if "@samdario_activity" in args:
                self.titles[args[args.index("-t") + 1]] = args[-1]
            return ""

        for module in (samdario, bar):
            replaced = patch.object(module, "tmux", side_effect=option)
            replaced.start()
            self.addCleanup(replaced.stop)

    def test_last_relay_waits_for_the_recipient_to_receive_it(self):
        samdario.dashboard(self.state)
        self.launch()
        auto.toggle(self.state, now=3)
        auto.toggle(self.state, now=4)  # OFF : l'attente de Claude est annulée
        with patch.object(auto, "paste_draft") as paste:
            auto.toggle(self.state, now=5)  # le relais est collé chez Claude, pas encore envoyé
            before = bar.dashboard(self.state, now=6)
        self.assertIn("Entrée chez Claude", before)
        self.assertNotIn("dernier relais", before)
        auto.handle_event(self.state, "claude", typed(paste.call_args.args[2], "entree"), now=7)
        self.assertIn("dernier relais : Milo → Alma", bar.dashboard(self.state, now=8))

    def test_common_request_in_claude_field_shows_enter_expected(self):
        samdario.dashboard(self.state)
        self.launch()
        with patch.object(auto, "queue_send", return_value="q1"), patch.object(auto, "paste_draft") as paste:
            auto.broadcast(self.state, "Quel format choisir ?", now=4100)  # le réveil de Claude a expiré
            line = bar.dashboard(self.state, now=4101)
        self.assertIn("tour de table : Entrée chez Claude", line)
        self.assertEqual(self.titles["%0"], "— Entrée attendue")
        auto.handle_event(self.state, "claude", typed(paste.call_args.args[2], "nouvelle"), now=4102)
        bar.dashboard(self.state, now=4103)
        self.assertEqual(self.titles["%0"], "— rédige sa réponse")

    def test_session_without_the_dashboard_keeps_its_plain_status(self):
        output = io.StringIO()  # lanceur d'avant la tâche 11 : il n'a pas installé la barre
        with redirect_stdout(output):
            samdario.main(["_status", str(self.state)])
        self.assertEqual((output.getvalue().strip(), self.titles), ("OFF", {}))

    def test_locked_base_keeps_the_last_line(self):
        samdario.dashboard(self.state)
        shown = bar.dashboard(self.state, now=1)
        self.assertIn("OFF", shown)
        connect = sqlite3.connect

        def no_wait(*args, **kwargs):
            return connect(*args, **dict(kwargs, timeout=0))

        with closing(connect(self.state / "replies.db")) as lock:
            lock.execute("BEGIN IMMEDIATE")  # SQLite détient réellement le verrou
            with patch.object(sqlite3, "connect", side_effect=no_wait):
                self.assertEqual(bar.dashboard(self.state, now=2), shown)


if __name__ == "__main__":
    unittest.main()
