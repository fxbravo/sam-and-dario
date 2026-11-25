# Premier échange avec Sam & Dario

[English](getting-started.md) · **Français**

Ce guide te fait essayer Sam & Dario sur un projet fictif, en quatre étapes : un premier échange jusqu’à l’accord, l’export de cet accord, une décision que le binôme te soumet, puis un relais fait à la main. Dans l’exemple, Dario (Claude Code) et Sam (Codex) conçoivent le premier écran d’une liste de courses. Pour voir d’abord comment circulent les messages entre toi et les deux assistants, regarde le schéma de [Comment ça marche](../README.fr.md#comment-ça-marche).

## Avant de commencer

Sous Windows, fais tout dans [WSL](https://learn.microsoft.com/windows/wsl/install) (le sous-système Windows pour Linux), comme sous Linux. La section [Installation](../README.fr.md#installation) donne les commandes pour Ubuntu.

Installe tmux, Claude Code, Codex CLI et pipx, connecte les deux assistants avec tes propres comptes, puis installe Sam & Dario. La section [Installation](../README.fr.md#installation) du README donne les commandes ; sur un Mac avec Homebrew :

```sh
brew install tmux pipx
pipx ensurepath
pipx install git+https://github.com/fxbravo/sam-and-dario.git
```

Vérifie ensuite les outils :

```sh
samdario --version
tmux -V
claude --version
codex --version
codex queue --help
```

La dernière commande doit afficher l’aide de la mise en file de Codex. Les essais ont été faits sur macOS, avec tmux 3.8, Claude Code 2.1.330 et Codex CLI 0.171.0.

## Lancer un projet d’essai

Exécute ces trois lignes dans un terminal :

```sh
mkdir -p "$HOME/projets/samdario-demo"
cd "$HOME/projets/samdario-demo"
samdario --max 4
```

1. **Les prénoms.** Au premier lancement, Sam & Dario demande « Prénom de Claude : », puis « Prénom de Codex : ». Saisis **Dario**, puis **Sam**. Sam & Dario crée `.samdario.json` pour les prénoms et `AGENTS.md` pour la charte de l’équipe.
2. **La question commune.** À l’invite « Demande commune aux deux : », écris par exemple : « Proposez le texte et le bouton d’une liste de courses vide, avec un critère d’acceptation commun. Répondez sans modifier de fichier. » Les deux panneaux s’ouvrent : Dario à gauche, Sam à droite.
3. **La confiance.** Dans un dossier neuf, Claude Code te demande si tu fais confiance au dossier, et Codex peut le demander aussi. Lis chaque écran avant de valider. Chez Claude, le choix « No, exit » le ferme : choisis « Yes, I trust this folder » si tu veux continuer.
4. **Les hooks de Codex.** Au premier lancement, Codex affiche « Hooks need review » : 2 hooks sont nouveaux. Choisis **Review hooks** et vérifie les deux commandes de Sam & Dario, pour `UserPromptSubmit` et `PermissionRequest`. Elles se terminent par `-I -m samdario _event codex`. Valide-les si elles te conviennent. Codex les retient : il ne les redemande que si leur commande change, par exemple après une mise à jour qui la modifie. Si tu as passé cet écran sans valider, la barre affiche « PAUSE — hooks Codex à valider dans /hooks » : tape `/hooks` dans le panneau de Codex, valide les deux hooks, puis reprends avec le bouton **▶ Auto** de la barre (ou **Ctrl-b**, puis **a**).

Pour travailler plus tard sur une vraie application, ouvre un terminal dans le dossier de cette application et lance Sam & Dario depuis ce dossier :

```sh
cd ~/Dev/MonApplication
samdario --max 4
```

Les deux assistants y partagent les fichiers.

## 1. Le premier échange

Dès que les deux ont répondu à la question commune, Sam & Dario relaie la réponse de chacun à l’autre, sans que tu touches à rien. La barre affiche `AUTO`, le nombre de relais et le dernier relais parti.

Clique sur **▶ Scène** dans la barre (ou **Ctrl-b**, puis **v**). Dario et Sam y apparaissent, chacun avec sa dernière réponse dans une bulle. Une réponse longue défile à la molette. Reviens avec **◀ Terminaux**.

L’échange s’arrête de lui-même quand ils tombent d’accord : la barre affiche « PAUSE — accord trouvé », et la scène affiche « ✦ ACCORD ✦ ». Il s’arrête aussi après quatre relais, la limite fixée par `--max 4`. Si la barre affiche « limite de relais atteinte » avant le premier accord, clique sur **▶ Auto** pour poursuivre le même échange.

## 2. Exporter l’accord

Après l’accord, le bouton **Exporter l’accord** apparaît dans la barre de la fenêtre Terminaux. Si tu es dans la scène, reviens d’abord avec **◀ Terminaux**. Clique sur **Exporter l’accord** (ou **Ctrl-b**, puis **e**).

Claude propose un titre au bout de quelques secondes. Il est souvent long, et il donne aussi son nom au fichier : raccourcis-le si tu veux, puis appuie sur **Entrée**. La barre affiche alors le chemin du fichier, dans `docs/accords/` du projet. Il contient ta demande et les deux dernières réponses, signées. Si le champ du titre reste vide, saisis le tien. **Échap** ou un titre vide annule l’export sans rien enregistrer.

Avec un tmux plus ancien que 3.6 (Ubuntu 24.04 a le 3.4), rien de cela ne s’applique : il n’y a ni champ de titre ni annulation. L’accord est enregistré dès le clic sur **Exporter l’accord**, sous le titre de Claude ou sous « Accord », et la barre affiche la version de tmux, l’origine du titre et le chemin du fichier.

## 3. Trancher une décision

Pose la question chez **Dario**, à gauche : « Faut-il demander une confirmation avant de vider la liste ? Donnez-moi les options et votre recommandation, et laissez-moi trancher. » Tu trancheras ensuite chez Sam.

Si l’un des deux te demande de choisir, Sam & Dario ne s’arrête pas tout de suite. Il fait d’abord lire à chacun l’avis de l’autre, et la barre affiche « lecture croisée avant ta décision ». Ensuite seulement vient la pause « décision requise : à toi de trancher ». Dans la scène, un `!` marque celui qui attend ta décision.

Lis leurs options et leur recommandation, puis tranche chez **Sam**, à droite : « On suit votre recommandation. » Si Dario a conclu la lecture croisée par une nouvelle décision, Sam ne l’a pas encore lue : Sam & Dario joint sa dernière décision en tête de ton message. Tu pourrais aussi répondre chez Dario ; ses messages partiraient alors chez Sam avec ta réponse, au relais suivant.

## 4. Relayer à la main

Pour parler à un seul assistant sans que sa réponse soit relayée, coupe l’auto pendant qu’il tourne. Après ta réponse chez Sam, la barre reste en pause pendant qu’il rédige. Attends la réponse de Sam : l’auto repart, et le bouton affiche **⏸ Pause**. Clique alors une fois sur **⏸ Pause** (ou **Ctrl-b**, puis **a**). La barre affiche `OFF`.

Évite de couper depuis une pause, par exemple après un accord. Il faut alors deux appuis, et le premier relance l’auto, qui peut envoyer un relais avant le second.

1. Clique dans le panneau de **Dario**, à gauche. S’il est en train de répondre au relais de Sam, attends qu’il ait fini. Écris-lui ensuite une demande, par exemple : « Propose le texte du message qui accompagne le bouton Annuler. » Attends sa réponse : elle reste chez Dario.
2. Clique dans le panneau de **Sam**, à droite, puis sur **⎘ Coller** dans la barre (ou **Ctrl-b**, puis **r**). La réponse de Dario apparaît en brouillon chez Sam, sous son en-tête `[Réponse de Claude #…]`.
3. Ajoute ton mot sous l’en-tête, par exemple « Qu’en penses-tu ? », puis appuie sur **Entrée**.

Si tu effaces le brouillon par erreur, **↻ Recoller** (ou **Ctrl-b**, puis **R**) le remet.

## Poser une question aux deux, puis fermer

Pour poser une même question aux deux, clique sur **✉ Pour les deux** (ou **Ctrl-b**, puis **b**), écris ta question et valide avec **Entrée**.

Pour terminer, clique sur **✕ Fermer**, tout à droite de la barre, puis réponds **o**. Depuis le terminal de lancement, Sam & Dario se ferme et supprime son dossier temporaire. Les fichiers de ton projet, dont les accords exportés, restent en place.

Pour aller plus loin, lis le [guide complet](../README.fr.md).
