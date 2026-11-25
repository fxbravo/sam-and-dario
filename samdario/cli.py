"""Ouvre Claude Code et Codex dans tmux pour travailler sur une demande commune."""

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from . import auto as auto
from . import bar as bar
from . import export as export
from . import scene as scene
from . import rules as rules
from . import team as team
from .messages import instructions
from .rules import WAIT_SECONDS
from .state import (
    DRAFTED, NAMES, QUEUED, REPLACED, SENT, UNCERTAIN, Relay, common, common_text, draft, get, hold, initialize,
    latest_reply,
    normalize,
    other, paste_text, put, record_permission, record_reply, record_submitted, round_open, share, transaction,
)
from .tmux import paste_to_pane, tmux, tmux_command
from . import __version__


# Tâche 21 : les hooks et les boutons relancent le paquet installé, en mode isolé. Python ne cherche alors pas
# dans le dossier du projet ouvert, où un samdario.py ne peut pas masquer l'application.
COMMAND = [sys.executable, "-I", "-m", "samdario"]
INSTALL = "pipx install git+https://github.com/fxbravo/sam-and-dario.git"
SESSION = "samdario"  # session tmux de chaque lancement, sur son propre serveur
STATE_VARIABLE = "SAMDARIO_STATE"  # dossier de session transmis aux hooks
CHANNELS = "codex-hooks.json"  # écrit au lancement : réglages des hooks Codex, et preuve que les canaux sont branchés


def helper(action, state, *args):
    return [*COMMAND, action, str(state), *args]


def tmux_shell(action: str, state: Path) -> str:
    """Protège les chemins pour le shell et les dièses littéraux pour les formats tmux."""
    return shlex.join(helper(action, state)).replace("#", "##")


def bind_broadcast(state: Path) -> None:
    # Le bloc tmux est analysé avant la saisie : %1 reste un seul argument de set-option.
    # run-shell fige la valeur avant de rendre la main. Entre apostrophes shell, une apostrophe
    # littérale s'écrit '"'"' ; cela préserve aussi un argument vide, les tabulations et les dièses.
    question = """'#{s|'|'"'"'|:@samdario-question}'"""
    tmux(state, "set-option", "-g", "@samdario-both", f"{tmux_shell('_both', state)} {question}")
    tmux(state, "source-file", "-", data=f"bind-key b {BROADCAST_PROMPT}\n".encode())


# La ligne « Pour les deux », écrite une seule fois : liée à ^b b et au bouton de la barre (tâche 15).
BROADCAST_PROMPT = """command-prompt -p 'Pour les deux :' {
    set-option -g -- @samdario-question '%1'
    run-shell -b '#{E:@samdario-both}'
    set-option -gu @samdario-question
}"""


def bind_buttons(state: Path) -> None:
    """Les boutons de la barre (tâche 15) : un clic aiguille selon la zone vers la même commande que le raccourci.
    Les commandes shell passent par des options, comme `@samdario-both` : aucune apostrophe à protéger ici."""
    relay = tmux_shell("_relay", state) + ' "#{pane_id}"'
    for name, command in (("auto", tmux_shell("_toggle", state)), ("coller", relay), ("recoller", relay + " force"),
                          ("exporter", tmux_shell("_export_accord", state) + " #{q:client_name}")):
        tmux(state, "set-option", "-g", f"@samdario-cmd-{name}", command)
    branches = {"vue": TOGGLE, "auto": "run-shell -b '#{E:@samdario-cmd-auto}'", "deux": BROADCAST_PROMPT,
                "coller": "run-shell -b '#{E:@samdario-cmd-coller}'",
                "recoller": "run-shell -b '#{E:@samdario-cmd-recoller}'",
                "exporter": "run-shell -b '#{E:@samdario-cmd-exporter}'",
                "fermer": 'confirm-before -c o -p "Fermer Sam & Dario ? (o/n)" detach-client'}
    block = "".join(f'    if-shell -F "#{{==:#{{mouse_status_range}},{name}}}" {{\n        {command}\n    }}\n'
                    for name, command in branches.items())
    # Un second clic rapide est un double-clic pour tmux, un troisième un triple-clic : chacun garde son action.
    bindings = "".join(f"bind-key -n {event} {{\n{block}}}\n"
                       for event in ("MouseDown1Status", "DoubleClick1Status", "TripleClick1Status"))
    tmux(state, "source-file", "-", data=bindings.encode())


def bind_export(state: Path) -> None:
    tmux(state, "bind-key", "e", "run-shell", "-b", tmux_shell("_export_accord", state) + " #{q:client_name}")


def _tmux_literal(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def title_prompt(state: Path, client: str, proposal: str) -> str:
    # -I interprète les formats et les dates. Une option par invite fournit le texte brut, même pour "%Y".
    initial = "@samdario-titre-" + uuid.uuid4().hex
    prompt = "Titre de l’accord " if proposal else "Saisis un titre pour l’accord "
    # Le bloc est analysé avant la saisie. Le titre validé ressort sur stdout comme un argument littéral.
    command = (f"set-option -g -- {initial} {_tmux_literal(proposal)}\n"
               f"command-prompt -l -t {_tmux_literal(client)} -p '{prompt}' -I '#{{{initial}}}' {{\n"
               "    display-message -p -l -- '%1'\n}\n"
               f"set-option -gu {initial}\n")
    return tmux(state, "source-file", "-", data=command.encode())


TITLE_PROMPT_TMUX = (3, 6)  # `command-prompt -l`, qui garde le titre tapé tel quel, date de tmux 3.6


def server_version(state: Path) -> str:
    """La version du serveur tmux de la session, qui fait foi pour `command-prompt` : un client mis à jour pendant
    la session peut être plus récent que lui. À défaut de réponse du serveur, celle de la commande `tmux`."""
    try:
        return tmux(state, "display-message", "-p", "#{version}")
    except (RuntimeError, OSError):
        try:
            found = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return ""
        return (found.stdout or "").replace("tmux", "").strip()


def title_editable(state: Path) -> bool:
    """Vrai si le tmux de la session sait saisir le titre d'un accord (3.6 ou ultérieur). Ubuntu 24.04, donc WSL,
    fournit tmux 3.4 : l'accord y est enregistré dès l'export. Une version illisible compte comme récente
    (développement de tmux)."""
    version = re.search(r"(\d+)\.(\d+)", server_version(state))
    return version is None or (int(version[1]), int(version[2])) >= TITLE_PROMPT_TMUX


def save_accord(state: Path, token: str, title: str) -> str:
    path = export.save(state, token, title)
    return str(path) if path else ""


def export_accord(state: Path, client: str) -> str:
    agreement = export.latest(state)
    if agreement is None:
        return "Aucun accord à exporter"
    found = export.existing(agreement)
    if found:
        return str(found)
    tmux(state, "display-message", "-l", "-c", client, "Claude prépare un titre…")
    proposal = export.suggest(agreement)
    if not title_editable(state):
        path = save_accord(state, agreement.token, proposal or "Accord")
        if not path:
            return ""
        version = server_version(state) or "ancien"
        origin = "titre proposé par Claude" if proposal else "titre par défaut « Accord », faute de proposition de Claude"
        # Les versions d'abord : sur un terminal étroit, le chemin peut déborder de la barre.
        return (f"tmux {version} : titre non modifiable (il faut tmux 3.6 ou plus récent) · {origin} · {path}"
                " · renomme le fichier si besoin")
    title = title_prompt(state, client, proposal)
    return save_accord(state, agreement.token, title)


def export_action(state: Path, client: str, *selection: str) -> None:
    try:
        result = save_accord(state, *selection) if selection else export_accord(state, client)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        result = f"Export de l’accord impossible · {exc}"
    if result:
        tmux(state, "display-message", "-l", "-c", client, "--", result)


def prompt(question: str, agent: str, round_id: int = 1) -> str:
    """Le premier message d'un assistant : la demande commune seule. La consigne passe par les options de
    lancement (`agent_command`), et tient donc après un /clear chez Claude ou un /new dans Codex."""
    return common(round_id, question)


def capture(state, sender, raw):
    event = normalize(sender, raw)
    if event is None:
        return
    with transaction(state) as db:
        if event.kind == "reply":
            record_reply(db, event)
        elif event.kind == "submitted":
            record_submitted(db, event)
        elif event.kind == "permission":
            record_permission(db, event)


def codex_hook_settings():
    """La commande fixe des hooks d'observation de Codex, et les réglages qui les déclarent au lancement."""
    command = shlex.join([*COMMAND, "_event", "codex"])
    hooks = '[{hooks=[{type="command",command=' + json.dumps(command, ensure_ascii=False) + '}]}]'
    overrides = []
    for name in ("UserPromptSubmit", "PermissionRequest"):
        overrides.extend(["-c", f"hooks.{name}={hooks}"])
    return {"command": command, "overrides": overrides}


def agent_command(state, agent, question, names=None, rules="", codex_existing=""):
    # Le dossier de session passe par SAMDARIO_STATE pour que la commande des hooks reste fixe.
    rules_text = instructions(agent, names or dict(NAMES), rules)
    callback = [*COMMAND, "_event", agent]
    if agent == "claude":
        hook = {"type": "command", "command": shlex.join(callback)}
        # La fin de tour tourne en arrière-plan : elle capte la réponse, puis attend le relais de Codex
        # et réveille Claude avec le code 2, sans aucune touche simulée.
        waiting = dict(hook, asyncRewake=True, timeout=WAIT_SECONDS + 60)
        # StopFailure remplace Stop quand l'API échoue (limite d'usage…) : Claude Code ignore sa sortie.
        settings = {"hooks": {"Stop": [{"hooks": [waiting]}], "UserPromptSubmit": [{"hooks": [hook]}],
                              "StopFailure": [{"hooks": [hook]}]}}
        return ["claude", "--settings", json.dumps(settings), "--append-system-prompt", rules_text, "--", question]
    notify = json.dumps(callback, ensure_ascii=False)
    # Une chaîne JSON est une chaîne TOML valide ; la commande des hooks reste inchangée (pas de nouvelle revue).
    overrides = ["codex", "--no-daemon", "-c", f"notify={notify}", *codex_hook_settings()["overrides"]]
    if codex_existing is None:  # consigne native illisible : ne jamais la remplacer sans l'avoir lue
        return [*overrides, "--", question]
    # L'option remplace la consigne développeur du projet : la reprendre devant celle de Sam & Dario.
    merged = f"{codex_existing}\n\n{rules_text}" if codex_existing.strip() else rules_text
    developer = "developer_instructions=" + json.dumps(merged, ensure_ascii=False)
    return [*overrides, "-c", developer, "--", question]


def codex_existing(cwd: str) -> Optional[str]:
    """La consigne développeur native de Codex, lue deux fois au plus. None si elle reste illisible : Codex démarre
    alors avec sa configuration intacte, sans le prénom ni les règles du binôme, et le panneau le dit."""
    for _ in range(2):
        try:
            return auto.codex_developer_instructions(cwd)
        except (OSError, RuntimeError, ValueError) as exc:
            error = exc
    print(f"Sam & Dario — consigne native de Codex illisible ({error}). Elle est gardée telle quelle : "
          "pour cette session, Codex ne reçoit ni son prénom ni les règles du binôme.")
    return None


def relay(state: Path, pane: str, force: bool = False) -> str:
    panes = json.loads((state / "panes.json").read_text())
    if pane not in panes:
        raise ValueError("Choisis le panneau Claude Code ou Codex.")
    if tmux(state, "display-message", "-p", "-t", pane, "#{pane_dead}/#{pane_in_mode}") != "0/0":
        raise ValueError("Reviens au champ de saisie de ce panneau.")
    sender = other(panes[pane])
    recalled = None
    if force:
        with transaction(state) as db:
            row = latest_reply(db, sender) if round_open(db) is None else None
        if row is not None and row[2] == UNCERTAIN:
            recalled = auto.recall(state, row[0])
    with transaction(state) as db:
        pending = rules.field_reply(db) if panes[pane] == "claude" else None
        if pending is not None:
            # Collé par l'auto chez Claude et attendu : Ctrl-b R recolle ce relais-là, même si Codex a répondu depuis
            # ou si une question « aux deux » attend derrière lui.
            if not force:
                return "Déjà collée. Ctrl-b R pour la recoller."
            pasted, = db.execute("SELECT pasted FROM replies WHERE id = ?", (pending,)).fetchone()
            paste_to_pane(state, pane, pasted, run=tmux)
            kind = "relay" if rules.field_kind(db) == "relay" else "manual"
            rules.occupy_field(db, f"{kind}:{pending}")
            return f"Réponse de {NAMES[sender]} recollée. Entrée pour envoyer."
        current = round_open(db)
        if current is not None and force and panes[pane] == "claude" and hold(db, current) in ("ready", "draft"):
            # La question « aux deux » attend un champ libre, ou y a été effacée : Ctrl-b R la colle.
            text = common_text(db, current)
            paste_to_pane(state, pane, paste_text(text), run=tmux)
            hold(db, current, "draft")
            rules.occupy_field(db, f"common:{current}")
            return "Question « pour les deux » collée. Entrée pour l'envoyer."
        if current is not None:
            return "Tour de table en cours. Attends les deux réponses."
        row = latest_reply(db, sender)
        if row is None or row[2] in (SENT, REPLACED):  # tâche 17 : un relais remplacé ne se recolle pas
            return f"Aucune nouvelle réponse de {NAMES[sender]}."
        reply_id, body, status = row
        if status == QUEUED:
            return "Cette réponse est déjà en file."
        if status == DRAFTED and not force:
            return "Déjà collée. Ctrl-b R pour la recoller."
        if status == UNCERTAIN and not force:
            return "Remise incertaine. Ctrl-b R pour la recoller."
        text = draft(db, Relay(reply_id, sender, panes[pane], body))
        paste_to_pane(state, pane, text, run=tmux)
        if panes[pane] == "claude":
            rules.occupy_field(db, f"manual:{reply_id}")  # un collage de l'auto attendra la soumission
    if recalled == auto.UNSURE:
        return (f"Réponse de {NAMES[sender]} collée, mais le sort de la copie automatique n'est pas confirmé : "
                "Codex l'a peut-être reçue, ou elle est peut-être encore dans sa file. Entrée pour envoyer quand même.")
    return f"Réponse de {NAMES[sender]} collée. Entrée pour envoyer."


def dashboard(state: Path) -> None:
    """La barre « tableau de bord » (tâche 11). Chaque réglage est facultatif : un tmux qui en refuse un garde la
    barre d'avant pour ce point, sans empêcher le lancement."""
    (state / bar.MARKER).touch()  # `_status` ne met en forme que les sessions qui ont installé la barre
    for args in (("set-option", "status-style", bar.STATUS_STYLE),
                 ("set-option", "status-format[0]", f"#({tmux_shell('_status', state)})"),
                 ("set-option", "status-format[1]", bar.BUTTONS),
                 ("set-option", "status", "2"),
                 ("set-option", "-w", "pane-border-lines", "heavy"),
                 ("set-option", "-w", "pane-active-border-style", "fg=#{@samdario_colour}"),
                 ("set-option", "-w", "pane-border-format", bar.border_format())):
        try:
            tmux(state, *args)
        except RuntimeError:
            continue


# Bascule entre les deux fenêtres, par leur nom : un clic sur le bouton de la barre, ou ^b v.
TOGGLE = "if-shell -F '#{==:#{window_name},scene}' 'select-window -t :terminaux' 'select-window -t :scene'"


def scene_window(state: Path) -> None:
    """La scène 16 bits (tâche 12), dans une seconde fenêtre. Créée après les deux panneaux et leurs réglages
    `-w` : ceux-ci visent la fenêtre des terminaux. Un réglage refusé n'empêche pas le lancement."""
    tmux(state, "rename-window", "-t", f"{SESSION}:", "terminaux")
    tmux(state, "set-option", "-g", "@samdario-ended", tmux_shell("_ended", state))
    for args in (("new-window", "-d", "-t", f"{SESSION}:", "-n", "scene", *helper("_scene", state)),
                 ("set-option", "-w", "-t", f"{SESSION}:scene", "remain-on-exit", "on"),
                 ("bind-key", "v", *shlex.split(TOGGLE)),
                 # Fermer la scène ne ferme rien ; la session se ferme quand les deux assistants ont disparu.
                 ("set-hook", "-t", SESSION, "window-unlinked", "run-shell -b '#{E:@samdario-ended}'")):
        try:
            tmux(state, *args)
        except RuntimeError:
            continue


def ended(state: Path) -> None:
    """Ferme la session si aucun des panneaux suivis n'existe plus. La décision suit une lecture réussie de
    tmux : un échec de lecture ne ferme rien. Les identifiants des panneaux ne dépendent pas des noms."""
    panes = json.loads((state / "panes.json").read_text())
    try:
        live = tmux(state, "list-panes", "-s", "-t", SESSION, "-F", "#{pane_id}").split()
    except RuntimeError:
        return
    if not set(panes) & set(live):
        tmux(state, "kill-session", "-t", SESSION)


def start(question: str, max_relays: int = 8, names: Optional[dict] = None, rules: str = "") -> None:
    for program in ("tmux", "claude", "codex"):
        if not shutil.which(program):
            raise ValueError(f"Programme introuvable dans le PATH : {program}")
    # Un chemin court tient dans la limite des sockets Unix sur macOS.
    with tempfile.TemporaryDirectory(prefix="samdario-", dir="/tmp") as directory:
        state = Path(directory).resolve()
        cwd = str(Path.cwd())
        initialize(state, max_relays=max_relays, cwd=cwd)
        (state / CHANNELS).write_text(json.dumps(codex_hook_settings()), encoding="utf-8")
        with transaction(state) as db:
            number = share(db, dict.fromkeys(NAMES, question))
            team.store(db, names or dict(NAMES), rules)
            put(db, autostart=1)  # l'auto démarre seul dès que les deux premières réponses sont arrivées
        (state / "question").write_text(question, encoding="utf-8")
        try:
            panes = {}
            for agent in NAMES:
                if agent == "claude":
                    create = ["new-session", "-s", SESSION, "-x", "160", "-y", "40"]
                else:
                    create = ["split-window", "-h", "-t", f"{SESSION}:"]
                pane = tmux(state, *create, "-d", "-P", "-F", "#{pane_id}", "-c", cwd.replace("#", "##"),
                            *helper("_pane", state, agent, str(number)))
                if not re.fullmatch(r"%\d+", pane):
                    raise RuntimeError("tmux n’a pas renvoyé l’identifiant du panneau.")
                panes[pane] = agent
                # tmux affiche la valeur de l'option telle quelle : un # du prénom n'est pas réinterprété.
                tmux(state, "set-option", "-p", "-t", pane, "@samdario", team.title(agent, names or dict(NAMES)))
                tmux(state, "set-option", "-p", "-t", pane, "@samdario_colour", bar.COLOURS[agent])
            (state / "panes.json").write_text(json.dumps(panes))
            tmux(state, "set-option", "-w", "pane-border-status", "top")
            tmux(state, "set-option", "-w", "pane-border-format", " #{@samdario} ")
            tmux(state, "set-option", "mouse", "on")
            tmux(state, "set-option", "status-interval", "1")
            tmux(state, "set-option", "status-format[0]",
                 f" #({tmux_shell('_status', state)})  |  Ctrl-b o  panneau  |  Ctrl-b r  coller  |  "
                 "Ctrl-b b  pour les deux  |  Ctrl-b a  auto  |  Ctrl-b d  fermer")
            dashboard(state)
            scene_window(state)
            command = tmux_shell("_relay", state) + ' "#{pane_id}"'
            tmux(state, "bind-key", "r", "run-shell", "-b", command)
            tmux(state, "bind-key", "R", "run-shell", "-b", command + " force")
            tmux(state, "bind-key", "a", "run-shell", "-b", tmux_shell("_toggle", state))
            bind_broadcast(state)
            bind_export(state)
            try:
                bind_buttons(state)  # un tmux qui refuse la liaison garde ses raccourcis clavier
            except RuntimeError:
                pass
            env = os.environ.copy()
            env.pop("TMUX", None)
            subprocess.run(tmux_command(state, "attach-session", "-t", SESSION), env=env, check=True)
        finally:
            if (state / "tmux.sock").exists():
                subprocess.run(tmux_command(state, "kill-server"), capture_output=True)


def check_installation() -> None:
    """Les hooks lancent `python -I -m samdario` : le paquet doit s'importer en mode isolé avec ce Python,
    sinon tous les relais échoueraient sans bruit (par exemple depuis un clone non installé)."""
    found = subprocess.run([sys.executable, "-I", "-c", "import samdario"], capture_output=True)
    if found.returncode != 0:
        sys.exit("Sam & Dario n'est pas installé pour ce Python : ses hooks ne pourraient pas le lancer.\n"
                 f"Installe-le avec : {INSTALL}\npuis lance simplement : samdario")


def parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="samdario", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--max", metavar="N", type=int, default=8,
                        help="nombre maximum de relais par activation du mode auto (défaut 8)")
    parser.add_argument("question", nargs="?", help="première demande, envoyée aux deux")
    options = parser.parse_args(args)
    if options.max < 1:
        parser.error("--max doit être un entier supérieur ou égal à 1")
    check_installation()  # avant toute question : sans paquet importable, aucun hook ne fonctionnerait
    options.names = team.setup(Path.cwd())  # prénoms d'abord, au premier lancement dans ce projet
    options.rules = ""
    if options.names != dict(NAMES):  # binôme du projet : règles communes, et charte que l'un ne lirait pas
        options.rules = team.charter_rules(Path.cwd())
        for warning in team.charter_warnings(Path.cwd()):
            print(warning)
    question = options.question if options.question is not None else input("Demande commune aux deux : ")
    while not any(char.isprintable() and not char.isspace() for char in question):
        if options.question is not None:
            parser.error("la demande est vide")
        question = input("La demande est vide. Écris ta question pour les deux assistants : ")
    options.question = question
    return options


def display_result(state: Path, action, *args) -> None:
    try:
        notice = action(state, *args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        notice = str(exc)
    tmux(state, "display-message", notice.replace("#", "##"))


def toggled_label(state: Path) -> None:
    """Après ^b a ou un clic sur le bouton d'auto : le libellé « ⏸ Pause » ou « ▶ Auto » change aussitôt (essai de
    l'utilisateur du 23 novembre). Sans la barre de la tâche 11, rien à faire ; un échec laisse la barre s'en charger."""
    if not bar.installed(state):
        return
    try:
        if bar.refresh_buttons(state):
            tmux(state, "refresh-client", "-S")
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        pass


def print_codex_context(text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}},
                     ensure_ascii=False), flush=True)


def main(args):
    if len(args) in (2, 3) and args[0] == "_event":
        _, sender, *payload = args
        directory = os.environ.get(STATE_VARIABLE)
        if not directory or not (Path(directory) / "replies.db").is_file():
            return
        raw = json.loads(payload[0]) if payload else json.load(sys.stdin)
        state = Path(directory)
        waiter = auto.handle_event(state, sender, raw, send_context=print_codex_context)
        # Une session lancée avant les canaux a un hook de fin de tour ordinaire : l'y faire attendre
        # bloquerait Claude. Seules les sessions lancées avec le hook en arrière-plan attendent.
        if waiter is not None and (state / CHANNELS).is_file():
            # Fin de tour de Claude : attendre en arrière-plan le relais qui lui revient.
            relayed = auto.wait(state, waiter)
            if relayed is not None:
                sys.stderr.write(relayed)
                sys.exit(2)
    elif len(args) in (3, 4) and args[0] == "_capture":
        # Les sessions lancées par l'ancien prototype utilisent encore cette commande.
        _, directory, sender, *payload = args
        event = json.loads(payload[0]) if payload else json.load(sys.stdin)
        capture(Path(directory), sender, event)
    elif len(args) in (3, 4) and args[0] == "_pane":
        _, directory, agent, *round_id = args
        state = Path(directory)
        question = (state / "question").read_text(encoding="utf-8")
        if round_id:  # Les anciens lanceurs passent déjà la demande complète dans le fichier.
            question = prompt(question, agent, int(round_id[0]))
        with transaction(state) as db:
            names, rules, cwd = team.names_of(db), team.rules_of(db), get(db, "cwd")
        existing = codex_existing(cwd) if agent == "codex" else ""
        command = agent_command(state, agent, question, names, rules, existing)
        os.execvpe(command[0], command, {**os.environ, STATE_VARIABLE: str(state.resolve())})
    elif len(args) == 2 and args[0] == "_status":
        folder = Path(args[1])
        print(bar.dashboard(folder) if bar.installed(folder) else auto.status(folder))
    elif len(args) == 2 and args[0] == "_scene":
        scene.run(Path(args[1]))
    elif len(args) == 2 and args[0] == "_ended":
        ended(Path(args[1]))
    elif len(args) == 2 and args[0] == "_toggle":
        display_result(Path(args[1]), auto.toggle)
        toggled_label(Path(args[1]))
    elif len(args) == 3 and args[0] == "_both":
        display_result(Path(args[1]), auto.broadcast, args[2])
    elif (len(args) == 3 and args[0] == "_export_accord") or (len(args) == 5 and args[0] == "_save_accord"):
        export_action(Path(args[1]), args[2], *args[3:])
    elif len(args) >= 3 and args[0] == "_relay" and args[3:] in ([], ["force"]):
        _, directory, pane, *option = args
        display_result(Path(directory), relay, pane, bool(option))
    else:
        options = parse(args)
        start(options.question, max_relays=options.max, names=options.names, rules=options.rules)


def run() -> None:
    """Point d'entrée commun de la commande `samdario` et de `python -m samdario` : lit les arguments et garde
    les codes de sortie du lanceur."""
    try:
        main(sys.argv[1:])
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        sys.exit(f"Sam & Dario — {exc}")
    except (EOFError, KeyboardInterrupt):
        sys.exit(130)
