"""Canaux du mode auto : file d'attente de Codex, réveil de Claude, état des panneaux.

Les règles sont dans rules.py. Ici, uniquement les effets : jamais de touche envoyée dans un panneau.
"""

import json
import os
import re
import select
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Callable, Optional

from . import crossread as crossread
from . import crossread_notes as notes
from . import replace as replace
from . import rules as rules
from . import state as state
from . import threads as threads
from .messages import paste_text
from .tmux import paste_to_pane, tmux

QUEUED_MESSAGE = re.compile(r"Queued message (\S+) for thread")
COMMAND_SECONDS = 60  # doit rester sous rules.SEND_SECONDS : un retour plus tardif serait pris pour un envoi perdu
SENT, FAILED, UNSURE, WITHDRAWN = "sent", "failed", "unsure", "withdrawn"
APP_SERVER = ["codex", "app-server"]
QUEUE_PAGES = 20  # pages de `thread/queue/list` lues au plus pour retrouver un message


class UncertainSend(RuntimeError):
    """`codex queue` n'a pas dit clairement si le message est en file : il est peut-être parti."""


def queue_send(thread: str, text: str, cwd: str) -> str:
    """Met un message dans la file de Codex et renvoie son identifiant. Aucune touche n'est simulée."""
    try:
        result = subprocess.run(["codex", "queue", "--thread", thread, "--message", text], cwd=cwd or None,
                                capture_output=True, text=True, timeout=COMMAND_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise UncertainSend("codex queue n'a pas rendu la main") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "codex queue a refusé le message")
    found = QUEUED_MESSAGE.search(result.stdout)
    if not found:
        raise UncertainSend("réponse de codex queue illisible")
    return found[1].rstrip(".")


def _answer(line: bytes, method: str) -> Optional[dict]:
    """Le résultat de la requête, si cette ligne du serveur est sa réponse."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("id") != 1:
        return None
    if isinstance(message.get("result"), dict):
        return message["result"]
    raise RuntimeError(f"{method} : {message.get('error')}")


def app_server(method: str, params: dict, cwd: str = "", overrides: Optional[list[str]] = None) -> dict:
    """Une requête à `codex app-server`, lancé pour l'occasion. Lève RuntimeError sans réponse exploitable."""
    process = subprocess.Popen([*APP_SERVER, *(overrides or [])], cwd=cwd or None,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + COMMAND_SECONDS
    requests = [{"id": 0, "method": "initialize",
                 "params": {"clientInfo": {"name": "samdario", "version": "1"},
                            "capabilities": {"experimentalApi": True}}},
                {"method": "initialized"},
                {"id": 1, "method": method, "params": params}]
    try:
        process.stdin.write("".join(json.dumps(request) + "\n" for request in requests).encode())
        process.stdin.flush()
        # Lecture brute : un lecteur tamponné peut garder la réponse en mémoire pendant que select attend.
        received = b""
        while True:
            *lines, received = received.split(b"\n")
            for line in lines:
                answer = _answer(line, method)
                if answer is not None:
                    return answer
            if not select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            received += chunk
        raise RuntimeError(f"{method} : pas de réponse de codex app-server")
    except OSError as exc:
        raise RuntimeError(f"{method} : {exc}") from exc
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout):
            with suppress(OSError):
                stream.close()


def queue_cancel(thread: str, queue_id: str, cwd: str) -> Optional[bool]:
    """Retire un message de la file de Codex. Vrai : retiré. Faux : il n'y était plus (pris par Codex, ou
    retiré autrement : seul le hook de Codex prouve la réception). None : pas de réponse exploitable."""
    try:
        answer = app_server("thread/queue/delete", {"threadId": thread, "queuedSubmissionId": queue_id}, cwd=cwd)
    except RuntimeError:
        return None
    deleted = answer.get("deleted")
    return deleted if isinstance(deleted, bool) else None


def queue_holds(thread: str, queue_id: str, header: str, cwd: str) -> bool:
    """Vrai si la file de ce fil contient le message `queue_id` et qu'il commence par cet en-tête de relais.
    Rien ne garantit qu'un identifiant de file soit unique d'un fil à l'autre : avant de retirer un message
    dans un autre fil que celui de la réservation, Sam & Dario vérifie qu'il est bien le sien."""
    cursor = None
    for _ in range(QUEUE_PAGES):
        try:
            answer = app_server("thread/queue/list", {"threadId": thread, "cursor": cursor}, cwd=cwd)
        except RuntimeError:
            return False
        items = answer.get("data")
        if not isinstance(items, list):
            return False
        for item in items:
            if isinstance(item, dict) and item.get("id") == queue_id:
                parts = item.get("input") if isinstance(item.get("input"), list) else []
                return any(isinstance(part, dict) and isinstance(part.get("text"), str)
                           and part["text"].startswith(header) for part in parts)
        cursor = answer.get("nextCursor")
        if not isinstance(cursor, str):
            return False
    return False


def codex_developer_instructions(cwd: str) -> str:
    """La consigne développeur que Codex aurait sans Sam & Dario, dans la configuration effective du projet.
    `-c developer_instructions=…` la remplace pour la session : Sam & Dario la reprend devant la sienne."""
    answer = app_server("config/read", {"cwd": cwd}, cwd=cwd)
    config = answer.get("config") if isinstance(answer, dict) else None
    if not isinstance(config, dict):
        raise RuntimeError("réponse de config/read sans configuration")  # illisible, pas « aucune consigne »
    value = config.get("developer_instructions")
    return value if isinstance(value, str) else ""


def codex_hooks_trusted(folder: Path) -> bool:
    """Vrai si les deux hooks d'observation de Codex, tels que Sam & Dario les a lancés, sont validés dans /hooks."""
    try:
        launched = json.loads((folder / "codex-hooks.json").read_text(encoding="utf-8"))
        with state.transaction(folder) as db:
            cwd = state.get(db, "cwd")
        answer = app_server("hooks/list", {"cwds": [cwd]}, cwd=cwd, overrides=launched["overrides"])
    except (OSError, ValueError, KeyError, RuntimeError):
        return False
    trusted = {hook.get("eventName") for entry in answer.get("data", []) for hook in entry.get("hooks", [])
               if hook.get("command") == launched["command"] and hook.get("trustStatus") == "trusted"
               and hook.get("enabled") is True}  # un hook validé mais désactivé ne signale rien
    return {"userPromptSubmit", "permissionRequest"} <= trusted


def alive(folder: Path) -> dict[str, bool]:
    """Pour chaque assistant : son panneau existe et son processus tourne encore."""
    panes = json.loads((folder / "panes.json").read_text(encoding="utf-8"))
    try:
        listed = dict(line.split() for line in tmux(folder, "list-panes", "-a", "-F", "#{pane_id} #{pane_dead}").splitlines())
    except (RuntimeError, ValueError, OSError):
        listed = {}
    return {agent: listed.get(pane) == "0" for pane, agent in panes.items()}


def _clock(now: Optional[float]) -> float:
    return time.time() if now is None else now


def paste_draft(folder: Path, agent: str, text: str) -> None:
    panes = json.loads((folder / "panes.json").read_text(encoding="utf-8"))
    pane = next((pane for pane, owner in panes.items() if owner == agent), None)
    if pane is None or tmux(folder, "display-message", "-p", "-t", pane, "#{pane_dead}/#{pane_in_mode}") != "0/0":
        raise RuntimeError(f"Le panneau de {state.NAMES[agent]} ne peut pas recevoir de brouillon.")
    paste_to_pane(folder, pane, paste_text(text), run=tmux)


def _paste_common_if_needed(folder: Path, db, number: int, now: float) -> Optional[str]:
    if state.hold(db, number) != "ready" or rules.waiter_valid(db, now):
        return None
    if rules.field_occupied(db):
        return "Envoyée à Codex. Collage en attente chez Claude."  # un seul brouillon à la fois dans son champ
    # Même transaction que le choix du canal : ni un deuxième rafraîchissement, ni le hook
    # qui vient de se réveiller ne peut remettre une seconde copie pendant le collage.
    try:
        paste_draft(folder, "claude", state.common_text(db, number))
    except (RuntimeError, OSError):
        return "Envoyée à Codex. Collage en attente chez Claude."
    state.hold(db, number, "draft")
    rules.occupy_field(db, f"common:{number}")
    return "Envoyée à Codex. Entrée dans le panneau de Claude pour l'envoyer."


def paste_relay(folder: Path, now: Optional[float] = None) -> bool:
    """Colle en brouillon chez Claude le relais qui lui revient quand aucune attente ne peut le réveiller.
    La réservation est enregistrée avant le collage, à l'heure où elle a lieu : un processus interrompu entre
    les deux ne recolle jamais, et la remise devient incertaine passé `rules.PASTE_SECONDS`."""
    now = _clock(now)
    with state.transaction(folder) as db:
        relay = rules.paste_due(db, now)
        if relay is None:
            return False
        text = state.compose(db, relay, for_paste=True)
        claim, seq = rules.reserve_paste(db, relay, text, now)
    return paste_reserved(folder, relay._replace(claim=claim), text, seq)


def paste_reserved(folder: Path, relay: state.Relay, text: str, seq: int) -> bool:
    """L'effet tmux du collage, dans une transaction : une coupure (OFF) passe avant ou après, jamais pendant.
    Si quelque chose a changé depuis la réservation (OFF, pause, Claude au travail, champ repris), rien n'est
    collé et la réservation est annulée : le relais attend la fin du tour de Claude ou la reprise."""
    with state.transaction(folder) as db:
        if not rules.paste_allowed(db, relay, seq):
            rules.paste_skipped(db, relay, seq)
            return False
        try:
            paste_draft(folder, "claude", text)
        except (RuntimeError, OSError):
            rules.paste_failed(db, relay)
            return False
        rules.pasted(db, relay.claim)
    return True


def broadcast(folder: Path, text: str, now: Optional[float] = None) -> str:
    """Ouvre ou abandonne un tour de table. La file de Codex est appelée hors transaction."""
    with state.transaction(folder) as db:
        current = state.round_open(db)
        if not text.strip():
            if text or current is None:
                return "La demande est vide."
            state.close_round(db, current)
            return "Tour de table abandonné."
        if current is not None:
            return "Tour de table en cours. Valide une ligne vide pour l'abandonner."
        thread, cwd = state.get(db, "codex_thread"), state.get(db, "cwd")
        if not thread or not alive(folder).get("codex"):
            return "Codex n'est pas joignable : rien n'a été envoyé."
        number = state.share(db, dict.fromkeys(state.NAMES, text))
        state.hold(db, number, "hold")
        codex_text = threads.with_context(db, thread, state.common(number, text), state.MAX_BODY - len(text))

    queue_id = None
    try:
        queue_id = queue_send(thread, codex_text, cwd)
        outcome = SENT
    except UncertainSend:
        outcome = UNSURE
    except (OSError, RuntimeError):
        outcome = FAILED

    with state.transaction(folder) as db:
        if state.round_open(db) == number:
            if outcome != SENT and not state.codex_received(db, number):
                if outcome == FAILED:
                    state.close_round(db, number)
                    return "Codex n'a pas pris la demande : rien n'a été envoyé."
                state.hold(db, number, "uncertain")
                return "Envoi à Codex incertain."
            state.hold(db, number, "ready")
            notice = _paste_common_if_needed(folder, db, number, _clock(now))
            return notice or "Demande envoyée aux deux."
    if queue_id is not None:
        queue_cancel(thread, queue_id, cwd)
    return "Tour de table abandonné pendant l'envoi."


def advance(folder: Path, now: Optional[float] = None) -> Optional[str]:
    """Envoie à Codex le relais qui lui revient, s'il y en a un. Renvoie l'issue de l'envoi, ou None."""
    now = _clock(now)
    with state.transaction(folder) as db:
        relay = rules.claim(db, "codex", now)
        if relay is None:
            return None
        thread, text = db.execute("""SELECT c.thread, r.pasted FROM claims c JOIN replies r ON r.id = c.reply_id
                                     WHERE c.id = ?""", (relay.claim,)).fetchone()
        cwd = state.get(db, "cwd")

    def failed() -> Optional[str]:
        with state.transaction(folder) as db:
            # Faux : Codex a déjà confirmé la réception, la remise fait foi et il n'y a rien à signaler.
            if not rules.revert(db, relay):
                return None
            current = rules.of_current_run(db, relay)
            if current:
                rules.refused(db, relay)
        if not current:
            # Le refus d'un ancien envoi n'arrête pas une boucle qui a repris : la réponse libérée repart une
            # fois sous l'activation en cours, dont le propre refus mettra en pause. Horloge réelle : cet
            # appel suit un retour de commande, pas l'événement qui a lancé l'envoi.
            advance(folder)
        return FAILED

    if not thread or not alive(folder)["codex"]:
        return failed()
    try:
        queue_id = queue_send(thread, text, cwd)
    except UncertainSend:
        with state.transaction(folder) as db:
            # Marquée incertaine, la copie se recolle par Ctrl-b R et bloque une reprise ; une réception
            # déjà confirmée par Codex fait foi.
            if not rules.unsure(db, relay, now):
                return SENT
            if rules.of_current_run(db, relay):
                rules.pause(db, "incertain")
        return UNSURE
    except RuntimeError:
        return failed()
    with state.transaction(folder) as db:
        if rules.enqueued(db, relay, queue_id, now):
            return SENT
    # Coupure pendant l'envoi, ou copie perdue de vue : cette opération est la seule chargée du retrait.
    _withdraw(folder, relay, thread, queue_id, cwd)
    return WITHDRAWN


def _cancel(folder: Path, relay: state.Relay, thread: str, queue_id: str, cwd: str) -> bool:
    """Retire la copie de la file de son fil. Quand une migration a gardé deux fils candidats, l'identifiant
    peut désigner un autre message dans l'un d'eux : chaque fil n'est vidé que s'il contient bien ce relais
    sous cet identifiant. Seul un retrait confirmé libère la réponse."""
    with state.transaction(folder) as db:
        row = db.execute("SELECT other_thread FROM claims WHERE id = ?", (relay.claim,)).fetchone()
    other = row[0] if row and row[0] != thread else ""
    if not other:
        return queue_cancel(thread, queue_id, cwd) is True
    header = state.label(relay.sender, relay.reply_id)
    return any(queue_holds(candidate, queue_id, header, cwd) and queue_cancel(candidate, queue_id, cwd) is True
               for candidate in (thread, other))


def _withdraw(folder: Path, relay: state.Relay, thread: str, queue_id: str, cwd: str) -> None:
    cancelled = _cancel(folder, relay, thread, queue_id, cwd)
    with state.transaction(folder) as db:
        applied = rules.release(db, relay, cancelled, _clock(None))
        current = rules.of_current_run(db, relay)
        # Peut-être partie, peut-être encore en file : après une coupure seule, le dire ; après une reprise,
        # ne pas arrêter la boucle.
        if applied and not cancelled and current:
            rules.pause(db, "incertain")
    if applied and cancelled and not current:
        advance(folder)  # retirée après OFF puis ON : la réponse repart une fois sous l'activation en cours


def recall(folder: Path, reply_id: int) -> Optional[str]:
    """Avant de recoller une remise incertaine vers Codex : retirer la copie automatique peut-être encore
    dans sa file. WITHDRAWN, UNSURE (sort inconnu), ou None s'il n'y a aucune copie automatique."""
    with state.transaction(folder) as db:
        row = db.execute(f"""SELECT claims.id, claims.state, claims.thread, {rules.QUEUE_ID}, replies.body FROM replies
                            JOIN claims ON claims.id = replies.claim_id
                            WHERE replies.id = ? AND replies.status = ? AND claims.recipient = 'codex'""",
                         (reply_id, state.UNCERTAIN)).fetchone()
        cwd = state.get(db, "cwd")
    if row is None:
        return None
    claim_id, claim_state, thread, queue_id, body = row
    # Sans identifiant (sortie de `codex queue` illisible), ou retrait déjà en cours : la copie peut encore partir.
    relay = state.Relay(reply_id, "claude", "codex", body, claim_id)
    if claim_state != state.UNCERTAIN or queue_id is None or not _cancel(folder, relay, thread, queue_id, cwd):
        # « Plus dans la file » ne prouve pas la réception : la tentative précédente, restée sans réponse,
        # a pu la retirer. Seul le hook de Codex prouve la réception ; la remise reste incertaine.
        return UNSURE
    with state.transaction(folder) as db:
        rules.release(db, relay, True, _clock(None))
    return WITHDRAWN


def handle_event(folder: Path, agent: str, raw: object, now: Optional[float] = None, *,
                 send_context: Optional[Callable[[str], None]] = None) -> Optional[int]:
    """Enregistre un événement d'un assistant. Pour une fin de tour de Claude, renvoie l'attente créée."""
    event = state.normalize(agent, raw)
    if event is None:
        return None
    given, now = now, _clock(now)  # `given` : None en usage réel, pour que les étapes suivantes relisent l'heure
    waiter = None
    start = restart = False
    with state.transaction(folder) as db:
        if event.kind == "reply":
            recorded = state.record_reply(db, event)
            if recorded is not None and agent == "claude":
                rules.turn_answered(db, event.session, event.request)
                waiter = rules.new_waiter(db, now)
            if recorded is not None:
                # Relancer seulement si la réponse à relayer est celle de la demande tapée (fil actif compris).
                restart = rules.answers_typed_request(db, event) and rules.restart_for(db, recorded)
                start = rules.claim_launch(db, now)
        elif event.kind == "submitted":
            awaited = replace.candidate(db, event)  # tâche 17 : relevé avant que la saisie ne l'efface
            before = replace.last_event(db)
            state.record_submitted(db, event)
            new = replace.fresh(db, event, before)  # un hook rejoué n'ajoute rien et ne remplace rien
            embedded = new and replace.confirm_embedded(db, event, awaited, before)
            # Tâche 13 : une demande tapée ouvre sa lecture croisée ; si elle répond à une décision, le panneau
            # reçoit en tête la décision de l'autre assistant qu'il n'a pas lue.
            typed = new and not embedded and crossread.note_submission(db, event)
            if typed and awaited is not None:
                replace.replace(db, event, awaited, replace.last_event(db))  # tâche 17 : la question remplace le relais
            joined = notes.answer_context(db, agent, event.session) if typed else None
            reminder = None
            if send_context is not None and agent == "codex" and state.get(db, "codex_thread") == event.session:
                # Décision jointe et rappel du fil partagent le même plafond (relecture de Codex, point 4).
                room = notes.CONTEXT_LIMIT - (len(joined) + 2 if joined else 0)
                reminder = threads.context(db, event.session, room) if room > 0 else None
            if send_context is not None and (joined or reminder):
                send_context("\n\n".join(part for part in (joined, reminder) if part))
                if reminder:
                    threads.delivered(db, event.session)
                    # Rendre le contexte au tour en cours sans attendre un appel à codex queue.
                    return None
        elif event.kind == "permission":
            state.record_permission(db, event)
        elif event.kind == "failure":
            rules.turn_failed(db, event.session, event.request, state.journal(db, event))
    if start:
        resume(folder, given, allowed=rules.launch_ready)  # lancement : les deux premières réponses sont là
    elif restart:
        # Une demande de l'utilisateur relance l'échange après un accord ou la limite, si sa réponse reste
        # celle à relayer au moment d'activer.
        resume(folder, given, allowed=lambda db: rules.restart_for(db, recorded))
    else:
        advance(folder, now)
    paste_relay(folder, given)
    return waiter


def wait(folder: Path, generation: int, clock=time.time, sleep=time.sleep, poll: float = 0.5) -> Optional[str]:
    """Attend, côté Claude, le relais à lui remettre. None si l'attente est périmée ou échue.

    Une pause ne périme pas l'attente : seuls OFF, un message soumis à Claude, une nouvelle fin de tour
    ou l'échéance le font.
    """
    while True:
        now = clock()
        with state.transaction(folder) as db:
            if int(state.get(db, "waiter")) != generation or now > float(state.get(db, "deadline")):
                return None
            question = state.take_broadcast(db, generation)
            if question is not None:
                return question
            relay = rules.claim(db, "claude", now, generation)
            if relay is not None:
                return state.compose(db, relay)
        sleep(poll)


def toggle(folder: Path, now: Optional[float] = None) -> str:
    """Ctrl-b a. Renvoie la ligne d'état à afficher."""
    return _switch(folder, now, None)


def resume(folder: Path, now: Optional[float] = None, *, allowed: Callable = rules.restartable) -> str:
    """Activation seule, pour le lancement et la relance par une demande : jamais de coupure. Rien ne se passe
    si `allowed` est faux, y compris dans la transaction qui active (un OFF entre-temps l'emporte)."""
    return _switch(folder, now, allowed)


def _switch(folder: Path, given: Optional[float], allowed: Optional[Callable]) -> str:
    now = _clock(given)
    queued, started, cwd = [], False, ""
    while True:
        with state.transaction(folder) as db:
            mode, seen_run = state.get(db, "auto"), state.get(db, "run")
            if allowed is not None and not allowed(db):
                break
            if allowed is None and mode == state.OFF and rules.autostart_pending(db):
                # Ctrl-b a avant le démarrage du lancement, même déjà engagé : l'utilisateur garde OFF.
                state.put(db, autostart=0)
                break
        active = mode == state.ON
        hooks_ok = True
        if not active:
            if not all(alive(folder).values()):
                with state.transaction(folder) as db:
                    # Reprise refusée, sans toucher au compteur ; un autre appui pendant la lecture l'emporte,
                    # y compris l'annulation du démarrage du lancement.
                    if (state.get(db, "auto"), state.get(db, "run")) == (mode, seen_run) and (
                            allowed is None or allowed(db)):
                        rules.pause(db, "suivi")
                return status(folder, now)
            hooks_ok = codex_hooks_trusted(folder)
            now = _clock(given)  # la vérification a pu durer : la suite (réservations, délais) part de maintenant
        # Un autre appui a pu basculer l'auto pendant la vérification : on agit sur l'état présent.
        with state.transaction(folder) as db:
            if allowed is not None and not allowed(db):
                break
            if active and state.get(db, "auto") != state.ON:
                continue  # déjà coupé par un autre appui : cette reprise doit passer par les vérifications
            run = int(state.get(db, "run"))
            queued = rules.toggle(db, now, hooks_ok)
            started = int(state.get(db, "run")) != run
            cwd = state.get(db, "cwd")
        break
    for item in queued:
        _withdraw(folder, item.relay, item.thread, item.queue_id, cwd)  # chaque coupure retire ce qu'elle a trouvé
    if started:
        advance(folder, now)  # un premier envoi refusé rend le compteur d'avant l'appui : voir rules.refused
    return status(folder, now)


def status(folder: Path, now: Optional[float] = None) -> str:
    """La ligne d'état. Un panneau fermé met l'auto en pause, y compris pendant l'attente d'une réponse.
    Chaque passage reprend aussi un envoi vers Codex resté prêt, par exemple quand le processus qui devait
    le renvoyer a été arrêté entre la libération de l'ancienne copie et la nouvelle réservation."""
    given, now = now, _clock(now)
    with state.transaction(folder) as db:
        launch = rules.claim_launch(db, now)
    if launch:  # démarrage du lancement interrompu (processus arrêté pendant les vérifications) : le reprendre
        resume(folder, given, allowed=rules.launch_ready)
    with state.transaction(folder) as db:
        active, run = state.get(db, "auto") == state.ON, int(state.get(db, "run"))
    if active and not all(alive(folder).values()):
        with state.transaction(folder) as db:
            rules.pause_run(db, "suivi", run)  # un appui pendant la lecture des panneaux l'emporte
    elif active:
        advance(folder, now)
    with state.transaction(folder) as db:
        current = state.round_open(db)
        if current is not None:
            _paste_common_if_needed(folder, db, current, now)
    if active and current is None:
        paste_relay(folder, given)
    with state.transaction(folder) as db:
        return rules.status_line(db, now)
