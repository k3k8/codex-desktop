"""Locations shared by setup and both entry points."""
import os
from pathlib import Path


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()


def root():
    return codex_home() / 'codex-desktop'


def settings_file():
    return root() / 'desktop.json'
