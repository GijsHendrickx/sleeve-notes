"""Collection browser + release detail drawer."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from sleeve_notes import db as dbmod
from sleeve_notes import sticker_layout as L
from sleeve_notes.fetch_bpm import derive_track_result, load_overrides
from sleeve_notes.generate_sticker_pdf import build_bpm_lookup
from sleeve_notes_web._deps import templates
from sleeve_notes_web.services.preview import render_release_stickers_svg


router = APIRouter()


def _basic_info(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _cover_url(basic: dict) -> str | None:
    return basic.get("thumb") or basic.get("cover_image") or None


def _release_for_render(conn, release_id: int) -> dict | None:
    """Build a release dict in the shape ``render_release_stickers_svg`` expects.

    Mirrors ``generate_sticker_pdf.load_releases_for_render`` but for a single ID.
    """
    r = conn.execute(
        "SELECT id, artist, title, year, compilation, labels, genres, styles, rpm "
        "FROM releases WHERE id = ?",
        (release_id,),
    ).fetchone()
    if r is None:
        return None
    tracks = conn.execute(
        "SELECT position, side, artist, title, duration, duration_s "
        "FROM tracks WHERE release_id = ? ORDER BY position",
        (release_id,),
    ).fetchall()
    def _jl(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return {
        "id": r["id"],
        "artist": r["artist"],
        "title": r["title"],
        "year": r["year"],
        "compilation": bool(r["compilation"]),
        "labels": _jl(r["labels"], []),
        "genres": _jl(r["genres"], []),
        "styles": _jl(r["styles"], []),
        "rpm": _jl(r["rpm"], []),
        "tracks": [dict(t) for t in tracks],
    }


def _format_short(basic: dict) -> str:
    fmts = basic.get("formats") or []
    if not fmts:
        return ""
    parts: list[str] = []
    for f in fmts:
        name = f.get("name") or ""
        descs = f.get("descriptions") or []
        parts.append(name + (f" ({', '.join(descs)})" if descs else ""))
    return " · ".join(parts) or ""


@router.get("/collection")
def index(
    request: Request,
    q: Optional[str] = None,
    bpm: Optional[str] = None,
    type: Optional[str] = None,
    format: Optional[str] = None,
):
    where = []
    params: list = []
    if q:
        where.append("(LOWER(r.artist) LIKE ? OR LOWER(r.title) LIKE ?)")
        like = f"%{q.lower()}%"
        params += [like, like]
    if type:
        where.append("r.type = ?")
        params.append(type)
    if format:
        where.append("r.format = ?")
        params.append(format)
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""

    with dbmod.session() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"]
        type_options = [
            r["type"] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM releases WHERE type IS NOT NULL "
                "GROUP BY type ORDER BY n DESC"
            ).fetchall()
        ]
        format_options = [
            r["format"] for r in conn.execute(
                "SELECT format, COUNT(*) AS n FROM releases WHERE format IS NOT NULL "
                "GROUP BY format ORDER BY n DESC"
            ).fetchall()
        ]
        rows_raw = conn.execute(
            "SELECT r.id, r.artist, r.title, r.year, "
            "r.type, r.format, r.basic_information FROM releases r" + sql_where +
            " ORDER BY r.artist NULLS LAST, r.title NULLS LAST LIMIT 500",
            params,
        ).fetchall()

        by_rp, by_tk, _ = load_overrides(conn)
        rows = []
        for r in rows_raw:
            basic = _basic_info(r["basic_information"])
            track_rows = conn.execute(
                "SELECT position, artist, title FROM tracks WHERE release_id = ?",
                (r["id"],),
            ).fetchall()
            tracks_total = len(track_rows)
            tracks_with_bpm = 0
            rel_artist = r["artist"] or "V/A"
            for t in track_rows:
                tr = derive_track_result(
                    conn, r["id"], t["position"] or "",
                    t["artist"] or rel_artist, t["title"] or "",
                    by_rp, by_tk,
                )
                if tr.get("bpm"):
                    tracks_with_bpm += 1
            if bpm == "missing" and (tracks_total == 0 or tracks_with_bpm == tracks_total):
                continue
            if bpm == "complete" and (tracks_total == 0 or tracks_with_bpm != tracks_total):
                continue
            rows.append({
                "id": r["id"],
                "artist": r["artist"],
                "title": r["title"],
                "year": r["year"],
                "type": r["type"],
                "format": r["format"],
                "cover": _cover_url(basic),
                "format_short": _format_short(basic),
                "tracks_total": tracks_total,
                "tracks_with_bpm": tracks_with_bpm,
            })

    return templates.TemplateResponse(
        "collection/index.html",
        {
            "request": request,
            "active": "collection",
            "rows": rows,
            "total": total,
            "q": q,
            "bpm_filter": bpm,
            "type_filter": type,
            "format_filter": format,
            "type_options": type_options,
            "format_options": format_options,
        },
    )


@router.get("/collection/{release_id}")
def detail(request: Request, release_id: int):
    with dbmod.session() as conn:
        r = conn.execute(
            "SELECT id, artist, title, year, compilation, type, format, "
            "basic_information FROM releases WHERE id = ?",
            (release_id,),
        ).fetchone()
        if r is None:
            raise HTTPException(status_code=404, detail="release not found")

        basic = _basic_info(r["basic_information"])

        tracks_out: list[dict] = []
        by_rp, by_tk, _ = load_overrides(conn)
        track_rows = conn.execute(
            "SELECT position, artist, title, duration, duration_s, spotify_track_id "
            "FROM tracks WHERE release_id = ? ORDER BY position",
            (release_id,),
        ).fetchall()
        rel_artist = r["artist"] or "V/A"
        for t in track_rows:
            tr = derive_track_result(
                conn, release_id, t["position"] or "",
                t["artist"] or rel_artist, t["title"] or "",
                by_rp, by_tk,
            )
            tracks_out.append({
                "position": t["position"],
                "artist": t["artist"] or rel_artist,
                "title": t["title"] or "",
                "duration": t["duration"],
                "bpm": tr.get("bpm"),
                "bpm_confidence": tr.get("bpm_confidence"),
                "bpm_sources": tr.get("bpm_sources"),
                "shared_siblings": tr.get("shared_siblings"),
                "key_camelot": tr.get("key_camelot"),
                "reason": tr.get("reason"),
                "spotify_track_id": t["spotify_track_id"],
                "release_id": release_id,
            })

        release = {
            "id": r["id"],
            "artist": r["artist"],
            "title": r["title"],
            "year": r["year"],
            "compilation": bool(r["compilation"]),
            "type": r["type"],
            "format": r["format"],
            "cover": basic.get("cover_image") or basic.get("thumb"),
            "format_short": _format_short(basic),
            "tracks": tracks_out,
        }

        svgs: list[str] = []
        svg_error: str | None = None
        render_rel = _release_for_render(conn, release_id)
        if render_rel and render_rel["tracks"]:
            try:
                layout = L.derive_layout(L.DEFAULT_STICKER_W_MM, L.DEFAULT_STICKER_H_MM)
                bpm_lookup = build_bpm_lookup(conn, [render_rel])
                svgs = render_release_stickers_svg(
                    render_rel, bpm_lookup[render_rel["id"]], layout
                )
            except (ValueError, KeyError) as e:
                svg_error = str(e)

    return templates.TemplateResponse(
        "collection/release_detail.html",
        {"request": request, "release": release, "svgs": svgs, "svg_error": svg_error},
    )
