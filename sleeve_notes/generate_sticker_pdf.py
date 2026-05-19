"""Sticker PDF render primitives + legacy SQLite helpers.

The Django-native top-level entry point lives at
``webapp/print_runs/management/commands/render_print_run.py`` (ORM-bound)
and ``webapp/print_runs/services/render.py`` (the function the web app
will call too). This module keeps:

- ``PdfDrawer`` — ReportLab adapter for the ``Drawer`` protocol in
  ``sticker_layout``.
- ``DEFAULT_SETTINGS`` + ``normalize_settings`` — settings shape / coercion.
- The legacy SQLite print-run + render helpers (``list_print_runs``,
  ``build_bpm_lookup``, ``load_releases_for_render``, …). These are
  imported by the FastAPI web app at ``sleeve_notes_web/`` and remain in
  place until that app is replaced in fase 5 of the Django migration.

``main()`` exists so the legacy ``sleeve-notes render`` entry point keeps
working — it boots Django and hands argv to ``manage.py render_print_run``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

from sleeve_notes import django_setup  # noqa: F401  ensures Django is set up
from sleeve_notes import sticker_layout as L
from sleeve_notes.fetch_bpm import derive_track_result, load_overrides


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


# ─── Legacy SQLite helpers (used by the FastAPI web app) ─────────────────────
#
# Everything below is the SQLite-bound implementation. It's still imported
# by `sleeve_notes_web/` and removed once that app is replaced by Django
# (migration_plan.md fase 5). The Django-native equivalents live at
# `webapp/print_runs/services/render.py`.


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


# ─── Delegating CLI entry point ──────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    from django.core.management import execute_from_command_line

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        execute_from_command_line(["manage.py", "render_print_run", *args])
    except SystemExit as e:
        return int(e.code) if e.code else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
