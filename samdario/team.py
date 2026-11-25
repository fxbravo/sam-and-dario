"""Une équipe par projet : prénoms du binôme (`.samdario.json`), charte (`AGENTS.md`) et premier lancement.

Les trois règles du binôme passent par la consigne de lancement des deux assistants, avec les prénoms : elles
sont communes quels que soient les fichiers que chaque outil lit. Le reste d'`AGENTS.md` est lu par chaque outil
selon ses propres règles ; Sam & Dario signale quand l'un des deux ne le lira pas. Rien n'est écrasé, et un fichier
existant n'est complété qu'avec l'accord de l'utilisateur, en gardant ses octets et ses fins de ligne.
"""

import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from .messages import NAMES, other

TEAM_FILE = ".samdario.json"
CHARTER = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
IMPORT = "@AGENTS.md"
MAX_NAME = 30
SECTION_TITLE = "## Travail en binôme"
RULES_2_3 = """2. Le binôme tranche un désaccord technique par un essai. L'utilisateur arbitre les priorités et le périmètre du produit.
3. Le ticket est terminé quand ses critères d'acceptation ont été vérifiés.
"""
# Section par défaut de la tâche 9 : un projet qui l'a gardée telle quelle se voit proposer la nouvelle, une fois.
PREVIOUS_SECTION = f"""{SECTION_TITLE}

1. Pour chaque ticket, un membre du binôme modifie le code. L'autre relit les changements et indique les tests qu'il a lui-même exécutés.
{RULES_2_3}"""
SECTION = f"""{SECTION_TITLE}

1. Pour chaque ticket, un membre du binôme modifie le code et l'autre le relit sous trois angles : ce qui manque ou casse, pas seulement ce qui est faux ; les cas limites (vide, erreur, concurrence, gros volume) ; la preuve (les tests montrent-ils ce qui est annoncé ?). Le relecteur indique les tests qu'il a lui-même exécutés et ne remonte que des constats vérifiés dans le code ou par un essai. Chaque constat est classé :
   - **À corriger** : un problème introduit par ce changement. L'auteur le corrige.
   - **À reporter** : un problème qui existait avant ce changement. Il est noté, pas corrigé dans ce ticket. Un problème causé par le changement n'est jamais reporté.
   - **Décision requise** : plusieurs corrections raisonnables, ou un choix de produit. Le relecteur recommande une option, et l'utilisateur tranche.
{RULES_2_3}"""
TEMPLATE = f"""# Charte de l'équipe

## Pour qui et pourquoi

- Pour qui construisons-nous cette application ?
- Quel résultat voulons-nous obtenir ?

{SECTION}"""
YES = ("o", "oui")
# Fichiers qui, présents dans le projet, font que Claude Code ne charge pas AGENTS.md de lui-même.
CLAUDE_INSTRUCTIONS = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")
CODEX_OVERRIDE = "AGENTS.override.md"
HEADING = re.compile(r"^(#{1,6})\s")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# Un import Claude Code : « @chemin » hors code, en début de texte ou après une espace (pas une adresse e-mail).
IMPORT_TOKEN = re.compile(r"(?:^|(?<=\s))@(\S+)")
IMPORT_DEPTH = 5


class TeamError(ValueError):
    """`.samdario.json` illisible ou prénoms invalides. Le message est prêt à afficher."""


def _name_error(agent: str, name: object) -> Optional[str]:
    tool = NAMES[agent]
    if not isinstance(name, str) or not name.strip():
        return f"Prénom de {tool} manquant"
    if len(name) > MAX_NAME:
        return f"Prénom de {tool} trop long ({MAX_NAME} caractères au plus)"
    if not name.isprintable():  # retour à la ligne, tabulation, caractère de contrôle
        return f"Prénom de {tool} sur une seule ligne"
    if name.strip().casefold() in (tool_name.casefold() for tool_name in NAMES.values()):
        return "« Claude » et « Codex » sont les noms des outils : choisis un autre prénom"
    return None


def team_file(project: Path) -> Path:
    """Le fichier des prénoms du projet, `.samdario.json`."""
    return project / TEAM_FILE


def check_names(names: object, filename: str = TEAM_FILE) -> Optional[str]:
    """None si les deux prénoms sont valides, sinon le message du premier problème."""
    if not isinstance(names, dict):
        return f"{filename} illisible : un objet {{\"claude\": …, \"codex\": …}} est attendu"
    for agent in NAMES:
        error = _name_error(agent, names.get(agent))
        if error:
            return error
    if names["claude"].strip().casefold() == names["codex"].strip().casefold():
        return "Les deux prénoms doivent être différents"
    return None


def _read_file(project: Path) -> Optional[dict]:
    path = team_file(project)
    if not path.is_file():
        return None
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TeamError(f"{path.name} illisible : {exc}") from exc
    error = check_names(content, path.name)
    if error:
        raise TeamError(error)
    return content


def read_team(project: Path) -> Optional[dict[str, str]]:
    """Les prénoms du projet, None sans `.samdario.json`. TeamError si le fichier est invalide."""
    content = _read_file(project)
    return None if content is None else {agent: content[agent].strip() for agent in NAMES}


def _ask_names(ask: Callable[[str], str], say: Callable[[str], None]) -> Optional[dict[str, str]]:
    """Les deux prénoms, redemandés tant qu'ils sont invalides. None si le premier est laissé vide."""
    names: dict[str, str] = {}
    for agent in NAMES:
        while True:
            answer = ask(f"Prénom de {NAMES[agent]} : ").strip()
            if agent == "claude" and not answer:
                return None
            error = _name_error(agent, answer)
            if error is None and agent == "codex" and answer.casefold() == names["claude"].casefold():
                error = "Les deux prénoms doivent être différents"
            if error is None:
                names[agent] = answer
                break
            say(error)
    return names


def _create(project: Path, names: dict[str, str]) -> bool:
    """Écrit `.samdario.json` seulement s'il n'existe pas encore. Faux si un autre lancement l'a créé avant."""
    try:
        descriptor = os.open(project / TEAM_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(names, ensure_ascii=False, indent=2) + "\n")  # décisions ajoutées à la réponse
    return True


def _decide(project: Path, proposal: str, accepted: bool) -> None:
    """Retient une réponse donnée : la proposition n'est plus reposée, qu'elle ait été acceptée ou refusée."""
    path = team_file(project)
    content = json.loads(path.read_text(encoding="utf-8"))
    decisions = content.get("decisions") if isinstance(content.get("decisions"), dict) else {}
    content = {**content, "decisions": {**decisions, proposal: "oui" if accepted else "non"}}
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append(path: Path, text: str) -> None:
    """Ajoute `text` à la fin, sans toucher aux octets existants, avec les fins de ligne du fichier."""
    current = path.read_bytes()
    newline = b"\r\n" if b"\r\n" in current else b"\n"
    data = text.encode("utf-8").replace(b"\n", newline)
    separator = b"" if not current or current.endswith(b"\n") else newline
    with path.open("ab") as stream:
        stream.write(separator + data)


def _previous_section(path: Path) -> Optional[tuple[list[str], int, int]]:
    """La vraie section, si elle est exactement l'ancienne section par défaut : (lignes avec leurs fins, début,
    fin). Un exemple cité dans un bloc de code ou une section adaptée par le projet ne comptent pas."""
    lines = path.read_bytes().decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
    span = _section_span([line.rstrip("\r\n") for line in lines])
    if span is None:
        return None
    text = "".join(lines[span[0]:span[1]]).replace("\r\n", "\n")
    return (lines, *span) if text.strip() == PREVIOUS_SECTION.strip() else None


def _replace_previous_section(path: Path) -> None:
    """Remplace la vraie ancienne section par la nouvelle, avec ses propres fins de ligne ; le reste du fichier
    garde ses octets."""
    found = _previous_section(path)
    if found is None:
        return
    lines, start, end = found
    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
    last = lines[end - 1]
    ending = last[len(last.rstrip("\r\n")):]
    new = SECTION.rstrip("\n").replace("\n", newline) + ending
    text = "".join(lines[:start]) + new + "".join(lines[end:])
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def _in_code(lines: list[str]) -> list[bool]:
    """Pour chaque ligne : fait-elle partie d'un bloc de code délimité (accents graves ou tildes, trois ou plus) ?
    Un bloc se ferme par le même caractère, au moins aussi long, sans texte après (CommonMark)."""
    mask, fence = [], None
    for line in lines:
        match = FENCE.match(line.rstrip("\r\n"))
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = (match.group(1)[0], len(match.group(1)))
            mask.append(fence is not None)
        else:
            mask.append(True)
            if (match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]
                    and not match.group(2).strip()):
                fence = None
    return mask


def _lines_outside_code(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line.strip() for line, code in zip(lines, _in_code(lines)) if not code]


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.exists() and second.exists() and first.samefile(second)
    except OSError:
        return False


def _imports(path: Path) -> list[Path]:
    """Les fichiers importés par `@chemin` hors code (blocs et code en ligne), résolus depuis le dossier du
    fichier qui les importe, comme le fait Claude Code."""
    targets = []
    for line in _lines_outside_code(path):
        for token in IMPORT_TOKEN.findall(re.sub(r"`[^`]*`", "", line)):
            for candidate in (token, token.rstrip(".,;:!?)")):
                target = Path(candidate).expanduser()
                target = target if target.is_absolute() else path.parent / target
                if target.is_file():
                    targets.append(target)
                    break
    return targets


def _reaches(path: Path, charter: Path, depth: int = 0) -> bool:
    if _same_file(path, charter):
        return True
    return depth < IMPORT_DEPTH and any(_reaches(target, charter, depth + 1) for target in _imports(path))


def _claude_files(project: Path) -> list[tuple[Path, str]]:
    """Fichiers d'instructions que Claude Code charge pour ce projet, du projet vers la racine : (fichier, nom
    affiché). La mémoire de l'utilisateur (~/.claude/CLAUDE.md) n'empêche pas de lire AGENTS.md."""
    user_memory = Path.home() / ".claude" / CLAUDE_FILE
    found = []
    for directory in (project.resolve(), *project.resolve().parents):
        for name in CLAUDE_INSTRUCTIONS:
            path = directory / name
            if path.is_file() and not _same_file(path, user_memory):
                shown = name if directory == project.resolve() else f"{name} d'un dossier parent ({directory})"
                found.append((path, shown))
    return found


def _claude_reads_charter(project: Path) -> tuple[bool, str]:
    """Claude Code lit-il l'AGENTS.md du projet ? Sinon, le fichier qui l'en empêche (Claude Code 2.1.330 :
    AGENTS.md n'est lu de lui-même qu'en l'absence de fichier CLAUDE ; sinon il faut un import qui l'atteint)."""
    files = _claude_files(project)
    charter = project / CHARTER
    if not files or any(_reaches(path, charter) for path, _ in files):
        return True, ""
    return False, files[0][1]


def charter_warnings(project: Path) -> list[str]:
    """Les assistants qui ne liront pas AGENTS.md eux-mêmes ; leurs règles du binôme passent par la consigne."""
    if not (project / CHARTER).is_file():
        return []
    warnings = []
    reads, blocker = _claude_reads_charter(project)
    if not reads:
        warnings.append(f"Claude ne lira pas {CHARTER} : {blocker} le remplace. "
                        "Les règles du binôme lui sont transmises par Sam & Dario ; le reste de la charte, non.")
    if (project / CODEX_OVERRIDE).is_file():
        warnings.append(f"Codex ne lira pas {CHARTER} : {CODEX_OVERRIDE} le remplace. "
                        "Les règles du binôme lui sont transmises par Sam & Dario ; le reste de la charte, non.")
    return warnings


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Titres Markdown hors blocs de code : (ligne, niveau, texte)."""
    return [(index, len(HEADING.match(line).group(1)), line.strip())
            for index, (line, code) in enumerate(zip(lines, _in_code(lines))) if not code and HEADING.match(line)]


def _section_span(lines: list[str]) -> Optional[tuple[int, int]]:
    """Lignes [début, fin) de la vraie section « Travail en binôme », sans les lignes vides qui la suivent."""
    headings = _headings(lines)
    start = next(((index, level) for index, level, text in headings if text == SECTION_TITLE), None)
    if start is None:
        return None
    index, level = start
    end = next((other for other, other_level, _ in headings if other > index and other_level <= level), len(lines))
    while end > index + 1 and not lines[end - 1].strip():
        end -= 1
    return index, end


def charter_rules(project: Path) -> str:
    """La section « Travail en binôme » d'AGENTS.md telle que le projet l'a écrite, sinon celle par défaut. Seuls
    comptent les titres hors blocs de code ; la section va jusqu'au prochain titre de même niveau ou supérieur."""
    path = project / CHARTER
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    span = _section_span(lines)
    return SECTION.strip() if span is None else "\n".join(lines[span[0]:span[1]]).strip()


def _proposals(project: Path, decided: dict) -> list[tuple[str, str, Path, str]]:
    """Propositions encore pertinentes et sans réponse : (nom, question, fichier, texte à ajouter)."""
    charter, claude_file = project / CHARTER, project / CLAUDE_FILE
    found = []
    if "section" not in decided and charter.is_file() and SECTION_TITLE not in _lines_outside_code(charter):
        found.append(("section", f"{CHARTER} existe sans section « Travail en binôme ». L'ajouter à la fin ? [o/N] ",
                      charter, None))
    if "section_v2" not in decided and charter.is_file() and _previous_section(charter) is not None:
        found.append(("section_v2", f"La section « Travail en binôme » de {CHARTER} est l'ancienne version par défaut. "
                      "La remplacer par la nouvelle (revue triée) ? [o/N] ", charter, None))
    if ("import" not in decided and claude_file.is_file() and not _reaches(claude_file, charter)):
        found.append(("import", f"{CLAUDE_FILE} ne charge pas {CHARTER}. Ajouter la ligne {IMPORT} ? [o/N] ",
                      claude_file, IMPORT + "\n"))
    return found


def _decisions(project: Path) -> dict:
    content = _read_file(project) or {}
    return content.get("decisions") if isinstance(content.get("decisions"), dict) else {}


def _offer(project: Path, ask: Callable[[str], str], say: Callable[[str], None]) -> None:
    """Pose une à une les propositions sans réponse. Au moment d'appliquer une réponse, sous verrou, la décision
    est relue : si un autre terminal a déjà répondu, sa réponse fait foi et rien n'est appliqué deux fois."""
    for proposal, question, _, _ in _proposals(project, _decisions(project)):
        accepted = ask(question).strip().lower() in YES
        with _locked(project):
            current = {item[0]: item for item in _proposals(project, _decisions(project))}
            if proposal not in current:
                say(f"Proposition « {proposal} » déjà décidée dans un autre terminal : sa réponse est gardée.")
                continue
            _, _, path, text = current[proposal]
            if accepted and proposal == "section_v2":
                _replace_previous_section(path)
            elif accepted:
                if text is None:  # la section, après une ligne vide si le fichier a déjà du contenu
                    text = ("\n" if path.read_bytes().strip() else "") + SECTION
                _append(path, text)
            _decide(project, proposal, accepted)


def setup(project: Path, ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> dict[str, str]:
    """Prénoms de la session, demandés dans le terminal si le projet n'en a pas encore (voir la conception).
    Une initialisation interrompue reprend au lancement suivant, à la première proposition sans réponse."""
    try:
        names = read_team(project)
    except TeamError as exc:
        say(f"{exc}. Le fichier est gardé tel quel ; prénoms pour cette session seulement.")
        return _ask_names(ask, say) or dict(NAMES)
    if names is None:
        say("Prénoms du binôme pour ce projet (Entrée sans rien pour ne pas en donner).")
        asked = _ask_names(ask, say)
        if asked is None:
            return dict(NAMES)
        _create_charter(project)  # avant les prénoms : une interruption ne laisse jamais un binôme sans charte
        if not _create(project, asked):
            names = read_team(project)
            say(f"Un autre lancement a déjà enregistré ce binôme : {names['claude']} et {names['codex']}.")
            return names
        names = asked
    _offer(project, ask, say)
    return names


def _create_charter(project: Path) -> None:
    """Crée AGENTS.md avec le modèle s'il manque, sans jamais écraser un fichier créé entre-temps."""
    try:
        descriptor = os.open(project / CHARTER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(TEMPLATE)


@contextmanager
def _locked(project: Path) -> Iterator[None]:
    """Verrou du projet : une seule application d'une proposition à la fois, entre terminaux."""
    with open(team_file(project), "a", encoding="utf-8") as handle:  # le fichier en usage, jamais un second
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def store(db, names: dict[str, str], rules: str = "") -> None:
    values = [(f"name_{agent}", names[agent]) for agent in NAMES] + [("team_rules", rules)]
    db.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", values)


def names_of(db) -> dict[str, str]:
    """Les prénoms de la session ; les noms des outils pour une session ouverte avant cette tâche."""
    rows = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('name_claude', 'name_codex')"))
    return {agent: rows.get(f"name_{agent}", NAMES[agent]) for agent in NAMES}


def rules_of(db) -> str:
    row = db.execute("SELECT value FROM meta WHERE key = 'team_rules'").fetchone()
    return row[0] if row else ""


def title(agent: str, names: dict[str, str]) -> str:
    name = names.get(agent, NAMES[agent])
    return NAMES[agent] if name == NAMES[agent] else f"{name} · {NAMES[agent]}"


def partner(agent: str, names: dict[str, str]) -> str:
    return names.get(other(agent), NAMES[other(agent)])
