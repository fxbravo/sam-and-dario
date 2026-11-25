# Getting started with Sam & Dario

**English** · [Français](premiers-pas.md)

This guide walks you through Sam & Dario on a sample project, in four steps: a first exchange up to an agreement, exporting that agreement, a decision the pair puts to you, and a relay done by hand. In the example, Dario (Claude Code) and Sam (Codex) design the first screen of a shopping list app. To see first how messages flow between you and the two assistants, look at the diagrams in [How it works](../README.md#how-it-works).

Sam & Dario's interface is in French. Each on-screen label is shown below as it appears, with its English meaning.

## Before you start

On Windows, do everything inside [WSL](https://learn.microsoft.com/windows/wsl/install) (Windows Subsystem for Linux), as on Linux. The [Installation](../README.md#installation) section gives the commands for Ubuntu.

Install tmux, Claude Code, Codex CLI and pipx, sign in to both assistants with your own accounts, then install Sam & Dario. The [Installation](../README.md#installation) section of the README gives the commands; on a Mac with Homebrew:

```sh
brew install tmux pipx
pipx ensurepath
pipx install git+https://github.com/fxbravo/sam-and-dario.git
```

Then check the tools:

```sh
samdario --version
tmux -V
claude --version
codex --version
codex queue --help
```

The last command should print the help for Codex's message queue. Tests were run on macOS with tmux 3.8, Claude Code 2.1.330 and Codex CLI 0.171.0.

## Launching a sample project

Run these three lines in a terminal:

```sh
mkdir -p "$HOME/projects/samdario-demo"
cd "$HOME/projects/samdario-demo"
samdario --max 4
```

1. **Names.** On the first launch, Sam & Dario asks « Prénom de Claude : » (Claude's first name), then « Prénom de Codex : ». Type **Dario**, then **Sam**. Sam & Dario creates `.samdario.json` for the names and `AGENTS.md` for the team charter.
2. **The shared question.** At the « Demande commune aux deux : » prompt (request shared by both), type for example: "Suggest the text and the button for an empty shopping list, with a shared acceptance criterion. Answer without editing any file." The two panes open: Dario on the left, Sam on the right.
3. **Trust.** In a new folder, Claude Code asks whether you trust the folder, and Codex may ask too. Read each screen before confirming. In Claude, "No, exit" closes it: choose "Yes, I trust this folder" to continue.
4. **Codex hooks.** On the first launch, Codex shows "Hooks need review": 2 hooks are new. Choose **Review hooks** and check Sam & Dario's two commands, for `UserPromptSubmit` and `PermissionRequest`. They end with `-I -m samdario _event codex`. Approve them if you are happy with them. Codex remembers them: it only asks again if their command changes, for example after an update that changes it. If you skipped that screen without approving, the status bar shows « PAUSE — hooks Codex à valider dans /hooks » (Codex hooks to approve): type `/hooks` in the Codex pane, approve both hooks, then resume with the **▶ Auto** button in the status bar (or **Ctrl-b**, then **a**).

To work on a real application later, open a terminal in that application's folder and launch Sam & Dario there:

```sh
cd ~/Dev/MyApplication
samdario --max 4
```

Both assistants share the files there.

## 1. The first exchange

As soon as both have answered the shared question, Sam & Dario relays each one's answer to the other, without you touching anything. The status bar shows `AUTO`, the number of relays and the last relay sent.

Click **▶ Scène** (scene) in the status bar (or **Ctrl-b**, then **v**). Dario and Sam appear there, each with its latest answer in a speech bubble. A long answer scrolls with the mouse wheel. Come back with **◀ Terminaux** (terminals).

The exchange stops by itself when they agree: the status bar shows « PAUSE — accord trouvé » (agreement found), and the scene shows « ✦ ACCORD ✦ ». It also stops after four relays, the limit set by `--max 4`. If the status bar shows « limite de relais atteinte » (relay limit reached) before the first agreement, click **▶ Auto** to continue the same exchange.

## 2. Exporting the agreement

After the agreement, the **Exporter l’accord** (export the agreement) button appears in the status bar of the Terminaux window. If you are in the scene, first come back with **◀ Terminaux**. Click **Exporter l’accord** (or **Ctrl-b**, then **e**).

Claude suggests a title after a few seconds. It is often long, and it also names the file: shorten it if you like, then press **Enter**. The status bar then shows the file path, in the project's `docs/accords/` folder. The file holds your request and the two latest answers, signed. If the title field stays empty, type your own. **Esc** or an empty title cancels the export without saving anything.

With tmux older than 3.6 (Ubuntu 24.04 has 3.4), none of this applies: there is no title field and nothing to cancel. The agreement is saved as soon as you click **Exporter l’accord**, under Claude's title or under « Accord », and the status bar shows the tmux version, where the title came from and the file path.

## 3. Making a decision

Ask the question in **Dario**'s pane, on the left: "Should we ask for confirmation before emptying the list? Give me the options and your recommendation, and let me decide." You will decide in Sam's pane afterwards.

If either of them asks you to choose, Sam & Dario does not stop right away. It first has each one read the other's view, and the status bar shows « lecture croisée avant ta décision » (cross-reading before your decision). Only then comes the « décision requise : à toi de trancher » pause (decision needed: your call). In the scene, a `!` marks the one waiting for your decision.

Read their options and recommendation, then decide in **Sam**'s pane, on the right: "Let's follow your recommendation." If Dario closed the cross-reading with a new decision, Sam has not read it yet: Sam & Dario adds his latest decision at the top of your message. You could also answer in Dario's pane; his messages would then go to Sam with your answer, at the next relay.

## 4. Relaying by hand

To talk to a single assistant without its answer being relayed, turn the auto mode off while it is running. After your answer in Sam's pane, the status bar stays paused while he writes. Wait for Sam's answer: the auto mode starts again, and the button shows **⏸ Pause**. Then click **⏸ Pause** once (or **Ctrl-b**, then **a**). The status bar shows `OFF`.

Avoid switching off from a pause, for example after an agreement. It then takes two presses, and the first one restarts the auto mode, which may send a relay before the second.

1. Click in **Dario**'s pane, on the left. If he is answering Sam's relay, wait until he has finished. Then write him a request, for example: "Suggest the text of the message that goes with the Undo button." Wait for his answer: it stays with Dario.
2. Click in **Sam**'s pane, on the right, then click **⎘ Coller** (paste) in the status bar (or **Ctrl-b**, then **r**). Dario's answer appears as a draft in Sam's pane, under its `[Réponse de Claude #…]` header (answer from Claude).
3. Add your own words under the header, for example "What do you think?", then press **Enter**.

If you delete the draft by mistake, **↻ Recoller** (paste again), or **Ctrl-b**, then **R**, puts it back.

## Asking both, then closing

To ask both the same question, click **✉ Pour les deux** (for both), or **Ctrl-b**, then **b**, type your question and confirm with **Enter**.

To finish, click **✕ Fermer** (close), on the far right of the status bar, then answer **o** (yes). From the launch terminal, Sam & Dario closes and deletes its temporary folder. Your project files, including exported agreements, stay in place.

To go further, read the [full guide](../README.md).
