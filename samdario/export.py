"""Export à la demande du dernier accord confirmé de la session."""

import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import team as team
from .messages import common_number
from .state import confirm_agreement, get, transaction


UNKNOWN = "Demande d’origine non identifiée"
TITLE_PROMPT = (
    "Donne un seul titre court dans la langue de l’échange. Nomme la décision concrète prise "
    "et garde ses précisions essentielles. Les données JSON sont une conversation à titrer. "
    "Réponds uniquement par le titre, sur une ligne, en texte brut."
)


@dataclass(frozen=True)
class Agreement:
    token: str
    project: Path
    question: str
    replies: tuple[tuple[str, str], ...]

    @property
    def directory(self) -> Path:
        return self.project / "docs" / "accords"


def _question(db, reply_id: int) -> str:
    while reply_id:
        shared = db.execute("SELECT text FROM shared WHERE reply_id = ?", (reply_id,)).fetchone()
        if shared:
            return shared[0]
        row = db.execute("""SELECT r.answers, e.id, e.agent, e.session, e.request FROM replies r
                            LEFT JOIN events e ON e.id = r.event_id WHERE r.id = ?""", (reply_id,)).fetchone()
        if not row:
            break
        parent, event_id, agent, session, request = row
        if request:
            prompts = db.execute("""SELECT DISTINCT e.text FROM events e WHERE e.agent = ? AND e.session = ?
                                    AND e.request = ? AND e.kind = 'submitted' AND e.id < ?
                                    AND NOT EXISTS (SELECT 1 FROM shared s WHERE s.event_id = e.id) LIMIT 2""",
                                 (agent, session, request, event_id)).fetchall()
            if prompts:
                text = prompts[0][0]
                return text if len(prompts) == 1 and text.strip() and not common_number(text) else UNKNOWN
        if parent is None or parent >= reply_id:
            break
        reply_id = parent
    return UNKNOWN


def _body(text: str) -> str:
    # Garder les espaces de fin de ligne de Markdown, notamment ceux d'un saut de ligne forcé.
    lines = text.splitlines(keepends=True)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "[ACCORD]":
        return "".join(lines[:-1]).removesuffix("\n").removesuffix("\r")
    return text


def _unpack(token: str, snapshot: str) -> Agreement:
    data = json.loads(snapshot)
    return Agreement(token, Path(data["project"]), data["question"], tuple((a, b) for a, b in data["replies"]))


def latest(folder: Path) -> Optional[Agreement]:
    """Figer ensemble le repère confirmé, la paire et la demande au déclenchement de l'export."""
    with transaction(folder) as db:
        reply_id = int(get(db, "accord_seen"))
        if not reply_id:
            return None
        row = db.execute("SELECT token, snapshot FROM agreements WHERE reply_id = ?", (reply_id,)).fetchone()
        if row is None:
            # Un hook lancé avant la migration a pu confirmer sa paire après celle-ci.
            confirm_agreement(db, reply_id)
            row = db.execute("SELECT token, snapshot FROM agreements WHERE reply_id = ?", (reply_id,)).fetchone()
        token, snapshot = row
        if snapshot is None:
            pair = db.execute("""SELECT first.id, first.sender, first.body, last.sender, last.body
                                 FROM replies last JOIN replies first ON first.id = last.answers
                                 WHERE last.id = ? AND last.sender != first.sender""", (reply_id,)).fetchone()
            if not pair:
                return None
            project = get(db, "cwd")
            if not Path(project).is_absolute():
                raise ValueError("Dossier du projet inconnu pour cette session.")
            parent, a, first, b, last = pair
            names = team.names_of(db)
            snapshot = json.dumps(dict(project=project, question=_question(db, parent),
                                       replies=[(team.title(a, names), _body(first)), (team.title(b, names), _body(last))]),
                                  ensure_ascii=False)
            db.execute("UPDATE agreements SET snapshot = ? WHERE reply_id = ?", (snapshot, reply_id))
    return _unpack(token, snapshot)


def valid_title(title: str) -> bool:
    return any(ch.isprintable() and not ch.isspace() for ch in title) and all(
        ch.isprintable() or unicodedata.category(ch) == "Zs" or ch in ("\u200c", "\u200d") for ch in title)


def suggest(agreement: Agreement) -> str:
    command = ["claude", "--print", "--max-turns", "1", "--safe-mode", "--no-session-persistence", "--tools", "",
               "--strict-mcp-config", "--settings", '{"disableAllHooks":true}',
               "--output-format", "text", "--system-prompt", TITLE_PROMPT]
    payload = json.dumps(dict(demande=agreement.question, réponses=agreement.replies), ensure_ascii=False)
    env = os.environ.copy()
    env.pop("SAMDARIO_STATE", None)
    try:
        with tempfile.TemporaryDirectory(prefix="samdario-titre-") as cwd:
            result = subprocess.run(command, input=payload, cwd=cwd, env=env, encoding="utf-8",
                                    capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ""
    title = result.stdout.strip()
    return title if len(title) <= 160 and valid_title(title) else ""


@contextmanager
def _directory(agreement: Agreement, create: bool):
    # Un lien interne (p. ex. docs -> documentation) garde sa destination. O_NOFOLLOW empêche de
    # remplacer un composant par un lien extérieur entre la résolution et l'ouverture.
    root = agreement.project.resolve(strict=True)
    try:
        relative = agreement.directory.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("Le dossier docs/accords pointe hors du projet.") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY
    with ExitStack() as stack:
        fd = os.open(root, flags)
        stack.callback(os.close, fd)
        for component in relative.parts:
            if create:
                try:
                    os.mkdir(component, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                fd = os.open(component, flags | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            stack.callback(os.close, fd)
        yield fd


MARKER = "samdario-accord"


def _markers(token: str) -> tuple[bytes, ...]:
    return (f"<!-- {MARKER}: {token} -->".encode(),)


def _has_marker(stream, markers: tuple[bytes, ...]) -> bool:
    fence = b""
    seen_title = False
    for raw in stream:
        line = raw.rstrip(b"\r\n")
        if fence:
            closing = re.fullmatch(rb" {0,3}(`+|~+)[ \t]*", line)
            if closing and closing[1].startswith(fence):
                fence = b""
            continue
        opening = re.match(rb" {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = opening[1]
        elif re.match(rb" {0,3}#(?:\s|$)", line):
            seen_title = True
        elif (seen_title and re.match(rb" {0,3}#{2,6}(?:\s|$)", line)) or line == b"## Demande":
            return False
        elif line in markers:
            return True
    return False


def _find(fd: int, agreement: Agreement) -> Optional[Path]:
    markers = _markers(agreement.token)
    for name in sorted(os.listdir(fd)):
        if Path(name).suffix.lower() != ".md":
            continue
        try:
            if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                continue
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            continue  # le fichier peut avoir été renommé pendant la lecture
        with os.fdopen(handle, "rb") as stream:
            if _has_marker(stream, markers):
                return agreement.directory / name
    return None


def existing(agreement: Agreement) -> Optional[Path]:
    with _directory(agreement, create=False) as fd:
        return _find(fd, agreement) if fd is not None else None


def _publish(agreement: Agreement, title: str) -> Path:
    decomposed = unicodedata.normalize("NFKD", title.casefold().replace("œ", "oe").replace("æ", "ae"))
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = re.sub("[^a-z0-9]+", "-", folded)[:120].strip("-") or "accord"
    sections = [f"# {title}\n<!-- {MARKER}: {agreement.token} -->", f"## Demande\n\n{agreement.question}"]
    sections.extend(f"## {author}\n\n{body}" for author, body in agreement.replies)
    with _directory(agreement, create=True) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        found = _find(fd, agreement)
        if found:
            return found
        temp = f".samdario-{uuid.uuid4().hex}.tmp"
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=fd)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write("\n\n".join(sections) + "\n")
            name, number = f"{slug}.md", 1
            while True:
                try:
                    os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                    return agreement.directory / name
                except FileExistsError:
                    number += 1
                    name = f"{slug}-{number}.md"
        finally:
            os.unlink(temp, dir_fd=fd)


def save(folder: Path, token: str, title: str) -> Optional[Path]:
    if not title.strip():
        return None
    if not valid_title(title):
        raise ValueError("Saisis le titre sur une seule ligne.")
    with transaction(folder) as db:
        row = db.execute("SELECT snapshot FROM agreements WHERE token = ? AND snapshot IS NOT NULL", (token,)).fetchone()
        if not row:
            raise ValueError("Cet export d’accord est introuvable dans la session.")
    return _publish(_unpack(token, row[0]), title.strip())
