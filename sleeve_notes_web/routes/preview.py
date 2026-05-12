"""Preview screen: per-release live SVG + on-demand full PDF.

The SVG preview uses the same composition module as the PDF (Drawer
protocol), so layout tweaks visible in the browser carry through to the
printed PDF exactly. PDF generation shells out to the existing
``sleeve-notes render`` so we don't fork its behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from sleeve_notes import db as dbmod
from sleeve_notes import sticker_layout as L
from sleeve_notes.generate_sticker_pdf import (
    already_printed_ids,
    build_bpm_lookup,
    load_releases_for_render,
)
from sleeve_notes_web._deps import templates
from sleeve_notes_web.services.preview import render_release_stickers_svg


router = APIRouter()


def _load_releases(conn, new_only: bool):
    releases = load_releases_for_render(conn)
    if new_only:
        printed = already_printed_ids(conn)
        releases = [r for r in releases if int(r["id"]) not in printed]
    return releases


def _pick_release(releases, requested_id: Optional[int]):
    if not releases:
        return None
    if requested_id is not None:
        for r in releases:
            if int(r["id"]) == requested_id:
                return r
    return releases[0]


@router.get("/preview")
def index(
    request: Request,
    release_id: Optional[int] = None,
    sticker_w: float = L.DEFAULT_STICKER_W_MM,
    sticker_h: float = L.DEFAULT_STICKER_H_MM,
    tile: Optional[str] = None,
    tile_cols: int = 2,
    tile_rows: int = 5,
    new_only: Optional[str] = None,
    mark_printed: Optional[str] = None,
):
    tile_b = bool(tile)
    new_only_b = bool(new_only)
    mark_printed_b = bool(mark_printed)

    with dbmod.session() as conn:
        all_releases = _load_releases(conn, new_only=new_only_b)
        chosen = _pick_release(all_releases, release_id)
        if chosen is None:
            svgs = []
            error = "No releases match (try clearing 'new only')." if new_only_b else "No DJ releases — run `fetch` + `filter` first."
        else:
            error = None
            try:
                layout = L.derive_layout(sticker_w, sticker_h)
                bpm_lookup = build_bpm_lookup(conn, [chosen])
                svgs = render_release_stickers_svg(
                    chosen, bpm_lookup[chosen["id"]], layout
                )
            except ValueError as e:
                svgs = []
                error = str(e)

        picker = [
            {"id": r["id"], "artist": r["artist"], "title": r["title"], "year": r["year"]}
            for r in all_releases
        ]

    pdf_q = urlencode({
        "sticker_w": sticker_w,
        "sticker_h": sticker_h,
        **({"tile": "1"} if tile_b else {}),
        "tile_cols": tile_cols,
        "tile_rows": tile_rows,
        **({"new_only": "1"} if new_only_b else {}),
        **({"mark_printed": "1"} if mark_printed_b else {}),
    })

    return templates.TemplateResponse(
        "preview/index.html",
        {
            "request": request,
            "active": "preview",
            "releases": picker,
            "release_id": chosen["id"] if chosen else None,
            "release": chosen,
            "svgs": svgs,
            "error": error,
            "sticker_w": sticker_w,
            "sticker_h": sticker_h,
            "tile": tile_b,
            "tile_cols": tile_cols,
            "tile_rows": tile_rows,
            "new_only": new_only_b,
            "mark_printed": mark_printed_b,
            "pdf_query": pdf_q,
        },
    )


@router.get("/preview/svg")
def svg_fragment(
    request: Request,
    release_id: Optional[int] = None,
    sticker_w: float = L.DEFAULT_STICKER_W_MM,
    sticker_h: float = L.DEFAULT_STICKER_H_MM,
    new_only: Optional[str] = None,
    # Accept and ignore the rest so HTMX can include the whole form:
    tile: Optional[str] = None,
    tile_cols: Optional[int] = None,
    tile_rows: Optional[int] = None,
    mark_printed: Optional[str] = None,
):
    with dbmod.session() as conn:
        releases = _load_releases(conn, new_only=bool(new_only))
        chosen = _pick_release(releases, release_id)
        if chosen is None:
            return templates.TemplateResponse(
                "preview/_svg.html",
                {"request": request, "svgs": [], "error": "No matching release.", "release": None},
            )
        try:
            layout = L.derive_layout(sticker_w, sticker_h)
        except ValueError as e:
            return templates.TemplateResponse(
                "preview/_svg.html",
                {"request": request, "svgs": [], "error": str(e), "release": chosen},
            )
        bpm_lookup = build_bpm_lookup(conn, [chosen])
        svgs = render_release_stickers_svg(chosen, bpm_lookup[chosen["id"]], layout)
    return templates.TemplateResponse(
        "preview/_svg.html",
        {"request": request, "svgs": svgs, "error": None, "release": chosen},
    )


@router.get("/preview/pdf")
def generate_pdf(
    sticker_w: float = L.DEFAULT_STICKER_W_MM,
    sticker_h: float = L.DEFAULT_STICKER_H_MM,
    tile: Optional[str] = None,
    tile_cols: int = 2,
    tile_rows: int = 5,
    new_only: Optional[str] = None,
    mark_printed: Optional[str] = None,
    release_id: Optional[int] = None,  # accepted but unused at the PDF layer
):
    out = Path(tempfile.mkstemp(suffix=".pdf", prefix="sleeve-notes-")[1])
    argv = [
        sys.executable, "-m", "sleeve_notes.cli", "render",
        "--sticker-w", str(sticker_w),
        "--sticker-h", str(sticker_h),
        "--tile-cols", str(tile_cols),
        "--tile-rows", str(tile_rows),
        "-o", str(out),
    ]
    if tile:
        argv.append("--tile")
    if new_only:
        argv.append("--new-only")
    if mark_printed:
        argv.append("--mark-printed")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(argv, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(result.stderr or result.stdout or "render failed")[-2000:],
        )
    return FileResponse(
        str(out),
        media_type="application/pdf",
        filename="sleeve-notes.pdf",
    )
