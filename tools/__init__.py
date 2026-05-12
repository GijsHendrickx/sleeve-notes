"""bpm-stickers — generate Discogs DJ stickers.

Each `tools/<module>.py` exposes a `main(argv=None)` callable so it can be
invoked either as a script (`python tools/<module>.py`) or via the
`bpm-stickers` CLI (`tools.cli:main`). State files (`.tmp/`, `.env`,
`overrides.json`, `printed.json`) are resolved against the *project root*
returned by `project_root()`; see that function for the precedence rules.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.2.0"


def project_root() -> Path:
    """Resolve where state files (.tmp/, .env, overrides.json) live.

    Precedence:
      1. ``$BPM_STICKERS_ROOT`` — explicit override for advanced setups
         (CI, multi-project users).
      2. ``cwd`` if it looks like a project dir (has ``.tmp/``, ``.env``,
         ``pyproject.toml``, or ``overrides.json``). This is what makes
         ``pipx install . && cd /path/to/project && bpm-stickers run`` work.
      3. The directory two levels above this module — the legacy behaviour
         when running ``python tools/<module>.py`` from any cwd. Preserved
         so older muscle memory still works.
    """
    env = os.environ.get("BPM_STICKERS_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    if any((cwd / m).exists() for m in ("data", ".tmp", ".env", "pyproject.toml", "overrides.json")):
        return cwd
    return Path(__file__).resolve().parent.parent
