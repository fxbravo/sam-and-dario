# Contributing

Thanks for your interest in Sam & Dario. Issues and pull requests are welcome, in English or French.

## Set up

Sam & Dario only uses the Python standard library (3.9 or later). The tests launch the package the way its hooks do (`python -I -m samdario`), so install it in a virtual environment first:

```sh
git clone https://github.com/fxbravo/sam-and-dario.git
cd sam-and-dario
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[images]" ruff
```

The `images` extra installs Pillow, used only by `docs/images/dessiner.py` and its tests.

## Run the checks

```sh
.venv/bin/ruff check .
.venv/bin/python -m unittest discover -s tests -t .
```

The tests that need `tmux` are skipped if it is missing. Three tests need the `claude` command and are skipped without it, including in continuous integration: run them on a machine where Claude Code is installed. Tests never call the real Claude or Codex services.

## Pull requests

- Write the test first, see it fail, then write the code.
- Keep changes focused, and keep modules under 800 lines.
- Update `README.md`, `README.fr.md` and the guides in `docs/` when behaviour changes, and add a line to `CHANGELOG.md`.
- If you change the characters, regenerate the images with `python3 docs/images/dessiner.py`.
