"""Thin CLI wrapper that delegates to `python manage.py sync_discogs`.

The actual logic lives in webapp/records/services/discogs_sync.py and the
matching management command. This module exists so the `sleeve-notes fetch`
entry point keeps working.
"""
from __future__ import annotations

import sys

from sleeve_notes import django_setup  # noqa: F401  ensures Django is set up


def main(argv: list[str] | None = None) -> int:
    from django.core.management import execute_from_command_line

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        execute_from_command_line(["manage.py", "sync_discogs", *args])
    except SystemExit as e:
        return int(e.code) if e.code else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
