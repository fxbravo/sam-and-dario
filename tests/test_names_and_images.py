"""Le nom Sam & Dario dans les fichiers, les hooks et les consignes, et les illustrations du projet."""

import importlib.util
import json
import unittest
from pathlib import Path

from samdario import cli as samdario
from samdario import export as export
from samdario import messages as messages
from samdario import scene as scene
from samdario import team as team
from tests.test_export import ExportBase
from tests.test_team import TeamBase

ROOT = Path(__file__).resolve().parent.parent


class TeamFilesTest(TeamBase):
    def test_new_project_gets_the_samdario_file(self):
        self.setup_with("Dario", "Sam")
        self.assertEqual(team.TEAM_FILE, ".samdario.json")
        self.assertEqual(team.team_file(self.project), self.file(".samdario.json"))
        self.assertEqual(json.loads(self.file(".samdario.json").read_text(encoding="utf-8")),
                         {"claude": "Dario", "codex": "Sam"})


class ExportMarkersTest(ExportBase):
    def test_new_export_carries_the_samdario_marker_only(self):
        self.agree()
        a = export.latest(self.state)
        text = export.save(self.state, a.token, "Garder le PDF").read_text(encoding="utf-8")
        self.assertIn(f"<!-- samdario-accord: {a.token} -->", text)
        self.assertEqual(text.count("-accord:"), 1)


class NameTest(unittest.TestCase):
    def test_hooks_panes_session_and_instructions_carry_the_name(self):
        self.assertTrue(samdario.codex_hook_settings()["command"].endswith("-I -m samdario _event codex"))
        self.assertEqual((samdario.SESSION, samdario.STATE_VARIABLE), ("samdario", "SAMDARIO_STATE"))
        for agent in ("claude", "codex"):
            for names in (dict(messages.NAMES), {"claude": "Dario", "codex": "Sam"}):
                text = messages.instructions(agent, names, "")
                with self.subTest(agent=agent, names=names):
                    self.assertTrue(text.startswith("[Sam & Dario] "), text[:40])


def _drawing():
    """Le générateur d'images, ou None si son prérequis manque (Pillow 10.1 ou ultérieur, avec FreeType).
    L'application, elle, n'utilise que la bibliothèque standard."""
    if importlib.util.find_spec("PIL") is None:
        return None
    spec = importlib.util.spec_from_file_location("dessiner", ROOT / "docs" / "images" / "dessiner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module if module.missing_requirement() is None else None


DRAWING = _drawing()


# Références fixées côté test (relecture de Codex) : une erreur de couleur ou de placement dans le générateur
# ne peut pas se recopier ici. Couleurs RVB de la palette xterm pour chaque lettre des grilles.
REFERENCE_RGB = {"D": (175, 95, 0), "O": (255, 135, 0), "W": (255, 255, 255), "K": (28, 28, 28),
                 "P": (255, 175, 175), "N": (0, 0, 95), "B": (0, 95, 175), "L": (175, 215, 255), "Y": (255, 215, 95)}
# Coin supérieur gauche de chaque personnage dans la bannière de 1280 × 560 : Dario à gauche, Sam à droite.
REFERENCE_PLACES = {"claude": (272, 164), "codex": (752, 164)}
LABEL_BAND = range(480, 543)  # lignes des cartouches de prénom
WHITE = (255, 255, 255)


def _expected_character(agent: str, scale: int):
    """Le portrait attendu, construit pixel par pixel à partir de la grille et des couleurs de référence."""
    from PIL import Image
    grid = scene.SPRITES[agent]
    image = Image.new("RGBA", (len(grid[0]) * scale, len(grid) * scale), (0, 0, 0, 0))
    for y, row in enumerate(grid):
        for x, letter in enumerate(row):
            if letter != ".":
                for dy in range(scale):
                    for dx in range(scale):
                        image.putpixel((x * scale + dx, y * scale + dy), REFERENCE_RGB[letter] + (255,))
    return image


class GeneratorImportTest(unittest.TestCase):
    def test_without_pillow_the_generator_explains_how_to_install_it(self):
        import subprocess
        import sys
        script = ROOT / "docs" / "images" / "dessiner.py"
        before = {path.name: path.read_bytes() for path in script.parent.glob("*.png")}
        code = ("import runpy, sys; sys.modules['PIL'] = None; "
                f"runpy.run_path({str(script)!r}, run_name='__main__')")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pip install "Pillow>=10.1"', result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual({path.name: path.read_bytes() for path in script.parent.glob("*.png")}, before)


def _same_pixels(first, second) -> bool:
    """Compare les octets RVBA décodés : une couleur changée compte, pas seulement le canal alpha."""
    first, second = first.convert("RGBA"), second.convert("RGBA")
    return first.size == second.size and first.tobytes() == second.tobytes()


@unittest.skipIf(DRAWING is None, "Pillow 10.1 ou ultérieur avec FreeType absent : générateur d'images non testé")
class ImagesTest(unittest.TestCase):
    def setUp(self):
        self.draw = DRAWING

    def test_each_character_matches_its_grid_in_every_pixel_of_every_block(self):
        for letter, expected in REFERENCE_RGB.items():
            with self.subTest(letter=letter):
                self.assertEqual(self.draw.rgb(scene.PIXELS[letter]), expected)
        for agent in ("claude", "codex"):
            for scale in (1, 4):
                with self.subTest(agent=agent, scale=scale):
                    self.assertTrue(_same_pixels(self.draw.character(agent, scale=scale),
                                                 _expected_character(agent, scale)))

    def test_an_unknown_colour_letter_fails(self):
        with self.assertRaises(ValueError):
            self.draw.sprite(("Z.",), scale=2)

    def test_banner_shows_each_character_at_its_place(self):
        image = self.draw.banner().convert("RGBA")
        self.assertEqual(image.size, (1280, 560))
        for agent, (left, top) in REFERENCE_PLACES.items():
            figure = _expected_character(agent, 16)
            opaque = 0
            for y in range(figure.height):
                for x in range(figure.width):
                    pixel = figure.getpixel((x, y))
                    if pixel[3]:
                        opaque += 1
                        with self.subTest(agent=agent):
                            self.assertEqual(image.getpixel((left + x, top + y)), pixel)
            self.assertGreater(opaque, 100)

    def test_banner_writes_dario_and_sam_in_their_own_cartouches(self):
        image = self.draw.banner().convert("RGB")
        widths = {}
        for agent, letter, half in (("claude", "O", range(0, 640)), ("codex", "B", range(640, 1280))):
            label = REFERENCE_RGB[letter]
            box = [x for x in half if image.getpixel((x, LABEL_BAND[10])) == label]
            with self.subTest(agent=agent):
                self.assertTrue(box, "cartouche absent")
                white = [x for y in LABEL_BAND for x in range(min(box), max(box) + 1)
                         if image.getpixel((x, y)) == WHITE]
                self.assertGreater(len(white), 150, "cartouche sans texte")
                widths[agent] = max(white) - min(white)
        self.assertGreater(widths["claude"], widths["codex"] * 1.1)  # « Dario » est plus long que « Sam »

    def test_banner_writes_exactly_dario_on_the_left_and_sam_on_the_right(self):
        from unittest.mock import patch
        written = []
        original = self.draw.ImageDraw.ImageDraw.text

        def spy(drawing, xy, text, *args, **kwargs):
            written.append((xy[0], text))
            return original(drawing, xy, text, *args, **kwargs)

        with patch.object(self.draw.ImageDraw.ImageDraw, "text", spy):
            self.draw.banner()
        self.assertEqual([text for _, text in written], ["Dario", "Sam"])
        sides = {text: x for x, text in written}
        self.assertLess(sides["Dario"], 640)
        self.assertGreater(sides["Sam"], 640)

    def test_a_missing_requirement_stops_before_writing_any_image(self):
        from unittest.mock import patch
        with patch.object(self.draw.features, "check", return_value=False):
            self.assertIn("FreeType", self.draw.missing_requirement())
            with patch.object(self.draw.Image.Image, "save") as save, self.assertRaises(SystemExit):
                self.draw.main()
            save.assert_not_called()
        with patch.object(self.draw.PIL, "__version__", "10.0.1"):
            self.assertIn("10.1", self.draw.missing_requirement())
        self.assertIsNone(self.draw.missing_requirement())

    def test_committed_images_match_the_current_drawing(self):
        from PIL import Image
        expected = {"dario.png": self.draw.character("claude"), "sam.png": self.draw.character("codex"),
                    "sam-et-dario.png": self.draw.banner()}
        for name, fresh in expected.items():
            with self.subTest(image=name), Image.open(ROOT / "docs" / "images" / name) as stored:
                self.assertTrue(_same_pixels(stored, fresh))


if __name__ == "__main__":
    unittest.main()
