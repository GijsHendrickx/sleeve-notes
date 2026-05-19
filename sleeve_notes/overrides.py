"""Thin CLI wrapper that delegates to `python manage.py overrides`.

The actual logic lives in webapp/records/management/commands/overrides.py
so the web app and the CLI share one implementation. This module exists so
the `sleeve-notes overrides` entry point keeps working — it boots Django,
then hands argv to manage.py.
"""
from __future__ import annotations

import sys

from sleeve_notes import django_setup  # noqa: F401  ensures Django is set up


def main(argv: list[str] | None = None) -> int:
    from django.core.management import execute_from_command_line

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        execute_from_command_line(["manage.py", "overrides", *args])
    except SystemExit as e:
        return int(e.code) if e.code else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
