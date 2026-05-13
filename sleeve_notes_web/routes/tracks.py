"""Tracks browser — every track in the kept set, with per-row overrides
and a per-row "sync" button that re-fetches BPM/key from the cascade.

Renders every track in the kept set as a row with editable BPM / key /
mix-flag / note. POSTed back as one form; the server diffs against the
existing overrides table and inserts / updates / deletes accordingly.

Address mode: precise (release_id + position). Broad (artist + title)
overrides created via the CLI are honoured at render time but aren't
editable from this screen — they'd collide with the per-track addressing
the table assumes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData

from sleeve_notes import db as dbmod
from sleeve_notes.fetch_bpm import (
    BPM_MAX,
    BPM_MIN,
    RateLimiter,
    cache_key,
    cascade_all,
    derive_track_result,
    load_overrides,
    parse_key_to_camelot,
    save_cache_entry,
)
from sleeve_notes_web._deps import templates


router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_precise_overrides(conn: sqlite3.Connection) -> dict[tuple[int, str], dict]:
    """One row per (release_id, position) override. Other overrides are ignored here."""
    rows = conn.execute(
        "SELECT id, release_id, position, bpm, key_camelot, note "
        "FROM overrides WHERE release_id IS NOT NULL AND position IS NOT NULL"
    ).fetchall()
    return {(r["release_id"], r["position"]): dict(r) for r in rows}


def _build_row(
    conn: sqlite3.Connection,
    track_row: sqlite3.Row,
    precise: dict[tuple[int, str], dict],
    by_rp: dict,
    by_tk: dict,
    *,
    override_display: Optional[dict] = None,
) -> dict:
    """Construct the dict shape `tracks/_row.html` expects from a tracks-join row.

    `override_display` lets callers (e.g. the sync endpoint) preserve in-progress
    edits from the submitted form instead of falling back to the saved DB row.
    """
    rel_artist = track_row["r_artist"] or "V/A"
    track_artist = track_row["artist"] or rel_artist
    tr = derive_track_result(
        conn, track_row["release_id"], track_row["position"] or "",
        track_artist, track_row["title"] or "",
        by_rp, by_tk,
    )
    if override_display is None:
        ovr_row = precise.get((track_row["release_id"], track_row["position"] or ""))
        override_display = {
            "bpm": ovr_row["bpm"] if ovr_row else None,
            "key_camelot": ovr_row["key_camelot"] if ovr_row else None,
            "note": ovr_row["note"] if ovr_row else None,
        }
    return {
        "release_id": track_row["release_id"],
        "release_artist": rel_artist,
        "release_title": track_row["r_title"] or "",
        "position": track_row["position"] or "",
        "artist": track_artist,
        "title": track_row["title"] or "",
        "bpm": tr.get("bpm"),
        "bpm_confidence": tr.get("bpm_confidence"),
        "bpm_sources": tr.get("bpm_sources"),
        "key_camelot": tr.get("key_camelot"),
        "reason": tr.get("reason"),
        "override": override_display,
    }


@router.get("/tracks")
def index(
    request: Request,
    filter: str = "all",
    release_id: Optional[int] = None,
    q: Optional[str] = None,
):
    if filter not in ("all", "needs_attention", "has_override"):
        filter = "all"
    q = (q or "").strip() or None

    with dbmod.session() as conn:
        precise = _load_precise_overrides(conn)
        by_rp, by_tk, _ = load_overrides(conn)

        sql = (
            "SELECT t.release_id, t.position, t.artist, t.title, "
            "r.artist AS r_artist, r.title AS r_title "
            "FROM tracks t JOIN releases r ON r.id = t.release_id"
        )
        params: list = []
        where: list[str] = []
        if release_id is not None:
            where.append("t.release_id = ?")
            params.append(release_id)
        if q:
            where.append(
                "(LOWER(t.artist) LIKE ? OR LOWER(t.title) LIKE ? "
                "OR LOWER(r.artist) LIKE ? OR LOWER(r.title) LIKE ?)"
            )
            like = f"%{q.lower()}%"
            params += [like, like, like, like]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.artist NULLS LAST, r.title NULLS LAST, t.position"
        track_rows = conn.execute(sql, params).fetchall()

        rows = []
        for t in track_rows:
            row = _build_row(conn, t, precise, by_rp, by_tk)
            ovr_row = precise.get((row["release_id"], row["position"]))
            if filter == "needs_attention":
                if row["bpm"] or ovr_row:
                    continue
            elif filter == "has_override":
                if not ovr_row:
                    continue
            rows.append(row)

    return templates.TemplateResponse(
        "tracks/index.html",
        {
            "request": request,
            "active": "tracks",
            "rows": rows,
            "filter": filter,
            "release_id": release_id,
            "q": q,
            "saved": request.query_params.get("saved"),
        },
    )


def _parse_form_changes(form: FormData) -> dict[tuple[int, str], dict]:
    """Group form fields by (release_id, position) → submitted values."""
    grouped: dict[tuple[int, str], dict] = {}
    for key, value in form.multi_items():
        for prefix in ("bpm_", "key_", "note_"):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            try:
                rid_s, pos = rest.split("_", 1)
                rid = int(rid_s)
            except ValueError:
                continue
            entry = grouped.setdefault((rid, pos), {})
            field = prefix.rstrip("_")
            entry[field] = (value or "").strip()
            break
    return grouped


@router.post("/tracks/save")
async def save(request: Request):
    form = await request.form()
    grouped = _parse_form_changes(form)
    changed = 0
    warnings: list[str] = []

    with dbmod.session() as conn:
        existing = _load_precise_overrides(conn)
        for (rid, pos), entry in grouped.items():
            bpm_raw = entry.get("bpm") or ""
            key_raw = entry.get("key") or ""
            note_raw = entry.get("note") or ""

            bpm_val: int | None = None
            if bpm_raw:
                try:
                    bpm_val = int(bpm_raw)
                except ValueError:
                    warnings.append(f"{rid}/{pos}: BPM {bpm_raw!r} is not an integer")
                    continue
                if not (BPM_MIN <= bpm_val <= BPM_MAX):
                    warnings.append(f"{rid}/{pos}: BPM {bpm_val} outside {BPM_MIN}-{BPM_MAX}")
                    continue

            key_val: str | None = None
            if key_raw:
                key_val = parse_key_to_camelot(key_raw)
                if key_val is None:
                    warnings.append(f"{rid}/{pos}: key {key_raw!r} not parseable")
                    continue

            note_val = note_raw or None

            empty = bpm_val is None and key_val is None and not note_val
            current = existing.get((rid, pos))

            if empty and current is None:
                continue  # nothing to do
            if empty and current is not None:
                conn.execute("DELETE FROM overrides WHERE id = ?", (current["id"],))
                changed += 1
                continue
            if current is None:
                conn.execute(
                    "INSERT INTO overrides "
                    "(release_id, position, bpm, key_camelot, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rid, pos, bpm_val, key_val, note_val, _now_iso()),
                )
                changed += 1
            else:
                same = (
                    current["bpm"] == bpm_val
                    and current["key_camelot"] == key_val
                    and (current["note"] or None) == note_val
                )
                if same:
                    continue
                conn.execute(
                    "UPDATE overrides SET bpm=?, key_camelot=?, note=? WHERE id=?",
                    (bpm_val, key_val, note_val, current["id"]),
                )
                changed += 1

    qs = f"?saved={changed}"
    return RedirectResponse(url="/tracks" + qs, status_code=303)


@router.post("/tracks/sync")
async def sync(request: Request) -> HTMLResponse:
    """Re-fetch BPM/key for one track from every source, then re-render its row.

    The current row's override inputs are submitted with the request (via
    HTMX `hx-include="closest tr"`) so any unsaved edits in this row are
    preserved across the swap. The big top-of-page Save still applies.
    """
    form = await request.form()
    try:
        rid = int((form.get("release_id") or "").strip())
    except ValueError:
        return HTMLResponse("invalid release_id", status_code=400)
    pos = (form.get("position") or "").strip()

    with dbmod.session() as conn:
        track = conn.execute(
            "SELECT t.release_id, t.position, t.artist, t.title, "
            "r.artist AS r_artist, r.title AS r_title "
            "FROM tracks t JOIN releases r ON r.id = t.release_id "
            "WHERE t.release_id = ? AND t.position = ?",
            (rid, pos),
        ).fetchone()
        if track is None:
            return HTMLResponse(f"track {rid}/{pos} not found", status_code=404)

        rel_artist = track["r_artist"] or "V/A"
        track_artist = track["artist"] or rel_artist
        title = track["title"] or ""

        # Force-refetch: clear the cache row so cascade_all re-runs every source.
        ck = cache_key(track_artist, title)
        conn.execute("DELETE FROM bpm_source_hits WHERE cache_key = ?", (ck,))
        conn.execute("DELETE FROM bpm_cache WHERE cache_key = ?", (ck,))

        rl = RateLimiter()
        entry = cascade_all(track_artist, title, prev=None, rl=rl)
        save_cache_entry(conn, ck, track_artist, title, entry)

        precise = _load_precise_overrides(conn)
        by_rp, by_tk, _ = load_overrides(conn)

        # Preserve any in-progress override edits the user submitted with the
        # sync request so they survive the row swap.
        bpm_raw = (form.get(f"bpm_{rid}_{pos}") or "").strip()
        key_raw = (form.get(f"key_{rid}_{pos}") or "").strip()
        note_raw = (form.get(f"note_{rid}_{pos}") or "").strip()
        override_display = {
            "bpm": bpm_raw or None,
            "key_camelot": key_raw or None,
            "note": note_raw or None,
        }

        row = _build_row(conn, track, precise, by_rp, by_tk, override_display=override_display)

    return templates.TemplateResponse(
        "tracks/_row.html",
        {"request": request, "r": row, "just_synced": True},
    )
