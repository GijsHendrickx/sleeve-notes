"""Render sticker PDFs from the ORM-backed collection.

ORM-port of sleeve_notes/generate_sticker_pdf.py. The pure layout +
PDF drawing primitives (sticker_layout, the PdfDrawer adapter, the
ReportLab canvas wiring) still live in the engine package — only the
DB layer and the print-run bookkeeping are new here.

Callable from:
- `manage.py render_print_run` (CLI / cron)
- the (future) web view that streams the PDF to the browser
- the (future) Django-Q2 task for genuinely huge runs
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from sleeve_notes import sticker_layout as L
from sleeve_notes.generate_sticker_pdf import (
    DEFAULT_SETTINGS,
    PdfDrawer,
    normalize_settings,
)

from print_runs.models import PrintRun
from records.models import Release
from records.services.bpm_cascade import derive_track_result, load_overrides


Logger = Callable[[str], None]


# ─── Print-run bookkeeping ───────────────────────────────────────────────────


def already_printed_ids(user) -> set[int]:
    """Discogs release ids that appear in any of the user's print runs."""
    out: set[int] = set()
    for ids in PrintRun.objects.filter(user=user).values_list("release_ids", flat=True):
        out.update(int(x) for x in (ids or []))
    return out


def last_print_timestamp(user):
    row = PrintRun.objects.filter(user=user).order_by("-created_at").first()
    return row.created_at if row else None


def create_print_run(user, release_ids: list[int], settings: dict, name: str | None = None):
    return PrintRun.objects.create(
        user=user,
        name=(name or ""),
        settings=normalize_settings(settings),
        release_ids=sorted({int(r) for r in release_ids}),
    )


def get_print_run(user, run_id):
    """Fetch a PrintRun, scoped to the user. Raises PrintRun.DoesNotExist."""
    return PrintRun.objects.get(pk=run_id, user=user)


# ─── DB → renderer-shaped release dicts ──────────────────────────────────────


def _hydrate(release: Release) -> dict:
    """Shape an ORM Release into the dict the layout module expects.

    Key invariant: ``dict["id"]`` is the Discogs release id (int), not the
    internal UUID. The renderer uses this id for the per-sticker QR code URL.
    """
    return {
        "id": release.discogs_release_id,
        "artist": release.artist,
        "title": release.title,
        "year": release.year,
        "compilation": bool(release.compilation),
        "labels": list(release.labels or []),
        "genres": list(release.genres or []),
        "styles": list(release.styles or []),
        "rpm": list(release.rpm or []),
        "tracks": [
            {
                "position": t.position,
                "side": t.side,
                "artist": t.artist,
                "title": t.title,
                "duration": t.duration,
                "duration_s": t.duration_s,
            }
            for t in release.tracks.all().order_by("position")
        ],
    }


def releases_for_render(user) -> list[dict]:
    """All releases with tracks, ordered by Discogs id (matches legacy order)."""
    qs = (
        Release.objects.filter(user=user, tracks__isnull=False)
        .distinct()
        .order_by("discogs_release_id")
        .prefetch_related("tracks")
    )
    return [_hydrate(r) for r in qs]


def releases_by_ids(user, discogs_ids: list[int]) -> list[dict]:
    """Hydrate the given Discogs release ids in input order, skipping any
    that have no tracks (those wouldn't render usefully)."""
    if not discogs_ids:
        return []
    qs = (
        Release.objects.filter(
            user=user, discogs_release_id__in=discogs_ids, tracks__isnull=False,
        )
        .distinct()
        .prefetch_related("tracks")
    )
    by_id = {r.discogs_release_id: r for r in qs}
    return [_hydrate(by_id[rid]) for rid in discogs_ids if rid in by_id]


# ─── BPM lookup ──────────────────────────────────────────────────────────────


def build_bpm_lookup(user, releases: list[dict]) -> dict[int, dict[str, dict]]:
    """Map (discogs_release_id → position → per-track BPM/key dict)."""
    by_rp, by_tk, _warnings = load_overrides(user)
    lookup: dict[int, dict[str, dict]] = {}
    for r in releases:
        per_pos: dict[str, dict] = {}
        release_artist = r.get("artist") or "V/A"
        rid = r["id"]
        for t in r["tracks"]:
            result = derive_track_result(
                user,
                rid,
                t.get("position", ""),
                t.get("artist") or release_artist,
                t.get("title") or "",
                by_rp,
                by_tk,
            )
            per_pos[t.get("position", "")] = result
        lookup[rid] = per_pos
    return lookup


# ─── Top-level render ────────────────────────────────────────────────────────


@dataclass
class RenderResult:
    output_path: Path
    sticker_count: int
    page_count: int
    extra_stickers: int
    missing_bpm_count: int
    layout: L.LayoutConfig
    pdf_hash: str


def render_pdf(
    user,
    *,
    releases: list[dict],
    settings: dict,
    output_path: Path,
    log: Logger = print,
) -> RenderResult:
    """Render the sticker PDF for the given releases + settings to ``output_path``.

    Returns a RenderResult with stats and the SHA256 hex of the file contents.
    """
    s = normalize_settings(settings)
    try:
        layout = L.derive_layout(
            s["sticker_w"], s["sticker_h"],
            tile=s["tile"], tile_cols=s["tile_cols"], tile_rows=s["tile_rows"],
        )
    except ValueError as e:
        raise ValueError(str(e))

    bpm_lookup = build_bpm_lookup(user, releases)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(output_path), pagesize=A4)
    c.setTitle("Discogs DJ Stickers")
    drawer = PdfDrawer(c)
    sticker_count = L.draw_sticker_pages(
        drawer, releases, bpm_lookup, layout,
        on_page_break=lambda _page: c.showPage(),
        qr=s["qr"],
        show_artist=s["show_artist"],
        show_title=s["show_title"],
        show_rpm=s["show_rpm"],
        show_key=s["show_key"],
        show_bpm=s["show_bpm"],
        show_duration=s["show_duration"],
        show_track_title=s["show_track_title"],
        show_sides=s["show_sides"],
    )
    c.save()

    per_page = layout.per_page
    pages = (sticker_count + per_page - 1) // per_page
    extra = sticker_count - len(releases)
    missing = sum(
        1
        for r in releases
        for t in r["tracks"]
        if bpm_lookup.get(r["id"], {}).get(t["position"], {}).get("bpm") is None
    )
    pdf_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return RenderResult(
        output_path=output_path,
        sticker_count=sticker_count,
        page_count=pages,
        extra_stickers=max(0, extra),
        missing_bpm_count=missing,
        layout=layout,
        pdf_hash=pdf_hash,
    )


__all__ = (
    "DEFAULT_SETTINGS",
    "RenderResult",
    "already_printed_ids",
    "build_bpm_lookup",
    "create_print_run",
    "get_print_run",
    "last_print_timestamp",
    "normalize_settings",
    "releases_by_ids",
    "releases_for_render",
    "render_pdf",
)
