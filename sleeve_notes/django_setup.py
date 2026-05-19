"""Boot Django when the engine is used outside the webapp's manage.py.

The CLI (`sleeve-notes <subcommand>`) and direct module invocations
(`python sleeve_notes/<module>.py`) live outside webapp/, so they need to
configure Django themselves before they can `from records.models import …`.

Inside webapp/, manage.py and the Django process already have settings
loaded — this module is a no-op there (idempotent).

Usage: at the top of any engine module that touches the ORM:

    from sleeve_notes import django_setup  # noqa: F401 — ensures setup
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import django
from django.apps import apps as _django_apps


_REPO_ROOT = Path(__file__).resolve().parent.parent
_WEBAPP_DIR = _REPO_ROOT / "webapp"


def _ensure_setup() -> None:
    if _django_apps.ready:
        return
    webapp_str = str(_WEBAPP_DIR)
    if webapp_str not in sys.path:
        sys.path.insert(0, webapp_str)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sleevenotes_app.settings")
    django.setup()


_ensure_setup()
