"""Tâche 12 : la scène 16 bits (seconde fenêtre tmux, lecture seule)."""

import fcntl
import os
import pty
import re
import select
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from samdario import auto as auto
from samdario import bar as bar
from samdario import scene as scene
from samdario import scene_text as text
from samdario import state as state
from samdario import team as team
from tests.test_auto import LoopBase, clock, done, stop, taken
from tests.test_state import Base

ASKING = "Deux options : UTF-8, ou UTF-8 avec BOM. Je recommande la seconde.\n[DÉCISION]"
POSITION = re.compile(r"\x1b\[\d+;1H")
STYLE = re.compile(r"\x1b\[[0-9;]*m")


def sample(**changes):
    data = scene.SceneData(names={"claude": "Alma", "codex": "Milo"},
                           texts={"claude": "Je garde Paprika.", "codex": "D’accord."},
                           activity={"claude": "rédige sa réponse", "codex": "attend le relais"},
                           awaited="claude", status="AUTO 3/8", decision_by=())
    return data._replace(**changes)


def rows(image):
    return ["".join(cell.char for cell in row) for row in image]


def rendered_rows(image):
    """Les lignes telles que le terminal les reçoit, sans les séquences d'échappement."""
    return [STYLE.sub("", part) for part in POSITION.split(scene.render(image))[1:]]


def columns(text):
    """Colonnes affichées, mesurées indépendamment du module : combinant 0, large 2."""
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in text)


def bubble(lines, left=0):
    """Haut, bas et largeur intérieure de la bulle qui commence à la colonne `left`."""
    top = next(r for r, line in enumerate(lines) if line[left:left + 1] == "╭")
    bottom = next(r for r, line in enumerate(lines) if line[left:left + 1] == "╰")
    return top, bottom, lines[top + 2].index("│", left + 1) - left - 3


def sprite_rows(lines, centre):
    return [r for r, line in enumerate(lines)
            if "▀" in line[centre - 8:centre + 8] and set(line.strip()) != {"▀"}]



# La scène est lancée comme module du paquet ; la racine du dépôt suffit, sans installation (tâche 21).
PACKAGE_ENV = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))

class TextTest(unittest.TestCase):
    def test_clean_removes_markers(self):
        self.assertEqual(scene.clean("Oui.\n[ACCORD]"), "Oui.")
        self.assertEqual(scene.clean("Choisis.\n[DÉCISION]\n"), "Choisis.")
        # Relecture de Codex : seul le marqueur de la ligne finale est du protocole ; un marqueur cité reste.
        self.assertEqual(scene.clean("Je cite [ACCORD] ici.\nSuite."), "Je cite [ACCORD] ici.\nSuite.")
        # Écart noté par Codex à la clôture : un marqueur qui termine une phrase n'est pas une ligne de protocole.
        self.assertEqual(scene.clean("Tu peux écrire [DÉCISION]"), "Tu peux écrire [DÉCISION]")
        self.assertEqual(scene.clean("[ACCORD]"), "")

    def test_clean_drops_markdown_emphasis_marks(self):
        # Essai réel : « Ma proposition : **Paprika** » s'affichait avec ses astérisques.
        self.assertEqual(scene.clean("Ma proposition : **Paprika**, ou `Safran` et __Rouille__."),
                         "Ma proposition : Paprika, ou Safran et Rouille.")
        self.assertEqual(scene.clean("2 * 3 = 6, et snake_case_reste"), "2 * 3 = 6, et snake_case_reste")

    def test_clean_keeps_only_the_text_of_markdown_links(self):
        # Capture de l'utilisateur (22 novembre) : « [Compte à rebours](/private/tmp/…/main.js:335) » s'affichait tel quel.
        self.assertEqual(scene.clean("Voir [Compte à rebours](/private/tmp/projet/src/main.js:335) · "
                                     "[Jour enregistré au bilan](../src/main.js#L609)."),
                         "Voir Compte à rebours · Jour enregistré au bilan.")
        self.assertEqual(scene.clean("Schéma : ![carte du réseau](docs/carte.png)"), "Schéma : carte du réseau")
        # Une étiquette de relais ou un marqueur cité, sans lien, reste intact.
        self.assertEqual(scene.clean("[Réponse de Codex #2] puis [ACCORD] cité (sans lien)."),
                         "[Réponse de Codex #2] puis [ACCORD] cité (sans lien).")

    def test_control_characters_never_reach_the_terminal(self):
        image = scene.frame(sample(texts={"claude": "\x1b[31mrouge\x1b[0m\r fin\x07", "codex": None}), 110, 30)
        self.assertFalse(any("\x1b" in cell.char or "\r" in cell.char or "\x07" in cell.char
                             for row in image for cell in row))
        self.assertIn("rouge fin", "\n".join(rows(image)))
        self.assertEqual(scene.render(image).count("\x1b") - scene.render(image).count("\x1b[") , 0)

    def test_wrap_keeps_french_punctuation_with_its_word(self):
        text = "Je peux accepter Paprika avec « Papri » comme diminutif : oui ? Bien sûr ! Voilà ; fin."
        for width in range(12, 40):
            lines = scene.wrap(text, width)
            with self.subTest(width=width):
                for line in lines:
                    self.assertFalse(line[:1] in "»:;?!" and line, line)
                    self.assertFalse(line.endswith("«"), line)
                    self.assertLessEqual(columns(line), width)

    def test_wrap_cuts_a_word_longer_than_the_line(self):
        self.assertEqual([len(line) for line in scene.wrap("x" * 50, 20)], [20, 20, 10])

    def test_wrap_keeps_paragraphs_and_list_lines(self):
        self.assertEqual(scene.wrap("a\n\nb\n1. c\n2. d", 30), ["a", "", "b", "1. c", "2. d"])

    def test_width_of_each_kind_of_character(self):
        self.assertEqual([text.width_of(char) for char in ("a", "é", "́", "😀", "中")], [1, 1, 0, 2, 2])


class FrameTest(unittest.TestCase):
    def test_frame_fills_the_window_at_each_size(self):
        for width, height in ((60, 20), (110, 30), (162, 38), (220, 56)):
            with self.subTest(size=(width, height)):
                image = scene.frame(sample(), width, height)
                self.assertEqual(len(image), height)
                self.assertTrue(all(len(row) == width for row in image))
                lines = rows(image)
                for centre in (width // 4, width * 3 // 4):
                    found = sprite_rows(lines, centre)
                    self.assertEqual(len(found), 9)
                    self.assertEqual(found[-1] - found[0], 8)
                    spans = [i for r in found for i, char in enumerate(lines[r]) if char == "▀"
                             and centre - 8 <= i < centre + 8]
                    self.assertLessEqual(max(spans) - min(spans), 15)

    def test_bubbles_share_the_width_and_take_the_height(self):
        lines = rows(scene.frame(sample(texts={"claude": "mot " * 2000, "codex": "mot " * 2000}), 162, 38))
        for left in (0, 81):
            top, bottom, inner = bubble(lines, left)
            self.assertGreaterEqual(bottom - top - 2, 20)
            self.assertGreaterEqual(inner, 74)

    def numbered(self, count=200):
        return "\n".join(f"ligne {n}" for n in range(1, count + 1))

    def test_long_reply_scrolls_instead_of_being_cut(self):
        self.assertFalse(hasattr(scene, "CONTINUED"))  # tâche 16 : plus de « suite dans le terminal »
        data = sample(texts={"claude": self.numbered(), "codex": None})
        top, _, _ = bubble(rows(scene.frame(data, 110, 30)))
        self.assertIn("ligne 1 ", rows(scene.frame(data, 110, 30))[top + 2])
        lines = rows(scene.frame(data, 110, 30, {"claude": 30, "codex": 0}))
        top, _, _ = bubble(lines)
        self.assertIn("ligne 31 ", lines[top + 2])

    def test_position_is_clamped_to_the_text(self):
        data = sample(texts={"claude": self.numbered(), "codex": None})
        lines = rows(scene.frame(data, 110, 30, {"claude": 10_000, "codex": 0}))
        _, bottom, _ = bubble(lines)
        self.assertIn("ligne 200 ", lines[bottom - 1])
        lines = rows(scene.frame(data, 110, 30, {"claude": -5, "codex": 0}))
        top, _, _ = bubble(lines)
        self.assertIn("ligne 1 ", lines[top + 2])

    def test_scrollbar_and_marks(self):
        data = sample(texts={"claude": self.numbered(), "codex": "Court."})
        limits = scene.bubble_limits(data, 110, 30)
        for offset, up, down in ((0, False, True), (limits["claude"] // 2, True, True), (limits["claude"], True, False)):
            with self.subTest(offset=offset):
                lines = rows(scene.frame(data, 110, 30, {"claude": offset, "codex": 0}))
                top, bottom, inner = bubble(lines)
                self.assertEqual("▲" in lines[top][:55], up)
                self.assertEqual("▼" in lines[bottom][:55], down)
                self.assertTrue(any("█" in line[:55] for line in lines[top + 2:bottom]))
        lines = rows(scene.frame(data, 110, 30))
        top, bottom, _ = bubble(lines, 55)
        right = "".join(line[55:] for line in lines[top:bottom + 1])
        self.assertFalse(any(mark in right for mark in "▲▼█░"))  # réponse courte : ni barre ni repère

    def test_text_keeps_a_gap_before_the_scrollbar(self):
        # Capture de l'utilisateur (22 novembre) : des lignes pleines touchaient la barre (« la date░ »).
        _, inner, _, _ = scene._geometry(110, 30, False)
        data = sample(texts={"claude": "\n".join("x" * inner for _ in range(100)), "codex": None})  # lignes pleines
        lines = rows(scene.frame(data, 110, 30))
        top, bottom, _ = bubble(lines)
        for line in lines[top + 2:bottom]:
            bar = next(i for i, char in enumerate(line[:55]) if char in "█░")
            self.assertEqual(line[bar - 1], " ", line)

    def test_marks_fit_the_minimum_width(self):
        data = sample(texts={"claude": self.numbered(), "codex": None})
        lines = rows(scene.frame(data, 60, 20, {"claude": 5, "codex": 0}))
        top, bottom, _ = bubble(lines)
        self.assertTrue(lines[bottom].startswith("╰") and lines[bottom][:30].rstrip().endswith("╯"))
        self.assertIn("▼", lines[bottom][:30])
        self.assertTrue(lines[top].startswith("╭") and "▲" in lines[top][:30])

    def test_too_small_window_asks_to_grow(self):
        for width, height in ((59, 20), (60, 19), (0, 0), (5, 1)):
            with self.subTest(size=(width, height)):
                image = scene.frame(sample(), width, height)
                self.assertEqual((len(image), {len(row) for row in image} or {width}), (height, {width}))
                if width >= len(scene.TOO_SMALL) and height:
                    self.assertIn(scene.TOO_SMALL, rows(image)[height // 2])

    def test_waiting_bubble_before_any_reply(self):
        lines = rows(scene.frame(sample(texts={"claude": "Bonjour.", "codex": None}), 110, 30))
        top, _, _ = bubble(lines, 55)
        self.assertEqual(lines[top + 2][57:58], scene.WAITING)

    def test_wide_characters_keep_the_bubble_aligned(self):
        self.assert_aligned("😀" * 40)

    def test_combining_accents_keep_the_bubble_aligned(self):
        self.assert_aligned("é" * 40)

    def assert_aligned(self, text):
        image = scene.frame(sample(texts={"claude": text, "codex": text}), 110, 30)  # deux bulles de même hauteur
        lines = rendered_rows(image)
        top, bottom, _ = bubble(rows(image))
        borders = set()
        for line in lines[top:bottom + 1]:
            right = max(i for i, char in enumerate(line) if char in "│╮╯")
            borders.add(columns(line[:right]))
        self.assertEqual(len(borders), 1, borders)

    def test_banner_per_state(self):
        long = {"claude": "mot " * 2000, "codex": "mot " * 2000}
        running = rows(scene.frame(sample(texts=long), 110, 30))
        self.assertEqual(bubble(running)[0], 0)
        self.assertEqual(rows(scene.frame(sample(status="OFF", texts=long), 110, 30))[0], running[0])
        cases = (("accord", ("✦ ACCORD ✦", "◀ Terminaux pour écrire")),  # on n'écrit pas dans la scène
                 ("decision", ("DÉCISION REQUISE", "◀ Terminaux pour répondre")),
                 ("incertain", ("PAUSE",)))
        for key, words in cases:
            with self.subTest(reason=key):
                data = sample(status=f"PAUSE — {bar.rules.REASONS[key]}", texts=long,
                              decision_by=("codex",) if key == "decision" else ())
                image = scene.frame(data, 110, 30)
                lines = rows(image)
                for word in words:
                    self.assertIn(word, lines[0])
                self.assertEqual(bubble(lines)[0], 1)
                colour = int(bar.COLOURS[bar.family(data.status)].removeprefix("colour"))
                self.assertEqual(image[0][0].bg, colour)
                if key == "decision":
                    head = sprite_rows(lines, 110 * 3 // 4)[0]
                    self.assertIn("!", lines[head - 1][110 * 3 // 4 - 8:110 * 3 // 4 + 8])
                    self.assertNotIn("!", lines[head - 1][110 // 4 - 8:110 // 4 + 8])

    def test_sky_is_one_light_blue(self):
        # Tâche 12 bis : un seul bleu clair, du haut du ciel jusqu'au sol, à la place des quatre bandes.
        image = scene.frame(sample(texts={"claude": "Oui.", "codex": "Non."}), 110, 30)
        ground = 30 - 3
        sky = {image[row][54].bg for row in range(ground)}  # la colonne entre les deux bulles, loin des collines
        self.assertEqual(sky, {scene.SKY})
        self.assertEqual({cell.bg for cell in image[0]}, {scene.SKY})

    def test_names_activity_and_awaited_mark(self):
        lines = rows(scene.frame(sample(), 110, 30))
        label = next(line for line in lines if "Alma · Claude" in line)
        self.assertIn("● Alma · Claude", label)
        self.assertIn("rédige sa réponse", label)
        self.assertIn("○ Milo · Codex", label)
        self.assertIn("attend le relais", label)

    def test_render_uses_256_colours(self):
        output = scene.render(scene.frame(sample(), 80, 24))
        self.assertIn("38;5;", output)
        self.assertIn("48;5;", output)
        self.assertNotIn("38;2;", output)


class CodexReviewTest(Base):
    """Relecture de la tâche 12 par Codex : cinq cas reproduits."""

    def rendered_reply(self, body):
        self.turn("claude", "Comment le présenter ?", body)
        output, _ = scene.step(self.state, (162, 38), None)
        return re.sub(r"\x1b\[[0-9;]*[Hm]", "", output)

    def test_literal_code_and_quoted_marker_survive_to_the_screen(self):
        visible = self.rendered_reply("Utilise `__init__` et cite `[DÉCISION]`.\n[ACCORD]")
        self.assertIn("Utilise __init__ et cite [DÉCISION].", visible)
        self.assertNotIn("[ACCORD]", visible)

    def test_joined_emoji_survives_to_the_screen(self):
        self.assertIn("La développeuse 👩\u200d💻 a répondu.", self.rendered_reply("La développeuse 👩\u200d💻 a répondu."))

    def test_long_valid_name_keeps_the_bubble_border(self):
        name = "Anne-Marguerite-Élisabeth-Rose"
        self.assertIsNone(team._name_error("claude", name))
        image = scene.frame(sample(names={"claude": name, "codex": "Milo"}), 60, 20)
        top = next(n for n, row in enumerate(image) if row[0].char == "╭")
        right = next(i for i, cell in enumerate(image[top]) if cell.char == "╮")
        self.assertEqual(image[top + 1][right].char, "│")
        label = next(line for line in rows(image) if "Milo · Codex" in line)  # l'étiquette de Milo reste entière
        self.assertIn("○ Milo · Codex", label)

    def test_drawing_exception_is_visible_and_the_next_refresh_recovers(self):
        with patch.object(scene, "frame", side_effect=ValueError("dessin indisponible")):
            output, shown = scene.step(self.state, (110, 30), None)
        self.assertIn("dessin indisponible", output)
        output, _ = scene.step(self.state, (110, 30), shown)
        self.assertTrue(output and "dessin indisponible" not in output)

    def test_typing_does_not_echo_in_the_read_only_scene(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        with subprocess.Popen([sys.executable, "-B", "-m", "samdario", "_scene", str(self.state)], env=PACKAGE_ENV,
                              stdin=slave, stdout=slave, stderr=slave) as child:
            try:
                self.assertIn(b"\x1b[0m", read_until(master, b"\x1b[0m", 4))
                os.write(master, b"Bonjour Alma")
                self.assertNotIn(b"Bonjour Alma", read_until(master, b"Bonjour Alma", 0.5))
            finally:
                child.terminate()


class MouseTest(Base):
    """La scène demande la souris et défile à la molette, dans un vrai pseudo-terminal (tâche 16)."""

    def start_scene(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        child = subprocess.Popen([sys.executable, "-B", "-m", "samdario", "_scene", str(self.state)], env=PACKAGE_ENV,
                                 stdin=slave, stdout=slave, stderr=slave)
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        return master

    def first_claude_line(self, output):
        """Première ligne de texte de la bulle de Claude dans le dernier dessin complet."""
        frame_text = output.decode("utf-8", "replace").split("\x1b[H")[-1]
        lines = [STYLE.sub("", part) for part in POSITION.split(frame_text)[1:]]
        top = next(r for r, line in enumerate(lines) if line.startswith("╭"))
        return lines[top + 2]

    def test_scene_asks_for_the_mouse_and_still_mutes_typing(self):
        self.opening()
        self.turn("claude", "Question", "\n".join(f"ligne {n}" for n in range(1, 120)))
        master = self.start_scene()
        first = read_until(master, b"\x1b[0m", 4)
        self.assertIn(b"\x1b[?1000h\x1b[?1006h", first)
        os.write(master, b"Bonjour Alma")
        self.assertNotIn(b"Bonjour Alma", read_until(master, b"Bonjour Alma", 0.5))
        os.write(master, b"\x1b[<65;10;5M")
        self.assertIn("ligne 4 ", self.first_claude_line(read_until(master, b"ligne 4 ", 2)))

    def test_wheel_scrolls_loaded_text_while_the_base_is_locked(self):
        self.opening()
        self.turn("claude", "Question", "\n".join(f"ligne {n}" for n in range(1, 120)))
        master = self.start_scene()
        self.assertIn("ligne 1 ", self.first_claude_line(read_until(master, b"\x1b[0m", 4)))
        with closing(sqlite3.connect(self.state / "replies.db")) as lock:
            lock.execute("BEGIN EXCLUSIVE")  # la base est verrouillée : le défilement n'en dépend pas
            os.write(master, b"\x1b[<65;10;5M")
            self.assertIn("ligne 4 ", self.first_claude_line(read_until(master, b"ligne 4 ", 2)))
            lock.rollback()
        self.turn("claude", "Question 2", "Nouvelle réponse.")
        os.write(master, b"\x1b[<65;10;5M")
        self.assertIn(b"Nouvelle r", read_until(master, b"Nouvelle r", 3))


def read_until(master, needle, seconds):
    received = bytearray()
    deadline = time.monotonic() + seconds
    while needle not in received:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([master], [], [], left)[0]:
            break
        received.extend(os.read(master, 65536))
    return bytes(received)


class ReadingTest(Base):
    def data(self):
        with closing(scene.read_only(self.state)) as db:
            return scene.scene_data(db)

    def test_scene_data_reads_the_latest_reply_of_each(self):
        self.opening()
        self.assertEqual(self.data().texts, {"claude": "C0", "codex": "R0"})

    def test_claude_bubble_follows_the_session_after_clear(self):
        self.opening()
        late = self.submit("claude", "Encore une chose", session="A")
        self.submit("claude", "Nouvelle conversation", session="B")
        self.assertIsNone(self.data().texts["claude"])
        current = self.submit("claude", "Question B", session="B")
        self.reply("claude", "Réponse B", current, session="B")
        self.assertEqual(self.data().texts["claude"], "Réponse B")
        self.reply("claude", "Réponse tardive de A", late, session="A")
        self.assertEqual(self.data().texts["claude"], "Réponse B")

    def test_codex_bubble_follows_the_displayed_thread(self):
        self.opening()
        late = self.submit("codex", "Encore une chose", session="TA")
        self.reply("codex", "Réponse TA", late, session="TA", prompt="Encore une chose")
        pending = self.submit("codex", "Suite", session="TA")
        self.submit("codex", "Nouveau fil", session="TB")
        self.assertIsNone(self.data().texts["codex"])
        current = self.submit("codex", "Question TB", session="TB")
        self.reply("codex", "Réponse TB", current, session="TB", prompt="Question TB")
        self.assertEqual(self.data().texts["codex"], "Réponse TB")
        self.reply("codex", "Réponse tardive de TA", pending, session="TA", prompt="Suite")
        self.assertEqual(self.data().texts["codex"], "Réponse TB")


class SceneLoopTest(LoopBase):
    def meta(self):
        with closing(sqlite3.connect(self.state / "replies.db")) as db:
            return db.execute("SELECT key, value FROM meta ORDER BY key").fetchall()

    def test_scene_never_changes_the_state(self):
        self.started()
        self.turn("claude", "Choisis le format.", ASKING)  # enregistrée sans settle : l'auto est encore ON
        with self.db() as db:
            self.assertIsNotNone(bar.rules.decision_reply(db))  # le cas de Codex : status_line poserait la pause
        before = self.meta()
        output, _ = scene.step(self.state, (110, 30), None)
        self.assertIsNotNone(output)
        self.assertEqual(self.meta(), before)
        self.assertEqual(dict(before)["auto"], "on")

    def test_step_opens_the_base_read_only_even_with_a_hash_in_the_path(self):
        with tempfile.TemporaryDirectory(prefix="samdario-", dir="/tmp") as directory:
            folder = Path(directory) / "session #1"
            folder.mkdir()
            state.initialize(folder)
            with state.transaction(folder) as db:
                state.put(db, count=5)
            before = sorted(path.name for path in Path(directory).rglob("*"))
            output, _ = scene.step(folder, (110, 30), None)
            self.assertEqual(sorted(path.name for path in Path(directory).rglob("*")), before)
            with state.transaction(folder) as db:
                self.assertEqual(state.get(db, "count"), "5")
        self.assertIsNotNone(output)

    def decide(self, author):
        self.started()
        with patch("samdario.auto.queue_send", return_value="q1") as send:
            if author == "claude":
                auto.handle_event(self.state, "claude", stop(ASKING, "k1"), now=1.0)
            else:
                waiter = auto.handle_event(self.state, "claude", stop("C1", "k1"), now=1.0)
                relayed = send.call_args.args[1]
                auto.handle_event(self.state, "codex", taken(relayed, "t1"), now=2.0)
                auto.handle_event(self.state, "codex", done(ASKING, "t1", relayed), now=3.0)
                auto.wait(self.state, waiter, clock=clock(), sleep=lambda seconds: None)
        self.assertTrue(auto.status(self.state, now=4.0).startswith("PAUSE — décision"))  # la pause est posée
        with closing(scene.read_only(self.state)) as db:
            data = scene.scene_data(db)
        self.assertEqual(data.decision_by, (author,))
        lines = rows(scene.frame(data, 110, 30))
        centre = 110 // 4 if author == "claude" else 110 * 3 // 4
        head = sprite_rows(lines, centre)[0]
        self.assertIn("!", lines[head - 1][centre - 8:centre + 8])

    def test_decision_author_survives_the_pause_claude(self):
        self.decide("claude")

    def test_decision_author_survives_the_pause_codex(self):
        self.decide("codex")

    def test_step_redraws_only_on_change(self):
        self.launch()
        output, shown = scene.step(self.state, (110, 30), None)
        self.assertIsNotNone(output)
        self.assertEqual(scene.step(self.state, (110, 30), shown), (None, shown))
        resized, _ = scene.step(self.state, (120, 30), shown)
        self.assertIsNotNone(resized)

    def test_locked_base_keeps_the_last_frame(self):
        _, shown = scene.step(self.state, (110, 30), None)
        connect = sqlite3.connect

        def no_wait(*args, **kwargs):
            return connect(*args, **dict(kwargs, timeout=0))

        with closing(connect(self.state / "replies.db")) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with patch.object(sqlite3, "connect", side_effect=no_wait):
                self.assertEqual(scene.step(self.state, (110, 30), shown), (None, shown))
            lock.rollback()

    def test_unexpected_error_is_shown(self):
        for error in (KeyError("auto"), sqlite3.OperationalError("attempt to write a readonly database")):
            with self.subTest(error=error), patch("samdario.scene.scene_data", side_effect=error):
                output, _ = scene.step(self.state, (110, 30), None)
                self.assertIn("erreur de la scène", STYLE.sub("", output))


if __name__ == "__main__":
    unittest.main()
