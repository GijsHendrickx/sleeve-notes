"""Render the sticker PDF on A4. Sticker size is configurable; grid auto-derived.

Defaults: 96 x 50.8 mm stickers, two per row on A4, fonts auto-shrink to fit.
Pass --sticker-w / --sticker-h (in mm) to change the size. Columns and rows
per page are derived from the chosen size so as many stickers as possible
fit on A4 while keeping the page-edge margin non-negative.

Per-user: every DB query is scoped on ``DISCOGS_USER_ID`` from the env
(set by the web JobRunner, or by the developer in .env for CLI use).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

try:
    from sleeve_notes import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sleeve_notes import project_root

from sleeve_notes import db as dbmod
from sleeve_notes import sticker_layout as L
from sleeve_notes.fetch_bpm import derive_track_result, load_overrides

ROOT = project_root()
TMP = ROOT / ".tmp"
DEFAULT_PDF_OUT = TMP / "stickers.pdf"


class PdfDrawer:
    """``Drawer`` adapter over a ReportLab ``canvas.Canvas``."""

    def __init__(self, c: canvas.Canvas) -> None:
        self.c = c

    def text(self, x, y, text, *, font, size, color, anchor="start"):
        self.c.setFont(font, size)
        self.c.setFillColor(HexColor(color))
        if anchor == "end":
            self.c.drawRightString(x, y, text)
        else:
            self.c.drawString(x, y, text)

    def rect(self, x, y, w, h, *, stroke=None, fill=None, stroke_width=0.5):
        if stroke is not None:
            self.c.setStrokeColor(HexColor(stroke))
            self.c.setLineWidth(stroke_width)
        if fill is not None:
            self.c.setFillColor(HexColor(fill))
        self.c.rect(x, y, w, h, stroke=1 if stroke else 0, fill=1 if fill else 0)

    def line(self, x1, y1, x2, y2, *, color, width):
        self.c.setStrokeColor(HexColor(color))
        self.c.setLineWidth(width)
        self.c.line(x1, y1, x2, y2)

    def circle(self, cx, cy, r, *, fill):
        self.c.setFillColor(HexColor(fill))
        self.c.circle(cx, cy, r, stroke=0, fill=1)

    def qr(self, x, y, size, data):
        qr = QrCodeWidget(data, barLevel="M")
        bx, by, bx2, by2 = qr.getBounds()
        w = bx2 - bx
        h = by2 - by
        drawing = Drawing(size, size, transform=[size / w, 0, 0, size / h, -bx, -by])
        drawing.add(qr)
        renderPDF.draw(drawing, self.c, x, y)


# ---------------------------------------------------------------------------
# Print runs: each run is a first-class {releases + settings} recipe
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    "sticker_w": L.DEFAULT_STICKER_W_MM,
    "sticker_h": L.DEFAULT_STICKER_H_MM,
    "tile": False,
    "tile_cols": 2,
    "tile_rows": 5,
    "qr": True,
    "show_artist": True,
    "show_title": True,
    "show_rpm": True,
    "show_key": True,
    "show_bpm": True,
    "show_duration": True,
    "show_track_title": True,
    "show_sides": True,
}


def normalize_settings(settings: dict | None) -> dict:
    """Merge over DEFAULT_SETTINGS and coerce types — lets callers pass partial
    or string-typed dicts (e.g. from a form post) and still get a clean dict."""
    s = dict(DEFAULT_SETTINGS)
    if not settings:
        return s
    for k, default in DEFAULT_SETTINGS.items():
        if k not in settings:
            continue
        v = settings[k]
        if isinstance(default, bool):
            s[k] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "on", "yes")
        elif isinstance(default, int):
            s[k] = int(v)
        elif isinstance(default, float):
            s[k] = float(v)
        else:
            s[k] = v
    return s


def already_printed_ids(conn, user_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT pr.release_id FROM print_run_releases pr "
        "JOIN print_runs p ON p.id = pr.print_run_id "
        "WHERE p.user_id = ?",
        (user_id,),
    ).fetchall()
    return {int(r["release_id"]) for r in rows}


def create_print_run(
    conn,
    user_id: int,
    release_ids: list[int],
    settings: dict,
    name: str | None = None,
) -> int:
    """Insert a new print_run row with its release links. Returns the new id."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO print_runs (user_id, timestamp, name, settings_json) VALUES (?, ?, ?, ?)",
        (user_id, timestamp, (name or None), json.dumps(normalize_settings(settings))),
    )
    pr_id = cur.lastrowid
    for rid in sorted({int(r) for r in release_ids}):
        conn.execute(
            "INSERT OR IGNORE INTO print_run_releases (print_run_id, release_id) VALUES (?, ?)",
            (pr_id, rid),
        )
    return pr_id


def load_print_run(conn, user_id: int, run_id: int) -> dict:
    """Returns ``{id, timestamp, name, settings, release_ids}``. Raises KeyError."""
    row = conn.execute(
        "SELECT id, timestamp, name, settings_json FROM print_runs "
        "WHERE id = ? AND user_id = ?",
        (run_id, user_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"print run {run_id} not found")
    rel_rows = conn.execute(
        "SELECT release_id FROM print_run_releases WHERE print_run_id = ? ORDER BY release_id",
        (run_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "name": row["name"],
        "settings": normalize_settings(_json_loads(row["settings_json"], None)),
        "release_ids": [int(r["release_id"]) for r in rel_rows],
    }


def list_print_runs(conn, user_id: int) -> list[dict]:
    """All runs for one user, newest first, with light summary for the list view."""
    rows = conn.execute(
        "SELECT p.id, p.timestamp, p.name, p.settings_json, "
        "       (SELECT COUNT(*) FROM print_run_releases pr WHERE pr.print_run_id = p.id) AS release_count "
        "FROM print_runs p WHERE p.user_id = ? ORDER BY p.id DESC",
        (user_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "name": r["name"],
            "release_count": r["release_count"],
            "settings": (
                normalize_settings(_json_loads(r["settings_json"], None))
                if r["settings_json"] else None
            ),
        })
    return out


def delete_print_run(conn, user_id: int, run_id: int) -> None:
    # Verify ownership before deleting; print_run_releases is keyed only by
    # print_run_id but the run itself carries the user_id.
    owned = conn.execute(
        "SELECT 1 FROM print_runs WHERE id = ? AND user_id = ?",
        (run_id, user_id),
    ).fetchone()
    if owned is None:
        return
    conn.execute("DELETE FROM print_run_releases WHERE print_run_id = ?", (run_id,))
    conn.execute("DELETE FROM print_runs WHERE id = ? AND user_id = ?", (run_id, user_id))


def last_print_timestamp(conn, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT timestamp FROM print_runs WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["timestamp"] if row else None


# ---------------------------------------------------------------------------
# DB → in-memory release dicts (shape the layout module expects)
# ---------------------------------------------------------------------------

def _hydrate_release_row(conn, user_id: int, r) -> dict:
    tracks = conn.execute(
        "SELECT position, side, artist, title, duration, duration_s "
        "FROM tracks WHERE user_id = ? AND release_id = ? ORDER BY position",
        (user_id, r["id"]),
    ).fetchall()
    return {
        "id": r["id"],
        "artist": r["artist"],
        "title": r["title"],
        "year": r["year"],
        "compilation": bool(r["compilation"]),
        "labels": _json_loads(r["labels"], []),
        "genres": _json_loads(r["genres"], []),
        "styles": _json_loads(r["styles"], []),
        "rpm": _json_loads(r["rpm"], []),
        "tracks": [dict(t) for t in tracks],
    }


def load_releases_for_render(conn, user_id: int) -> list[dict]:
    """Releases that should appear on the sticker sheet.

    Excludes rows without any track rows — those wouldn't render usefully.
    """
    releases = conn.execute(
        "SELECT r.id, r.artist, r.title, r.year, r.compilation, r.labels, "
        "r.genres, r.styles, r.rpm FROM releases r "
        "WHERE r.user_id = ? AND EXISTS ("
        "  SELECT 1 FROM tracks t WHERE t.user_id = r.user_id AND t.release_id = r.id"
        ") ORDER BY r.id",
        (user_id,),
    ).fetchall()
    return [_hydrate_release_row(conn, user_id, r) for r in releases]


def releases_by_ids(conn, user_id: int, ids: list[int]) -> list[dict]:
    """Hydrate the given release IDs (in input order), skipping any that lack
    tracks. Used by the web UI when rendering a print run's exact selection."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, artist, title, year, compilation, labels, genres, styles, rpm "
        f"FROM releases WHERE user_id = ? AND id IN ({placeholders}) "
        f"AND EXISTS ("
        f"  SELECT 1 FROM tracks t WHERE t.user_id = releases.user_id AND t.release_id = releases.id"
        f")",
        [user_id, *ids],
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [_hydrate_release_row(conn, user_id, by_id[rid]) for rid in ids if rid in by_id]


def _json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def build_bpm_lookup(
    conn, user_id: int, releases: list[dict]
) -> dict[int, dict[str, dict]]:
    """Map (release_id → position → per-track BPM dict) using cache + overrides."""
    by_rp, by_tk, _ = load_overrides(conn, user_id)
    lookup: dict[int, dict[str, dict]] = {}
    for r in releases:
        per_pos: dict[str, dict] = {}
        release_artist = r.get("artist") or "V/A"
        for t in r["tracks"]:
            result = derive_track_result(
                conn,
                user_id,
                r["id"],
                t.get("position", ""),
                t.get("artist") or release_artist,
                t.get("title") or "",
                by_rp,
                by_tk,
            )
            per_pos[t.get("position", "")] = result
        lookup[r["id"]] = per_pos
    return lookup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sticker-w", type=float, default=L.DEFAULT_STICKER_W_MM,
        help=f"Sticker width in mm (default: {L.DEFAULT_STICKER_W_MM}).",
    )
    parser.add_argument(
        "--sticker-h", type=float, default=L.DEFAULT_STICKER_H_MM,
        help=f"Sticker height in mm (default: {L.DEFAULT_STICKER_H_MM}).",
    )
    parser.add_argument(
        "--tile", action="store_true",
        help="Tile stickers edge-to-edge with no gutters or page margin so the "
        "print can be sliced with just a few straight ruler cuts.",
    )
    parser.add_argument(
        "--tile-cols", type=int, default=2,
        help="(with --tile) columns per page. Default 2.",
    )
    parser.add_argument(
        "--tile-rows", type=int, default=5,
        help="(with --tile) rows per page. Default 5.",
    )
    parser.add_argument(
        "--new-only", action="store_true",
        help="Only include releases that aren't already in the DB print history. "
        "Use with --mark-printed to commit the new additions after a successful print.",
    )
    parser.add_argument(
        "--mark-printed", action="store_true",
        help="After a successful render, save the rendered release IDs and current "
        "settings as a new print run (print_runs / print_run_releases tables).",
    )
    parser.add_argument(
        "--print-run-id", type=int, default=None,
        help="Render a previously-saved print run (uses its stored releases and "
        "settings; other layout/selection flags are ignored). Mutually exclusive "
        "with --new-only and --mark-printed.",
    )
    parser.add_argument(
        "--no-qr", dest="qr", action="store_false",
        help="Don't render the Discogs-release QR code in the top-right of each "
        "sticker. The header expands to use the full sticker width.",
    )
    parser.add_argument("--no-artist", dest="show_artist", action="store_false", help="Don't render the release artist line.")
    parser.add_argument("--no-title", dest="show_title", action="store_false", help="Don't render the release title line.")
    parser.add_argument("--no-rpm", dest="show_rpm", action="store_false", help="Don't render the RPM markers in the header.")
    parser.add_argument("--no-key", dest="show_key", action="store_false", help="Don't render the per-track Camelot key.")
    parser.add_argument("--no-bpm", dest="show_bpm", action="store_false", help="Don't render BPM values, the empty BPM box, or the BPM-certainty dot.")
    parser.add_argument("--no-duration", dest="show_duration", action="store_false", help="Don't render per-track durations.")
    parser.add_argument("--no-track-title", dest="show_track_title", action="store_false", help="Don't render per-track titles in the middle column.")
    parser.add_argument("--no-sides", dest="show_sides", action="store_false", help="Don't render A-SIDE / B-SIDE group labels; list tracks sequentially.")
    parser.set_defaults(
        qr=True, show_artist=True, show_title=True, show_rpm=True,
        show_key=True, show_bpm=True, show_duration=True, show_track_title=True,
        show_sides=True,
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_PDF_OUT,
        help=f"Output PDF path (default: {DEFAULT_PDF_OUT.relative_to(ROOT)}). "
        "Parent directory is created if needed. A bare filename writes to the "
        "current working directory.",
    )
    args = parser.parse_args(argv)
    if args.print_run_id is not None and (args.new_only or args.mark_printed):
        raise SystemExit(
            "--print-run-id is mutually exclusive with --new-only / --mark-printed."
        )
    pdf_out: Path = args.output.expanduser()
    if pdf_out.is_dir() or str(args.output).endswith(("/", "\\")):
        pdf_out = pdf_out / "stickers.pdf"
    if pdf_out.suffix.lower() != ".pdf":
        pdf_out = pdf_out.with_suffix(".pdf")

    uid_raw = os.environ.get("DISCOGS_USER_ID")
    if not uid_raw:
        print("ERROR: DISCOGS_USER_ID must be set in env.", file=sys.stderr)
        return 2
    try:
        user_id = int(uid_raw)
    except ValueError:
        print(f"ERROR: DISCOGS_USER_ID must be an integer, got {uid_raw!r}", file=sys.stderr)
        return 2

    with dbmod.session() as conn:
        if args.print_run_id is not None:
            try:
                run = load_print_run(conn, user_id, args.print_run_id)
            except KeyError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
            for k, v in run["settings"].items():
                setattr(args, k, v)
            releases = releases_by_ids(conn, user_id, run["release_ids"])
            if not releases:
                print(
                    f"ERROR: print run {args.print_run_id} has no renderable releases "
                    "(all source releases were removed or lack tracks).",
                    file=sys.stderr,
                )
                return 2
        else:
            releases = load_releases_for_render(conn, user_id)
            if not releases:
                print(
                    "ERROR: no releases with tracks found. Run "
                    "`sleeve-notes fetch` first.",
                    file=sys.stderr,
                )
                return 2

            if args.new_only:
                already = already_printed_ids(conn, user_id)
                before = len(releases)
                releases = [r for r in releases if int(r["id"]) not in already]
                skipped = before - len(releases)
                last_ts = last_print_timestamp(conn, user_id)
                last_str = f" (last print: {last_ts})" if last_ts else ""
                if not releases:
                    print(
                        f"Nothing new to print. All {before} release(s) are already in the "
                        f"print history{last_str}.",
                        file=sys.stderr,
                    )
                    return 0
                print(
                    f"--new-only: rendering {len(releases)} new release(s); skipping "
                    f"{skipped} already-printed{last_str}."
                )

        try:
            layout = L.derive_layout(
                args.sticker_w, args.sticker_h,
                tile=args.tile, tile_cols=args.tile_cols, tile_rows=args.tile_rows,
            )
        except ValueError as e:
            raise SystemExit(str(e))

        bpm_lookup = build_bpm_lookup(conn, user_id, releases)
        pdf_out.parent.mkdir(parents=True, exist_ok=True)

        c = canvas.Canvas(str(pdf_out), pagesize=A4)
        c.setTitle("Discogs DJ Stickers")
        drawer = PdfDrawer(c)
        sticker_count = L.draw_sticker_pages(
            drawer, releases, bpm_lookup, layout,
            on_page_break=lambda _page: c.showPage(),
            qr=args.qr,
            show_artist=args.show_artist,
            show_title=args.show_title,
            show_rpm=args.show_rpm,
            show_key=args.show_key,
            show_bpm=args.show_bpm,
            show_duration=args.show_duration,
            show_track_title=args.show_track_title,
            show_sides=args.show_sides,
        )
        c.save()

        per_page = layout.per_page
        pages = (sticker_count + per_page - 1) // per_page
        extra = sticker_count - len(releases)
        extra_note = (
            f" (incl. {extra} extra stickers from multi-disc / >8-track splits)"
            if extra else ""
        )
        missing_count = sum(
            1
            for r in releases
            for t in r["tracks"]
            if bpm_lookup.get(r["id"], {}).get(t["position"], {}).get("bpm") is None
        )
        layout_note = " edge-to-edge (tile mode)" if layout.tile_mode else ""
        print(
            f"Wrote {pdf_out}: {sticker_count} stickers from {len(releases)} releases across "
            f"{pages} sticker page(s) ({per_page}/page; "
            f"{layout.sticker_w/mm:.1f}x{layout.sticker_h/mm:.1f} mm "
            f"on a {layout.cols}x{layout.rows} grid{layout_note}){extra_note}."
        )
        if layout.tile_mode:
            cuts = (layout.cols - 1) + (layout.rows - 1)
            print(
                f"  Tile mode: slice each page with {cuts} straight ruler cuts "
                f"({layout.cols - 1} vertical + {layout.rows - 1} horizontal). Print borderless or "
                "expect ~3 mm printer clipping on the outer stickers."
            )
        if missing_count:
            print(f"  {missing_count} tracks have an empty BPM box for handwriting.")

        if args.mark_printed:
            rendered_ids = [int(r["id"]) for r in releases]
            settings = {k: getattr(args, k) for k in DEFAULT_SETTINGS}
            run_id = create_print_run(conn, user_id, rendered_ids, settings)
            total_in_history = len(already_printed_ids(conn, user_id))
            print(
                f"  --mark-printed: saved as print run #{run_id} "
                f"({len(rendered_ids)} release(s)). Total in print history: {total_in_history}."
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
