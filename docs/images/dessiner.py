"""Dessine les illustrations du README à partir des personnages de la scène (tâche 20).

    python3 docs/images/dessiner.py

Produit, à côté de ce script, `dario.png` et `sam.png` (chaque personnage sur fond transparent) et
`sam-et-dario.png` (les deux côte à côte, de face, comme dans la scène). Les grilles et les couleurs viennent de
`samdario/scene.py` : les images restent identiques aux personnages de l'application. Ce script demande Pillow,
qui ne sert qu'à lui ; l'application, elle, n'utilise que la bibliothèque standard.

Prérequis : Pillow 10.1 ou ultérieur, avec FreeType (`ImageFont.load_default(size=…)` en a besoin pour les
prénoms). Le script le vérifie avant d'écrire la moindre image : `python3 -m pip install "Pillow>=10.1"`.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import PIL
    from PIL import Image, ImageDraw, ImageFont, features
except ImportError:  # sans Pillow : `main` dit comment l'installer, sans trace d'erreur
    PIL = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from samdario import scene  # noqa: E402

SCALE = 16  # pixels d'image par pixel du dessin
BANNER_WIDTH, BANNER_HEIGHT = 1280, 560
NAMES = {"claude": "Dario", "codex": "Sam"}
CENTRES = {"claude": 400, "codex": 880}  # Dario à gauche, Sam à droite, comme dans la scène
GROUND = BANNER_HEIGHT - 110
LABEL_COLOURS = {"claude": scene.PIXELS["O"], "codex": scene.PIXELS["B"]}


def missing_requirement():
    """None si Pillow peut dessiner les prénoms, sinon la raison."""
    if PIL is None:
        return "Pillow absent"
    version = tuple(int(part) for part in PIL.__version__.split(".")[:2] if part.isdigit())
    if version < (10, 1):
        return f"Pillow {PIL.__version__} : il faut Pillow 10.1 ou ultérieur"
    if not features.check("freetype2"):
        return "Pillow sans FreeType : les prénoms ne peuvent pas être dessinés"
    return None


def rgb(colour: int) -> tuple:
    """Une couleur de la palette 256 couleurs de xterm, en RVB."""
    if colour < 16:
        base = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188),
                (17, 168, 205), (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
                (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]
        return base[colour]
    if colour < 232:
        index = colour - 16
        steps = (0, 95, 135, 175, 215, 255)
        return steps[index // 36], steps[(index // 6) % 6], steps[index % 6]
    level = 8 + (colour - 232) * 10
    return level, level, level


def sprite(grid: tuple, scale: int = SCALE) -> Image.Image:
    """Une grille de lettres (voir `scene.PIXELS`) en image ; `.` reste transparent. ValueError sur une lettre
    inconnue : une couleur oubliée ne doit pas passer inaperçue."""
    image = Image.new("RGBA", (len(grid[0]) * scale, len(grid) * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(grid):
        for x, letter in enumerate(row):
            if letter not in scene.PIXELS:
                raise ValueError(f"Couleur inconnue « {letter} » en ligne {y + 1}, colonne {x + 1}")
            colour = scene.PIXELS[letter]
            if colour is not None:
                draw.rectangle([x * scale, y * scale, (x + 1) * scale - 1, (y + 1) * scale - 1],
                               fill=rgb(colour) + (255,))
    return image


def character(agent: str, scale: int = SCALE) -> Image.Image:
    return sprite(scene.SPRITES[agent], scale)


def positions() -> dict:
    """Le coin supérieur gauche de chaque personnage dans la bannière, posé sur le sol."""
    width, height = len(scene.SPRITES["claude"][0]) * SCALE, len(scene.SPRITES["claude"]) * SCALE
    return {agent: (centre - width // 2, GROUND - height + 2) for agent, centre in CENTRES.items()}


def label_row() -> int:
    """Une ligne qui traverse les deux cartouches de prénom."""
    return GROUND + 40


def banner() -> Image.Image:
    """Dario à gauche, Sam à droite, sur le ciel, les collines et le sol de la scène, chacun avec son prénom."""
    image = Image.new("RGB", (BANNER_WIDTH, BANNER_HEIGHT), rgb(scene.SKY))
    draw = ImageDraw.Draw(image)
    ground = GROUND
    draw.ellipse([-200, ground - 170, 620, ground + 250], fill=rgb(scene.HILL))
    draw.ellipse([760, ground - 120, 1500, ground + 250], fill=rgb(scene.HILL))
    draw.rectangle([0, ground, BANNER_WIDTH, ground + 14], fill=rgb(scene.GRASS))
    draw.rectangle([0, ground + 14, BANNER_WIDTH, BANNER_HEIGHT], fill=rgb(scene.SOIL))
    font = ImageFont.load_default(size=46)
    for agent, centre in CENTRES.items():
        figure = character(agent)
        image.paste(figure, positions()[agent], figure)
        name = NAMES[agent]
        width = draw.textlength(name, font=font)
        draw.rectangle([centre - width / 2 - 14, ground + 30, centre + width / 2 + 14, ground + 92],
                       fill=rgb(LABEL_COLOURS[agent]))
        draw.text((centre - width / 2, ground + 34), name, font=font, fill=(255, 255, 255))
    return image


def main() -> None:
    problem = missing_requirement()
    if problem:
        sys.exit(f"Images non générées. {problem}. Installe-le avec : python3 -m pip install \"Pillow>=10.1\"")
    character("claude").save(HERE / "dario.png")
    character("codex").save(HERE / "sam.png")
    banner().save(HERE / "sam-et-dario.png")


if __name__ == "__main__":
    main()
