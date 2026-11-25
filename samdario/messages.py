"""Messages de Sam & Dario, en-têtes et reconnaissance des textes collés ou soumis."""

import re
from typing import NamedTuple, Optional


NAMES = {"claude": "Claude", "codex": "Codex"}
SESSION_KEYS = {"claude": "claude_session", "codex": "codex_thread"}
NEW, DRAFTED, QUEUED, SENT = "new", "drafted", "queued", "sent"
REPLACED = "replaced"  # tâche 17 : relais remplacé par une question tapée, jamais relayé seul, repris en contexte
UNCERTAIN = "uncertain"  # remise à Claude jamais confirmée : la trace reste, l'auto ne la retente pas
PASTED = "pasted"  # réservation d'un relais que l'auto a collé chez Claude : l'utilisateur peut y ajouter du texte
PASTING = "pasting"  # réservation enregistrée avant l'effet tmux : collage en cours, ou interrompu
RELAY_LABEL = re.compile(r"\[Réponse de (Claude|Codex) #([1-9][0-9]{0,18})\]")
# En-tête d'un relais : l'étiquette finit sa ligne, seule ou après la balise d'un collage de Claude Code, ou
# après le texte de réveil de Claude Code (observé : `Stop hook blocking error from command "Stop": `). Une
# étiquette citée dans une phrase n'est pas un relais.
RELAY_HEADER = re.compile(r'^[ \t]*(?:<pasted_content[^>\n]*>[ \t]*'
                          r'|[^\n]*Stop hook blocking error from command "[^"\n]*":[ \t]*)?'
                          + RELAY_LABEL.pattern + r"[ \t]*$", re.MULTILINE)
COMMON_LINE = r'\[Demande commune #([1-9][0-9]{0,18})\][ \t]*(?:\n|$)'
COMMON_HEADER = re.compile(r'\s*(?:<system-reminder>\s*)?(?:<pasted_content\b[^>\n]*>\s*)?'
                           r'(?:Stop hook blocking error from command "Stop":[ \t]*)?'
                           + COMMON_LINE)
TASK_WAKE_HEADER = re.compile(r'\s*<system-reminder>\s*Stop hook blocking error from command "Stop":[ \t]*'
                              + COMMON_LINE)
PASTE_WRAPPER = re.compile(r"</?pasted_content(?:\s+[^>]*)?>")
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
MAX_BODY = 100_000


class Event(NamedTuple):
    agent: str
    kind: str
    session: str
    turn: Optional[str]
    text: str
    prompt: str = ""
    request: str = ""


class Relay(NamedTuple):
    reply_id: int
    sender: str
    recipient: str
    body: str
    claim: int = 0  # identifiant de la réservation, quand le relais vient de `claim`


class ReferencedReply(NamedTuple):
    reply_id: int
    status: str
    pasted: Optional[str]
    body: str
    target: str = ""  # fil destinataire enregistré lors de la remise


def other(agent: str) -> str:
    return {"claude": "codex", "codex": "claude"}[agent]


def label(sender: str, reply_id: int) -> str:
    return f"[Réponse de {NAMES[sender]} #{reply_id}]"


def common(round_id: int, text: str) -> str:
    return f"[Demande commune #{round_id}]\n\n{text}"


def instructions(agent: str, names: Optional[dict] = None, rules: str = "") -> str:
    """La consigne de Sam & Dario. Avec des prénoms, elle dit qui est qui ; les en-têtes gardent le nom d'outil."""
    partner = NAMES[other(agent)]
    if names and (names.get(agent), names.get(other(agent))) != (NAMES[agent], partner):
        opening = (f"[Sam & Dario] Tu es {names[agent]} ({NAMES[agent]}). "
                   f"Tu travailles en binôme avec {names[other(agent)]} ({partner}). ")
        charter = "\nVotre charte commune est dans AGENTS.md."
        if rules:  # les règles du binôme, identiques pour les deux quel que soit ce que chaque outil lit
            charter += " Vos règles de travail en binôme :\n" + rules
    else:
        opening, charter = f"[Sam & Dario] Tu travailles en binôme avec {partner} sur cette demande. ", ""
    return (
        opening + "L’utilisateur relaie vos réponses avec Sam & Dario.\n"
        f"Un relais de {partner} porte un en-tête comme « [Réponse de {partner} #7] ». "
        "Le numéro identifie le message. Ce relais fait partie de la conversation, "
        "même s’il arrive comme retour d’un hook (« Stop hook feedback »).\n"
        f"Le relais peut rappeler les demandes de l’utilisateur à {partner} et ses réponses "
        "depuis le dernier relais, avant de donner sa réponse la plus récente.\n"
        "Une demande marquée « [Demande commune #n] » est posée aux deux. "
        f"Elle peut arriver comme retour d'un hook. Réponds-y de ton côté, avant de lire l'avis de {partner}.\n"
        "Ne te range pas à une proposition par politesse : un accord ne vaut que s’il a été mis à l’épreuve. "
        "Avant d’écrire [ACCORD], cherche la plus forte objection à la solution sur laquelle vous convergez : "
        "un cas qui la casse, une hypothèse non vérifiée, une meilleure option écartée trop vite. "
        "Écris-la sur une ligne « Objection la plus forte : … », puis dis pourquoi elle ne tient pas. "
        "Si elle tient, n’écris pas [ACCORD] : propose ce qu’il faut changer. "
        "Quand tu es entièrement d’accord et que tu n’as plus rien à ajouter, "
        "termine ton message par une ligne contenant exactement [ACCORD].\n"
        "Si une décision revient à l’utilisateur (plusieurs corrections raisonnables, ou un choix de produit), "
        "présente les options et ta recommandation, puis termine ton message par une ligne contenant "
        "exactement [DÉCISION], à la place de [ACCORD]. Sam & Dario s’arrête alors jusqu’à sa réponse."
        + charter
    )


def common_number(text: str) -> Optional[str]:
    """Le numéro en tête de la demande, y compris dans la consigne de lancement ou le réveil de Claude."""
    text = text.replace("\r\n", "\n").lstrip()
    if text.startswith("[Sam & Dario] "):
        _, _, text = text.partition("\n\n")
    if text.startswith("<task-notification>"):
        _, closed, reminder = text.partition("</task-notification>")
        match = TASK_WAKE_HEADER.match(reminder) if closed else None
    else:
        match = COMMON_HEADER.match(text)
    return match[1] if match else None


def paste_text(text: str) -> str:
    # Échap doit rester visible dans un collage encadré par le terminal.
    return CONTROLS.sub(lambda match: f"\\x{ord(match[0]):02x}", text.replace("\r\n", "\n"))


def journal(db, event: Event, kind: Optional[str] = None) -> int:
    return db.execute("INSERT INTO events (agent, kind, session, turn, text, request) VALUES (?, ?, ?, ?, ?, ?)",
                      (event.agent, kind or event.kind, event.session, event.turn, event.text, event.request)).lastrowid


def relay_headers(text: str, db=None) -> list[tuple[str, str]]:
    """Les étiquettes placées en tête d'un relais, dans l'ordre et sans doublon : (nom, numéro). Avec la base,
    le texte d'une réponse relayée est reconnu derrière son en-tête : une étiquette citée dans ce texte, même
    seule sur sa ligne ou dans un bloc de code, n'est pas un en-tête."""
    text = text.replace("\r\n", "\n")
    headers, covered = [], 0
    for match in RELAY_HEADER.finditer(text):
        if match.start() < covered:
            continue
        header = match.group(1, 2)
        headers.append(header)
        for block in _relayed_texts(db, *header) if db is not None else ():
            position = text.find(block, match.start())
            if position >= 0:
                covered = max(covered, position + len(block))
    return list(dict.fromkeys(headers))


def _relayed_texts(db, name: str, number: str) -> list[str]:
    """Les formes possibles du texte relayé sous cette étiquette : brouillon collé, puis réponse brute."""
    senders = {display: agent for agent, display in NAMES.items()}
    row = db.execute("SELECT body, pasted FROM replies WHERE id = ? AND sender = ?",
                     (number, senders[name])).fetchone()  # texte : un numéro trop grand ne trouve rien
    if row is None:
        return []
    body, pasted = row
    return [block for block in dict.fromkeys((pasted, paste_text(body), body)) if block and block.strip()]


def referenced_replies(db, event: Event, candidates: bool = True) -> list[ReferencedReply]:
    """Les réponses dont ce message porte l'en-tête de relais. `candidates` : accepter aussi l'autre fil qu'une
    migration a gardé pour une copie (faux pendant les migrations d'avant le suivi des copies)."""
    sender = other(event.agent)
    replies = []
    for name, number in relay_headers(event.text, db):
        if name != NAMES[sender]:
            continue
        row = db.execute("""SELECT id, status, pasted, body, target_session FROM replies
                            WHERE id = ? AND sender = ? AND status NOT IN (?, ?) AND target_session IN ('', ?)
                              AND event_id IS NOT NULL""", (number, sender, NEW, REPLACED, event.session)).fetchone()
        if row is None and candidates:
            # Copie d'une base du plan 17 dont le fil reste à établir : l'autre fil candidat vaut destinataire.
            row = db.execute("""SELECT replies.id, replies.status, replies.pasted, replies.body, claims.other_thread
                                FROM replies JOIN claims ON claims.id = replies.claim_id
                                WHERE replies.id = ? AND replies.sender = ? AND replies.status != ?
                                  AND claims.other_thread = ? AND replies.event_id IS NOT NULL""",
                             (number, sender, NEW, event.session)).fetchone()
        if row:
            replies.append(ReferencedReply(*row))
    return replies


def journal_input(db, event: Event, replies: list[ReferencedReply], legacy: bool = False) -> None:
    if not replies:
        journal(db, event)
        return

    journal(db, event, "relay")
    if legacy or any(reply.status == DRAFTED or _pasted_by_auto(db, reply.reply_id) for reply in replies):
        addition = _manual_addition(event, replies)
        if addition:
            journal(db, event._replace(text=addition), "submitted")


def _pasted_by_auto(db, reply_id: int) -> bool:
    """Vrai si l'auto a collé cette réponse chez Claude : quelle que soit l'issue de sa remise (en cours,
    collée, incertaine), le texte que l'utilisateur tape autour reste une demande à transmettre."""
    row = db.execute("SELECT value FROM meta WHERE key = 'claude_field'").fetchone()
    return row is not None and row[0] == f"relay:{reply_id}"


def _manual_addition(event: Event, replies: list[ReferencedReply]) -> str:
    snapshots = [reply.pasted or paste_text(f"{label(other(event.agent), reply.reply_id)}\n\n{reply.body}")
                 for reply in replies]
    remainder = event.text.replace("\r\n", "\n")
    quoted_lines = set()
    for snapshot in sorted(snapshots, key=len, reverse=True):
        if snapshot in remainder:
            # Retirer le bloc entier conserve les paragraphes et le code ajoutés de part et d'autre.
            remainder = remainder.replace(snapshot + "\n", "").replace(snapshot + " ", "").replace(snapshot, "")
        else:
            # L'utilisateur peut aussi retoucher le brouillon ligne par ligne avant Entrée.
            quoted_lines.update(snapshot.splitlines())
    lines = [line for line in remainder.splitlines() if line not in quoted_lines
             and not PASTE_WRAPPER.fullmatch(line.strip()) and not RELAY_LABEL.fullmatch(line.strip())]
    addition = "\n".join(lines).strip("\n")
    return addition if addition.strip() else ""


DECISION_REASON = "décision requise : à toi de trancher"


def decision_reply(db) -> Optional[int]:
    """Dernière décision des fils actifs qui attend encore une saisie de l'utilisateur."""
    rows = db.execute("""SELECT r.id, r.body, r.event_id FROM replies r
                         JOIN meta m ON m.key = CASE r.sender WHEN 'codex' THEN 'codex_thread' ELSE 'claude_session' END
                                    AND m.value = r.session ORDER BY r.id DESC""")
    row = db.execute("SELECT value FROM meta WHERE key = 'decision_seen'").fetchone()
    seen = int(row[0]) if row else 0
    for reply_id, body, event_id in rows:
        if reply_id <= seen:
            return None
        lines = body.rstrip().splitlines()
        if lines and lines[-1].strip() == "[DÉCISION]":
            answered = db.execute("""SELECT 1 FROM events e WHERE e.kind = 'submitted' AND e.id > ?
                                     AND NOT EXISTS (SELECT 1 FROM shared s WHERE s.event_id = e.id) LIMIT 1""",
                                  (event_id or 0,)).fetchone()
            return None if answered else reply_id
    return None
