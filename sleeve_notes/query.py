"""Thin CLI wrapper that delegates to `python manage.py query`.

The original SQLite query.py supported arbitrary --sql and --schema; both are
better served by Django's built-ins now (`manage.py dbshell` for SQL,
`/admin/` for browsing). The Django version exposes only the two things the
CLI is still nicer for: a quick model-count listing and a small row dump.
"""
from __future__ import annotations

import sys

from sleeve_notes import django_setup  # noqa: F401  ensures Django is set up


def main(argv: list[str] | None = None) -> int:
    from django.core.management import execute_from_command_line

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        execute_from_command_line(["manage.py", "query", *args])
    except SystemExit as e:
        return int(e.code) if e.code else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
