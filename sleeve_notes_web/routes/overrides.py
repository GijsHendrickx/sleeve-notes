"""Bulk spreadsheet-style overrides editor.

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

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from sleeve_notes import db as dbmod
from sleeve_notes.fetch_bpm import (
    BPM_MAX,
    BPM_MIN,
    derive_track_result,
    load_overrides,
    parse_key_to_camelot,
)
from sleeve_notes_web._deps import templates


router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_precise_overrides(conn: sqlite3.Connection) -> dict[tuple[int, str], dict]:
    """One row per (release_id, position) override. Other overrides are ignored here."""
    rows = conn.execute(
        "SELECT id, release_id, position, bpm, key_camelot, continuous_mix, note "
        "FROM overrides WHERE release_id IS NOT NULL AND position IS NOT NULL"
    ).fetchall()
    return {(r["release_id"], r["position"]): dict(r) for r in rows}


@router.get("/overrides")
def index(
    request: Request,
    filter: str = "all",
    release_id: Optional[int] = None,
):
    if filter not in ("all", "needs_attention", "has_override", "continuous_mixes"):
        filter = "all"

    with dbmod.session() as conn:
        precise = _load_precise_overrides(conn)
        by_rp, by_tk, _ = load_overrides(conn)

        sql = (
            "SELECT t.release_id, t.position, t.artist, t.title, t.duration_s, "
            "r.artist AS r_artist, r.title AS r_title "
            "FROM tracks t JOIN releases r ON r.id = t.release_id "
            "WHERE r.is_dj_release = 1"
        )
        params: list = []
        if release_id is not None:
            sql += " AND t.release_id = ?"
            params.append(release_id)
        sql += " ORDER BY r.artist NULLS LAST, r.title NULLS LAST, t.position"
        track_rows = conn.execute(sql, params).fetchall()

        rows = []
        for t in track_rows:
            rel_artist = t["r_artist"] or "V/A"
            track_artist = t["artist"] or rel_artist
            tr = derive_track_result(
                conn, t["release_id"], t["position"] or "",
                track_artist, t["title"] or "",
                t["duration_s"], by_rp, by_tk,
            )
            ovr_row = precise.get((t["release_id"], t["position"] or ""))
            ovr = {
                "bpm": ovr_row["bpm"] if ovr_row else None,
                "key_camelot": ovr_row["key_camelot"] if ovr_row else None,
                "continuous_mix": bool(ovr_row["continuous_mix"]) if ovr_row else False,
                "note": ovr_row["note"] if ovr_row else None,
            }
            row = {
                "release_id": t["release_id"],
                "release_artist": rel_artist,
                "release_title": t["r_title"] or "",
                "position": t["position"] or "",
                "artist": track_artist,
                "title": t["title"] or "",
                "bpm": tr.get("bpm"),
                "bpm_confidence": tr.get("bpm_confidence"),
                "bpm_sources": tr.get("bpm_sources"),
                "key_camelot": tr.get("key_camelot"),
                "reason": tr.get("reason"),
                "override": ovr,
            }
            if filter == "needs_attention":
                if row["bpm"] or row["reason"] == "continuous_mix" or ovr_row:
                    continue
            elif filter == "has_override":
                if not ovr_row:
                    continue
            elif filter == "continuous_mixes":
                if not ovr.get("continuous_mix") and row["reason"] != "continuous_mix":
                    continue
            rows.append(row)

    return templates.TemplateResponse(
        "overrides/index.html",
        {
            "request": request,
            "active": "overrides",
            "rows": rows,
            "filter": filter,
            "release_id": release_id,
            "saved": request.query_params.get("saved"),
        },
    )


def _parse_form_changes(form: FormData) -> dict[tuple[int, str], dict]:
    """Group form fields by (release_id, position) → submitted values."""
    grouped: dict[tuple[int, str], dict] = {}
    for key, value in form.multi_items():
        for prefix in ("bpm_", "key_", "mix_", "note_"):
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


@router.post("/overrides/save")
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
            mix_raw = entry.get("mix") or ""
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

            mix_val = mix_raw == "1"
            note_val = note_raw or None

            empty = bpm_val is None and key_val is None and not mix_val and not note_val
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
                    "(release_id, position, bpm, key_camelot, continuous_mix, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rid, pos, bpm_val, key_val, 1 if mix_val else 0, note_val, _now_iso()),
                )
                changed += 1
            else:
                same = (
                    current["bpm"] == bpm_val
                    and current["key_camelot"] == key_val
                    and bool(current["continuous_mix"]) == mix_val
                    and (current["note"] or None) == note_val
                )
                if same:
                    continue
                conn.execute(
                    "UPDATE overrides SET bpm=?, key_camelot=?, continuous_mix=?, note=? WHERE id=?",
                    (bpm_val, key_val, 1 if mix_val else 0, note_val, current["id"]),
                )
                changed += 1

    qs = f"?saved={changed}"
    return RedirectResponse(url="/overrides" + qs, status_code=303)
