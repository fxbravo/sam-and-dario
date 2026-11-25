"""Texte des bulles de la scène (tâche 12) : nettoyage, largeur affichée, découpage en lignes.

La largeur suit celle de tmux : un caractère large compte deux colonnes, un signe combinant zéro, et une séquence
reliée par un ZWJ (👩‍💻) reste une seule grappe de deux colonnes (vérifié dans tmux 3.8).
"""

import re
import unicodedata

# Mise en forme Markdown retirée des bulles, en trois temps (relecture de Codex) : un bloc de code reste tel
# quel ; le code en ligne est mis à l'abri (il garde ses tirets bas) ; puis liens et images, lus en comptant les
# parenthèses du chemin, et enfin le gras. Une étiquette « [Réponse de Codex #2] » sans lien reste intacte.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
SHELTER = re.compile(r"\x00(\d+)\x00")
# Protocole : le marqueur seul sur la dernière ligne. « Tu peux écrire [DÉCISION] » garde le sien.
MARKER_LINE = re.compile(r"(?:\A|\n)[ \t]*\[(?:ACCORD|DÉCISION)\]\s*\Z")
ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.?")
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
GLUE_AFTER = re.compile(r"« ")
GLUE_BEFORE = re.compile(r" ([»:;?!])")
NBSP = " "

def clean(text: str) -> str:
    """Retire les marqueurs de fin et tout ce qui piloterait le terminal (échappements, retours chariot)."""
    text = ESCAPES.sub("", text.replace("\t", "    ").replace("\r\n", "\n"))
    text = MARKER_LINE.sub("", CONTROLS.sub("", text))
    text = _outside_code(text)
    return unicodedata.normalize("NFC", text).strip()


def _outside_code(text: str) -> str:
    """Applique `_inline` hors des blocs de code délimités (CommonMark : même caractère, au moins aussi long)."""
    parts, plain, fence = [], [], None
    for line in text.split("\n"):
        found = FENCE.match(line)
        if fence is None and found:
            parts.extend([_inline("\n".join(plain))] if plain else [])
            plain, fence = [], found.group(1)
            parts.append(line)
        elif fence is not None:
            if found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence) and not found.group(2).strip():
                fence = None
            parts.append(line)
        else:
            plain.append(line)
    if plain:
        parts.append(_inline("\n".join(plain)))
    return "\n".join(parts)


def _inline(text: str) -> str:
    sheltered = []

    def shelter(found) -> str:
        sheltered.append(found.group(1))
        return f"\x00{len(sheltered) - 1}\x00"

    text = _links(CODE_SPAN.sub(shelter, text))
    text = BOLD.sub(lambda found: found.group(1) or found.group(2), text)
    return SHELTER.sub(lambda found: sheltered[int(found.group(1))], text)


def _links(text: str) -> str:
    """`[texte](chemin)` donne « texte », `![texte](chemin)` aussi, `![](chemin)` rien. Le chemin peut contenir
    des parenthèses équilibrées, comme `app/(mobile)/calendrier.tsx`."""
    out, position = [], 0
    while True:
        start = text.find("[", position)
        close = text.find("]", start + 1) if start >= 0 else -1
        if start < 0 or close < 0:
            return "".join(out) + text[position:]
        end = _destination_end(text, close + 1) if "\n" not in text[start:close] else None
        if end is None:
            out.append(text[position:start + 1])
            position = start + 1
            continue
        image = start > position and text[start - 1] == "!"
        out.append(text[position:start - 1 if image else start] + text[start + 1:close])
        position = end


def _destination_end(text: str, opening: int) -> "int | None":
    """Position après la parenthèse qui ferme la destination ouverte en `opening`, ou None."""
    if text[opening:opening + 1] != "(":
        return None
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "\n":
            return None
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0:
            return index + 1
    return None


def width_of(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


ZWJ = "\u200d"


def clusters(text: str) -> list:
    """Grappes affichées et leur largeur : un caractère, ses signes de largeur nulle, et ce qu'un ZWJ lui relie
    (👩‍💻 reste un seul pictogramme)."""
    result, joined = [], False
    for char in text:
        size = width_of(char)
        if result and (size == 0 or joined):
            cluster, width = result[-1]
            result[-1] = (cluster + char, width)
        else:
            result.append((char, size))
        joined = char == ZWJ
    return result


def columns(text: str) -> int:
    return sum(width for _, width in clusters(text))


def _cut(word: str, width: int) -> list:
    pieces, current = [], ""
    for cluster, size in clusters(word):
        if current and columns(current) + size > width:
            pieces.append(current)
            current = ""
        current += cluster
    return [*pieces, current] if current else pieces


def fit(text: str, width: int) -> str:
    """`text` raccourci à `width` colonnes, terminé par « … » s'il a fallu couper."""
    if columns(text) <= width:
        return text
    kept, used = "", 0
    for cluster, size in clusters(text):
        if used + size > width - 1:
            break
        kept, used = kept + cluster, used + size
    return kept + "…" if width > 0 else ""


def _wrap_line(line: str, width: int) -> list:
    glued = GLUE_BEFORE.sub(NBSP + r"\1", GLUE_AFTER.sub("«" + NBSP, line))
    lines, current = [], ""
    for word in glued.split(" "):
        candidate = f"{current} {word}" if current else word
        if columns(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        *full, current = _cut(word, width) or [""]
        lines.extend(full)
    lines.append(current)
    return [line.replace(NBSP, " ") for line in lines]


def wrap(text: str, width: int) -> list:
    """Découpe en lignes d'au plus `width` colonnes, sans séparer « » : ; ? ! de leur mot."""
    width = max(1, width)
    result = []
    for line in unicodedata.normalize("NFC", text).split("\n"):
        result.extend(_wrap_line(line.rstrip(), width) if line.strip() else [""])
    return result
