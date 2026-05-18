"""Print runs — each saved as a {releases + settings} recipe.

Routes:
  GET  /print-runs             list view
  GET  /print-runs/new         editor (optionally prefilled by query params)
  POST /print-runs             create + redirect to detail
  GET  /print-runs/{id}        detail view
  GET  /print-runs/{id}/pdf    synchronous render → stream PDF
  POST /print-runs/{id}/delete delete and redirect to list
  GET  /print-runs/svg         live sticker SVG fragment for the editor

The PDF endpoint shells out to ``sleeve-notes render --print-run-id N`` so the
render code path is identical to the CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from sleeve_notes import db as dbmod
from sleeve_notes import sticker_layout as L
from sleeve_notes.generate_sticker_pdf import (
    DEFAULT_SETTINGS,
    already_printed_ids,
    build_bpm_lookup,
    create_print_run,
    delete_print_run,
    list_print_runs,
    load_print_run,
    normalize_settings,
    releases_by_ids,
)
from sleeve_notes_web._deps import templates
from sleeve_notes_web.services.preview import render_release_stickers_svg


router = APIRouter()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _editor_release_rows(conn) -> list[dict]:
    """Lightweight rows for the editor's release list — one query."""
    rows = conn.execute(
        "SELECT r.id, r.artist, r.title, r.year, "
        "       (SELECT COUNT(*) FROM tracks t WHERE t.release_id = r.id) AS track_count "
        "FROM releases r "
        "WHERE EXISTS (SELECT 1 FROM tracks t WHERE t.release_id = r.id) "
        "ORDER BY COALESCE(r.artist, ''), COALESCE(r.title, ''), r.id"
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "artist": r["artist"] or "V/A",
            "title": r["title"] or "—",
            "year": r["year"],
            "track_count": int(r["track_count"]),
        }
        for r in rows
    ]


def _read_settings_form(form) -> dict:
    """Pull settings keys out of a posted form / query mapping."""
    raw = {k: form.get(k) for k in DEFAULT_SETTINGS if form.get(k) is not None}
    # Checkbox unchecked → key absent → falls back to False in normalize_settings.
    for k, default in DEFAULT_SETTINGS.items():
        if isinstance(default, bool) and k not in raw:
            raw[k] = False
    return normalize_settings(raw)


def _settings_summary(settings: dict) -> str:
    """Human-readable one-liner used in the list/detail views."""
    s = settings or DEFAULT_SETTINGS
    parts = [f'{s["sticker_w"]:g}×{s["sticker_h"]:g} mm']
    if s["tile"]:
        parts.append(f'tile {s["tile_cols"]}×{s["tile_rows"]}')
    if not s["qr"]:
        parts.append("no QR")
    hidden = [name[5:] for name in (
        "show_artist", "show_title", "show_rpm", "show_key",
        "show_bpm", "show_duration", "show_track_title", "show_sides",
    ) if not s.get(name, True)]
    if hidden:
        parts.append("hide " + ", ".join(hidden))
    return " · ".join(parts)


def _render_preview_svgs(conn, focus_id: Optional[int], settings: dict):
    """Render the SVG preview for a single release at the given settings.

    Returns (svgs, error_str, release_obj). The caller decides how to display.
    """
    if focus_id is None:
        return [], "No release selected for preview.", None
    releases = releases_by_ids(conn, [int(focus_id)])
    if not releases:
        return [], "Release has no tracks to preview.", None
    release = releases[0]
    try:
        layout = L.derive_layout(
            settings["sticker_w"], settings["sticker_h"],
            tile=settings["tile"], tile_cols=settings["tile_cols"], tile_rows=settings["tile_rows"],
        )
    except ValueError as e:
        return [], str(e), release
    bpm_lookup = build_bpm_lookup(conn, [release])
    svgs = render_release_stickers_svg(
        release, bpm_lookup[release["id"]], layout,
        qr=settings["qr"],
        show_artist=settings["show_artist"],
        show_title=settings["show_title"],
        show_rpm=settings["show_rpm"],
        show_key=settings["show_key"],
        show_bpm=settings["show_bpm"],
        show_duration=settings["show_duration"],
        show_track_title=settings["show_track_title"],
        show_sides=settings["show_sides"],
    )
    return svgs, None, release


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/print-runs")
def list_view(request: Request):
    with dbmod.session() as conn:
        runs = list_print_runs(conn)
    for r in runs:
        r["settings_summary"] = _settings_summary(r["settings"]) if r["settings"] else None
    return templates.TemplateResponse(
        "print_runs/list.html",
        {"request": request, "active": "print_runs", "runs": runs},
    )


@router.get("/print-runs/new")
def new(
    request: Request,
    duplicate_from: Optional[int] = None,
    release_id: Optional[int] = None,
):
    with dbmod.session() as conn:
        releases = _editor_release_rows(conn)
        printed_ids = already_printed_ids(conn)

        source_name: Optional[str] = None
        if duplicate_from is not None:
            try:
                source = load_print_run(conn, duplicate_from)
                checked = set(source["release_ids"])
                settings = source["settings"]
                source_name = source["name"] or source["timestamp"]
            except KeyError:
                raise HTTPException(status_code=404, detail=f"Print run {duplicate_from} not found")
        elif release_id is not None:
            checked = {int(release_id)}
            settings = dict(DEFAULT_SETTINGS)
        else:
            checked = {r["id"] for r in releases if r["id"] not in printed_ids}
            settings = dict(DEFAULT_SETTINGS)

        # Sample preview release for step 1 — first checked release in display
        # order, falling back to the first release in the collection so step 1
        # always has *something* to render even before any selection.
        sample_id: Optional[int] = None
        for r in releases:
            if r["id"] in checked:
                sample_id = r["id"]
                break
        if sample_id is None and releases:
            sample_id = releases[0]["id"]
        svgs, svg_error, sample_release = _render_preview_svgs(conn, sample_id, settings)

    return templates.TemplateResponse(
        "print_runs/editor.html",
        {
            "request": request,
            "active": "print_runs",
            "source_name": source_name,
            "releases": releases,
            "printed_ids": printed_ids,
            "checked_ids": checked,
            "settings": settings,
            "sample_id": sample_id,
            "sample_release": sample_release,
            "svgs": svgs,
            "svg_error": svg_error,
        },
    )


@router.get("/print-runs/svg")
async def svg_fragment(request: Request, focus_id: Optional[int] = None):
    """HTMX fragment: render one release as inline SVG at the current settings.

    Registered before /print-runs/{run_id} so ``svg`` isn't parsed as a run id.
    """
    settings = _read_settings_form(request.query_params)
    with dbmod.session() as conn:
        svgs, error, release = _render_preview_svgs(conn, focus_id, settings)
    return templates.TemplateResponse(
        "print_runs/_svg.html",
        {"request": request, "svgs": svgs, "error": error, "release": release},
    )


@router.get("/print-runs/preview-modal")
async def preview_modal(request: Request, release_id: int):
    """HTMX fragment: full modal dialog showing a single release's sticker(s)
    rendered with the settings hx-included from the editor's step 1 form."""
    settings = _read_settings_form(request.query_params)
    with dbmod.session() as conn:
        svgs, error, release = _render_preview_svgs(conn, release_id, settings)
    return templates.TemplateResponse(
        "print_runs/_preview_modal.html",
        {
            "request": request,
            "svgs": svgs,
            "error": error,
            "release": release,
            "settings_summary": _settings_summary(settings),
        },
    )


@router.post("/print-runs")
async def create(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip() or None
    release_ids = [int(v) for v in form.getlist("release_id") if v]
    if not release_ids:
        raise HTTPException(status_code=400, detail="Select at least one release.")
    settings = _read_settings_form(form)
    with dbmod.session() as conn:
        run_id = create_print_run(conn, release_ids, settings, name=name)
    return RedirectResponse(url=f"/print-runs/{run_id}", status_code=303)


@router.get("/print-runs/{run_id}")
def detail(request: Request, run_id: int):
    with dbmod.session() as conn:
        try:
            run = load_print_run(conn, run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Print run {run_id} not found")
        releases = releases_by_ids(conn, run["release_ids"])
        # Releases removed from collection since the run was created:
        present_ids = {r["id"] for r in releases}
        missing_ids = [rid for rid in run["release_ids"] if rid not in present_ids]
    return templates.TemplateResponse(
        "print_runs/detail.html",
        {
            "request": request,
            "active": "print_runs",
            "run": run,
            "releases": releases,
            "missing_ids": missing_ids,
            "settings_summary": _settings_summary(run["settings"]),
        },
    )


@router.get("/print-runs/{run_id}/pdf")
def pdf(run_id: int):
    """Re-render the run from its saved releases + settings, stream PDF."""
    with dbmod.session() as conn:
        try:
            run = load_print_run(conn, run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Print run {run_id} not found")

    out = Path(tempfile.mkstemp(suffix=".pdf", prefix="sleeve-notes-")[1])
    argv = [
        sys.executable, "-m", "sleeve_notes.cli", "render",
        "--print-run-id", str(run_id),
        "-o", str(out),
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(argv, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(result.stderr or result.stdout or "render failed")[-2000:],
        )
    filename = f"print-run-{run_id}.pdf"
    if run["name"]:
        safe = "".join(c for c in run["name"] if c.isalnum() or c in (" ", "-", "_")).strip()
        if safe:
            filename = f"{safe}.pdf"
    return FileResponse(str(out), media_type="application/pdf", filename=filename)


@router.post("/print-runs/{run_id}/delete")
def delete(run_id: int):
    with dbmod.session() as conn:
        delete_print_run(conn, run_id)
    return RedirectResponse(url="/print-runs", status_code=303)
