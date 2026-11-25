"""Relecture des bulles défilantes. Lancer avec python3 -m unittest tests.test_review_scroll."""

import io
import re
import sqlite3
import time
import unittest
from contextlib import closing, redirect_stdout
from types import SimpleNamespace
from typing import Callable, NamedTuple, Optional
from unittest.mock import patch

from samdario import scene as scene
from samdario import scene_scroll as scroll
from tests.test_scene import bubble, rendered_rows, sample
from tests.test_state import Base


LEFT = b"\x1b[<65;10;5M"
RIGHT = b"\x1b[<65;90;5M"
SIZE = (110, 30)


class Instant(NamedTuple):
    at: float
    wheel: bytes = b""
    size: tuple = SIZE
    before: Optional[Callable] = None


class EndOfReplay(BaseException):
    """Arrêt du terminal de test, y compris si la scène intercepte une erreur de dessin."""


class Terminal(io.StringIO):
    def flush(self):
        super().flush()
        self.displayed_at = time.perf_counter()


class Defilement(Base):
    def setUp(self):
        super().setUp()
        self.alma = "\n".join(f"Alma-{n:03d}" for n in range(1, 81))
        milo = "\n".join(f"Milo-{n:03d}" for n in range(1, 81))
        self.turn("claude", "Premier avis ?", self.alma)
        self.turn("codex", "Autre avis ?", milo)

    def screens(self, *instants):
        """Lance la scène sur sa vraie base et relève l'écran à chaque instant.

        Seuls l'horloge, les dimensions et les événements du terminal sont pilotés.
        """
        output = Terminal()
        clock = SimpleNamespace(at=0.0, size=SIZE)
        frames, self.displayed_at = [], []
        timeline = iter(instants)

        def read_input(keyboard, timeout):
            if clock.at:
                frames.append(output.getvalue().rsplit("\x1b[H", 1)[-1])
                self.displayed_at.append(output.displayed_at)
            instant = next(timeline, None)
            if instant is None:
                raise EndOfReplay
            clock.at, clock.size = instant.at, instant.size
            if instant.before:
                instant.before()
            return instant.wheel

        with redirect_stdout(output), patch.object(scroll, "_quiet_keyboard", return_value=None), \
                patch.object(scroll, "_wait_input", side_effect=read_input), \
                patch.object(scroll, "time", SimpleNamespace(monotonic=lambda: clock.at)), \
                patch.object(scroll.shutil, "get_terminal_size", side_effect=lambda: clock.size):
            with self.assertRaises(EndOfReplay):
                scene.run(self.state)
        return frames

    def first_lines(self, frame):
        return tuple(re.search(name + r"-\d+", frame).group() for name in ("Alma", "Milo"))

    def test_new_reply_starts_at_line_one_during_a_wheel_gesture(self):
        def publish():
            self.turn("claude", "Redis le même avis", self.alma)

        first, moved, renewed = self.screens(
            Instant(1), Instant(1.1, LEFT + RIGHT), Instant(2.1, LEFT + RIGHT, before=publish))
        self.assertEqual(self.first_lines(first), ("Alma-001", "Milo-001"))
        self.assertEqual(self.first_lines(moved), ("Alma-004", "Milo-004"))
        self.assertEqual(self.first_lines(renewed), ("Alma-001", "Milo-007"))

    def test_briefly_hiding_both_bubbles_keeps_the_readers_place(self):
        _, moved, small, restored = self.screens(
            Instant(1), Instant(1.1, LEFT * 2 + RIGHT),
            Instant(1.2, size=(59, 30)), Instant(1.3))
        self.assertEqual(self.first_lines(moved), ("Alma-007", "Milo-004"))
        self.assertIn(scene.TOO_SMALL, small)
        self.assertEqual(self.first_lines(restored), self.first_lines(moved))

    def test_locked_periodic_read_does_not_delay_the_wheel(self):
        with closing(sqlite3.connect(self.state / "replies.db")) as writer:
            started_at = 0.0

            def start_lock():
                nonlocal started_at
                writer.execute("BEGIN EXCLUSIVE")
                started_at = time.perf_counter()

            _, moved = self.screens(Instant(1), Instant(2.1, LEFT, before=start_lock))
            elapsed = self.displayed_at[-1] - started_at
        self.assertEqual(self.first_lines(moved), ("Alma-004", "Milo-001"))
        # Le délai SQLite existant est de 0,5 s. L'horloge de la scène et la molette n'attendent pas dans cet essai.
        self.assertLess(elapsed, 0.4, f"La ligne 4 a attendu {elapsed:.3f} s sous le verrou.")

    def test_read_error_stays_visible_until_data_is_read_again(self):
        database = self.state / "replies.db"
        saved = self.state / "base-de-test-en-attente"
        _, failed, during, recovered = self.screens(
            Instant(1), Instant(2.1, before=lambda: database.rename(saved)), Instant(2.2),
            Instant(3.2, before=lambda: saved.rename(database)))
        self.assertTrue(scene.ERROR in failed, "L'erreur de lecture doit s'afficher.")
        self.assertEqual(self.first_lines(recovered), ("Alma-001", "Milo-001"))
        self.assertTrue(scene.ERROR in during, "L'ancien texte a remplacé l'erreur avant le retour de la base.")

    def test_scene_recovers_when_preparing_the_visible_lines_fails_once(self):
        original_wrap = scene.wrap
        first_call = True

        def fail_once(text, width):
            nonlocal first_call
            if first_call:
                first_call = False
                raise ValueError("découpage indisponible")
            return original_wrap(text, width)

        with patch.object(scene, "wrap", side_effect=fail_once):
            failed, recovered = self.screens(Instant(1), Instant(2.1))
        self.assertIn("découpage indisponible", failed)
        self.assertEqual(self.first_lines(recovered), ("Alma-001", "Milo-001"))


class LiensMarkdown(unittest.TestCase):
    """Texte effectivement affiché dans la bulle. Les exemples tiennent sans retour automatique à la ligne."""

    def lignes(self, texte):
        data = sample(texts={"claude": texte, "codex": None})
        ecran = rendered_rows(scene.frame(data, 162, 38))
        haut, bas, _ = bubble(ecran)
        bord = ecran[haut].index("╮")
        return [ligne[2:bord].rstrip() for ligne in ecran[haut + 2:bas]]

    def test_exemple_de_lien_dans_un_bloc_de_code(self):
        lignes = self.lignes("```md\n[Guide](docs/lire.md)\n```\nOuvre [Guide](docs/lire.md).")
        self.assertIn("Ouvre Guide.", lignes)
        self.assertIn("[Guide](docs/lire.md)", lignes)

    def test_lien_en_gras(self):
        self.assertEqual(self.lignes("**[Compte à rebours](src/main.js:335)**"), ["Compte à rebours"])

    def test_nom_de_fonction_en_code_dans_le_libelle(self):
        self.assertEqual(self.lignes("[`__init__`](src/app.py:12)"), ["__init__"])

    def test_parentheses_dans_le_chemin(self):
        self.assertEqual(self.lignes("Choisir [Calendrier](app/(mobile)/calendrier.tsx:12)."), ["Choisir Calendrier."])

    def test_image_sans_texte_entre_deux_paragraphes(self):
        lignes = self.lignes("Avant\n![](carte.png)\nAprès")
        self.assertEqual((lignes[0], lignes[-1]), ("Avant", "Après"))
        self.assertNotIn("carte.png", "\n".join(lignes))


if __name__ == "__main__":
    unittest.main()
