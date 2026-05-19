"""The `sleeve-notes` command-line entry point.

Dispatches to the per-step modules in this package. Each subcommand's flags
are owned by its underlying module — `sleeve-notes fetch --csv x.csv` is the
same as `python webapp/manage.py sync_discogs --csv x.csv`. The `run`
subcommand chains the three steps in order with a small allow-list of the
flags most commonly customised end-to-end.

Inspection: use `sleeve-notes query` for a quick model listing, the Django
admin at `/admin/` for browsing, and `sleeve-notes overrides` for editing
manual BPM/key overrides.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

_USAGE = """\
usage: sleeve-notes <subcommand> [options]

Pipeline:
  fetch          Step 1 — fetch your Discogs collection (API or --csv export);
                 normalizes release fields and ingests tracks in one pass
  bpm            Step 2 — look up BPM + key for every track (parallel cascade)
  render         Step 3 — generate the printable A4 sticker PDF
  run            Chain steps 1→3 with common defaults

Inspection / data:
  query          List Django models + dump a model's first rows
  overrides      Add/list/remove manual BPM/key overrides

Interactive:
  web            Launch the localhost Django web UI (dashboard, overrides, print runs)

Pass -h/--help to any subcommand for its own options.
"""


def _resolve(name: str) -> Callable[[list[str] | None], int]:
    """Lazy-import a subcommand's main() function."""
    if name == "fetch":
        from sleeve_notes import fetch_discogs_collection
        return fetch_discogs_collection.main
    if name == "bpm":
        from sleeve_notes import fetch_bpm
        return fetch_bpm.main
    if name == "render":
        from sleeve_notes import generate_sticker_pdf
        return generate_sticker_pdf.main
    if name == "query":
        from sleeve_notes import query
        return query.main
    if name == "overrides":
        from sleeve_notes import overrides
        return overrides.main
    if name == "web":
        return _web_main
    raise KeyError(name)


def _web_main(argv: list[str] | None) -> int:
    """Launch the localhost web UI via Django's runserver.

    Background jobs (Discogs sync, BPM cascade) run in an in-process
    Django-Q2 cluster when DEV_INPROCESS_QCLUSTER=true in .env — so one
    process, one Ctrl-C, one log stream.
    """
    import subprocess
    import threading
    import time
    import webbrowser

    from sleeve_notes import project_root

    parser = argparse.ArgumentParser(
        prog="sleeve-notes web",
        description="Run the localhost Django web UI. Background jobs use the "
                    "in-process Django-Q2 cluster (DEV_INPROCESS_QCLUSTER=true).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser.")
    args = parser.parse_args(argv or [])

    webapp_dir = project_root() / "webapp"
    manage_py = webapp_dir / "manage.py"
    if not manage_py.exists():
        sys.stderr.write(
            f"sleeve-notes web: webapp/manage.py not found at {manage_py}. "
            "Run from the project root.\n"
        )
        return 2

    if not args.no_browser:
        def _open():
            time.sleep(1.2)
            webbrowser.open(f"http://{args.host}:{args.port}/")
        threading.Thread(target=_open, daemon=True).start()

    cmd = [sys.executable, str(manage_py), "runserver", f"{args.host}:{args.port}"]
    try:
        return subprocess.run(cmd, cwd=str(webapp_dir)).returncode
    except KeyboardInterrupt:
        return 0


def _run(argv: list[str]) -> int:
    """Chain fetch → bpm → render.

    Allow-list: --csv, --folder, --limit go to fetch; --tile, --tile-cols,
    --tile-rows, --sticker-w, --sticker-h, --new-only, --mark-printed go to
    render. For anything else, run the three subcommands individually.
    """
    parser = argparse.ArgumentParser(
        prog="sleeve-notes run",
        description="Fetch → BPM lookup → render. "
                    "For more granular control, run the subcommands individually.",
    )
    parser.add_argument("--csv", help="Forwarded to fetch: use a Discogs CSV export.")
    parser.add_argument("--folder", help="Forwarded to fetch: collection folder name (or id).")
    parser.add_argument("--limit", type=int, help="Forwarded to fetch: stop after N releases (debug).")
    parser.add_argument("--sticker-w", type=float, help="Forwarded to render.")
    parser.add_argument("--sticker-h", type=float, help="Forwarded to render.")
    parser.add_argument("--tile", action="store_true", help="Forwarded to render.")
    parser.add_argument("--tile-cols", type=int, help="Forwarded to render.")
    parser.add_argument("--tile-rows", type=int, help="Forwarded to render.")
    parser.add_argument("--new-only", action="store_true", help="Forwarded to render.")
    parser.add_argument("--mark-printed", action="store_true", help="Forwarded to render.")
    parser.add_argument("--no-qr", dest="qr", action="store_false", help="Forwarded to render.")
    parser.set_defaults(qr=True)
    parser.add_argument("-o", "--output", help="Forwarded to render: output PDF path.")
    args = parser.parse_args(argv)

    fetch_argv: list[str] = []
    if args.csv:
        fetch_argv += ["--csv", args.csv]
    if args.folder:
        fetch_argv += ["--folder", args.folder]
    if args.limit is not None:
        fetch_argv += ["--limit", str(args.limit)]

    render_argv: list[str] = []
    if args.sticker_w is not None:
        render_argv += ["--sticker-w", str(args.sticker_w)]
    if args.sticker_h is not None:
        render_argv += ["--sticker-h", str(args.sticker_h)]
    if args.tile:
        render_argv.append("--tile")
    if args.tile_cols is not None:
        render_argv += ["--tile-cols", str(args.tile_cols)]
    if args.tile_rows is not None:
        render_argv += ["--tile-rows", str(args.tile_rows)]
    if args.new_only:
        render_argv.append("--new-only")
    if args.mark_printed:
        render_argv.append("--mark-printed")
    if not args.qr:
        render_argv.append("--no-qr")
    if args.output:
        render_argv += ["--output", args.output]

    print(">>> Step 1/3: fetch", flush=True)
    rc = _resolve("fetch")(fetch_argv)
    if rc:
        return rc
    print(">>> Step 2/3: bpm", flush=True)
    rc = _resolve("bpm")([])
    if rc:
        return rc
    print(">>> Step 3/3: render", flush=True)
    return _resolve("render")(render_argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "run":
        return _run(rest)
    try:
        fn = _resolve(cmd)
    except KeyError:
        sys.stderr.write(f"sleeve-notes: unknown subcommand {cmd!r}\n\n")
        sys.stderr.write(_USAGE)
        return 2
    return fn(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
