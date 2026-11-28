# syntax=docker/dockerfile:1
# Sam & Dario, Claude Code et Codex dans un conteneur : rien d'autre à installer que Docker.
#
#   docker build -t samdario:local --build-arg SAMDARIO_UID="$(id -u)" --build-arg SAMDARIO_GID="$(id -g)" .
#   docker run --rm -it --init \
#     --mount "type=bind,src=$(pwd -P),dst=/work" \
#     --mount type=volume,src=samdario-monprojet-home,dst=/home/samdario \
#     samdario:local
#
# Le conteneur ne voit que le projet monté et son volume. Il ne restreint pas l'accès à Internet.
FROM ubuntu:26.04

ARG CLAUDE_CODE_VERSION=2.1.332
ARG CODEX_VERSION=0.172.0
ARG SAMDARIO_UID=1000
ARG SAMDARIO_GID=1000

# Ubuntu 26.04 fournit tmux 3.6, qui permet de modifier le titre d'un accord, et Node.js 22 pour les deux assistants.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates git tmux python3 python3-venv nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g --loglevel=error \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

COPY pyproject.toml README.md LICENSE /opt/samdario-src/
COPY samdario /opt/samdario-src/samdario
RUN python3 -m venv /opt/samdario \
 && /opt/samdario/bin/pip install --no-cache-dir /opt/samdario-src \
 && ln -s /opt/samdario/bin/samdario /usr/local/bin/samdario \
 && rm -rf /opt/samdario-src

# Le bac à sable de Codex (bubblewrap) ne peut pas créer d'espace de noms avec les permissions ordinaires d'un
# conteneur. Le conteneur en tient lieu : Codex ne voit que le projet et son volume, mais il peut lancer des commandes
# et joindre Internet sans demander. Configuration système, lue en plus de ~/.codex/config.toml.
COPY <<'EOF' /etc/codex/config.toml
sandbox_mode = "danger-full-access"
EOF

# L'image Ubuntu a déjà un utilisateur « ubuntu » (UID 1000), le plus souvent celui de l'utilisateur sous Linux.
RUN (! id -u ubuntu > /dev/null 2>&1 || userdel -r ubuntu) \
 && (getent group "${SAMDARIO_GID}" > /dev/null || groupadd -g "${SAMDARIO_GID}" samdario) \
 && useradd -m -u "${SAMDARIO_UID}" -g "${SAMDARIO_GID}" -s /bin/bash samdario

USER samdario
# Les assistants sont installés dans l'image : leur mise à jour automatique échouerait. On reconstruit l'image.
ENV LANG=C.UTF-8 \
    TERM=xterm-256color \
    DISABLE_AUTOUPDATER=1
WORKDIR /work
ENTRYPOINT ["samdario"]
