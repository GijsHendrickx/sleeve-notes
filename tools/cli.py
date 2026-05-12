"""The `bpm-stickers` command-line entry point.

Dispatches to the per-step modules in this package. Each subcommand's flags
are owned by its underlying module — `bpm-stickers fetch --csv x.csv` is the
same as `python tools/fetch_discogs_collection.py --csv x.csv`. The `run`
subcommand chains the four steps in order with a small allow-list of the
flags most commonly customised end-to-end.

Inspection: every JSON file from the old layout has moved into a single
SQLite DB at `bpm_stickers.db` in the project root. Use `bpm-stickers query`
to browse any of its tables (and `bpm-stickers overrides` for the only
table you typically need to edit by hand).
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

_USAGE = """\
usage: bpm-stickers <subcommand> [options]

Pipeline:
  fetch          Step 1 — fetch your Discogs collection (API or --csv export)
  filter         Step 2 — keep only 12"/LP vinyl, populate tracks table
  bpm            Step 3 — look up BPM + key for every track (parallel cascade)
  render         Step 4 — generate the printable A4 sticker PDF
  run            Chain steps 1→4 with common defaults

Inspection / data:
  query          Browse the SQLite DB: tables, schema, arbitrary SELECT
  overrides      Add/list/remove manual BPM/key overrides
  auth-beatport  One-time Beatport OAuth bootstrap

Pass -h/--help to any subcommand for its own options.
"""


def _resolve(name: str) -> Callable[[list[str] | None], int]:
    """Lazy-import a subcommand's main() function."""
    if name == "fetch":
        from tools import fetch_discogs_collection
        return fetch_discogs_collection.main
    if name == "filter":
        from tools import filter_dj_releases
        return filter_dj_releases.main
    if name == "bpm":
        from tools import fetch_bpm
        return fetch_bpm.main
    if name == "render":
        from tools import generate_sticker_pdf
        return generate_sticker_pdf.main
    if name == "auth-beatport":
        from tools import beatport_auth
        return beatport_auth.main
    if name == "query":
        from tools import query
        return query.main
    if name == "overrides":
        from tools import overrides
        return overrides.main
    raise KeyError(name)


def _run(argv: list[str]) -> int:
    """Chain fetch → filter → bpm → render.

    Allow-list: --csv, --folder, --limit go to fetch; --tile, --tile-cols,
    --tile-rows, --sticker-w, --sticker-h, --new-only, --mark-printed go to
    render. For anything else, run the four subcommands individually.
    """
    parser = argparse.ArgumentParser(
        prog="bpm-stickers run",
        description="Fetch → filter → BPM lookup → render. "
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
    if args.output:
        render_argv += ["--output", args.output]

    print(">>> Step 1/4: fetch", flush=True)
    rc = _resolve("fetch")(fetch_argv)
    if rc:
        return rc
    print(">>> Step 2/4: filter", flush=True)
    rc = _resolve("filter")([])
    if rc:
        return rc
    print(">>> Step 3/4: bpm", flush=True)
    rc = _resolve("bpm")([])
    if rc:
        return rc
    print(">>> Step 4/4: render", flush=True)
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
        sys.stderr.write(f"bpm-stickers: unknown subcommand {cmd!r}\n\n")
        sys.stderr.write(_USAGE)
        return 2
    return fn(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
