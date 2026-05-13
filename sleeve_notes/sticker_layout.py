"""Pure layout + composition for sleeve-notes stickers.

This module owns the geometry, fonts, and ordering of every visual element
on a sticker. The PDF renderer in ``generate_sticker_pdf.py`` and the SVG
preview renderer in ``web/services/preview.py`` are both thin adapters
around the ``Drawer`` protocol defined here, so a layout change only ever
needs to be made once.

All coordinates are in PDF points (1 mm = ``reportlab.lib.units.mm``).
Origins for ``Drawer.text``/``rect``/etc. are bottom-left of the page —
the SVG adapter is responsible for flipping Y.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth


PAGE_W, PAGE_H = A4

DEFAULT_STICKER_W_MM = 96.0
DEFAULT_STICKER_H_MM = 50.8
DEFAULT_GUTTER_MM = 4.0
MIN_PAGE_EDGE = 4 * mm

CROP_LEN = 2.5 * mm
PAD_X = 3.5 * mm
PAD_Y = 2.8 * mm

BLACK = "#000000"
GREY = "#888888"

QR_SIZE = 14 * mm
QR_GAP = 1.5 * mm
QR_CORNER_PAD = 1 * mm

DISCOGS_RELEASE_URL = "https://www.discogs.com/release/{id}"

HEADER_ARTIST_PT = 11.0
HEADER_TITLE_PT = 9.0
SIDE_LABEL_PT = 8.0
BPM_FONT_PT_MAX = 13.0
BPM_TO_TRACK_RATIO = 1.5
LINE_GAP = 1.35
SECTION_GAP = 3.0
HEADER_H = HEADER_ARTIST_PT + 2 + HEADER_TITLE_PT + 4

TRACK_FONT_CANDIDATES = (10.0, 9.0, 8.0, 7.5, 7.0, 6.5, 6.0)
MAX_SIDES_PER_STICKER = 2
MAX_TRACKS_PER_STICKER = 8

_RPM_FRACTION_FALLBACK = {"⅓": " 1/3", "⅔": " 2/3", "½": " 1/2", "¼": " 1/4", "¾": " 3/4"}


@dataclass(frozen=True)
class LayoutConfig:
    """Resolved page geometry for one sticker render."""
    sticker_w: float
    sticker_h: float
    cols: int
    rows: int
    gutter_x: float
    gutter_y: float
    margin_x: float
    margin_y: float
    tile_mode: bool

    @property
    def per_page(self) -> int:
        return self.cols * self.rows


def derive_layout(
    sticker_w_mm: float = DEFAULT_STICKER_W_MM,
    sticker_h_mm: float = DEFAULT_STICKER_H_MM,
    *,
    tile: bool = False,
    tile_cols: int = 2,
    tile_rows: int = 5,
) -> LayoutConfig:
    """Resolve a ``LayoutConfig`` for either gutter or tile mode.

    Tile mode fills A4 edge-to-edge with no gutters or page margin and
    derives the sticker size from ``tile_cols``/``tile_rows``. Default mode
    keeps the requested sticker size, with 4 mm gutters and an auto-derived
    grid that maximises the number of stickers per page.

    Raises ValueError on invalid inputs — callers (CLI / web routes) decide
    how to surface that to the user.
    """
    if tile:
        if tile_cols < 1 or tile_rows < 1:
            raise ValueError(
                f"--tile-cols / --tile-rows must be >= 1 (got {tile_cols}x{tile_rows})."
            )
        return LayoutConfig(
            sticker_w=PAGE_W / tile_cols,
            sticker_h=PAGE_H / tile_rows,
            cols=tile_cols,
            rows=tile_rows,
            gutter_x=0.0,
            gutter_y=0.0,
            margin_x=0.0,
            margin_y=0.0,
            tile_mode=True,
        )

    sticker_w = sticker_w_mm * mm
    sticker_h = sticker_h_mm * mm
    if sticker_w <= 0 or sticker_h <= 0:
        raise ValueError(f"Sticker size must be positive (got {sticker_w_mm}x{sticker_h_mm} mm).")
    usable_w = PAGE_W - 2 * MIN_PAGE_EDGE
    usable_h = PAGE_H - 2 * MIN_PAGE_EDGE
    if sticker_w > usable_w or sticker_h > usable_h:
        raise ValueError(
            f"Sticker {sticker_w_mm}x{sticker_h_mm} mm doesn't fit on A4 "
            f"with a {MIN_PAGE_EDGE/mm:.0f}mm page edge "
            f"(max usable {usable_w/mm:.1f}x{usable_h/mm:.1f} mm)."
        )
    gutter_x = DEFAULT_GUTTER_MM * mm
    gutter_y = DEFAULT_GUTTER_MM * mm
    cols = max(1, int((usable_w + gutter_x) // (sticker_w + gutter_x)))
    rows = max(1, int((usable_h + gutter_y) // (sticker_h + gutter_y)))
    margin_x = (PAGE_W - cols * sticker_w - (cols - 1) * gutter_x) / 2
    margin_y = (PAGE_H - rows * sticker_h - (rows - 1) * gutter_y) / 2
    return LayoutConfig(
        sticker_w=sticker_w,
        sticker_h=sticker_h,
        cols=cols,
        rows=rows,
        gutter_x=gutter_x,
        gutter_y=gutter_y,
        margin_x=margin_x,
        margin_y=margin_y,
        tile_mode=False,
    )


def sticker_origins(layout: LayoutConfig) -> list[tuple[float, float]]:
    """Bottom-left corner (in points, PDF y-up) of every sticker slot on a page."""
    origins: list[tuple[float, float]] = []
    for row in range(layout.rows):
        for col in range(layout.cols):
            x = layout.margin_x + col * (layout.sticker_w + layout.gutter_x)
            y = PAGE_H - layout.margin_y - (row + 1) * layout.sticker_h - row * layout.gutter_y
            origins.append((x, y))
    return origins


# ---------------------------------------------------------------------------
# Pure helpers — formatting + layout math
# ---------------------------------------------------------------------------

def bpm_font_for(track_font: float) -> float:
    return min(BPM_FONT_PT_MAX, BPM_TO_TRACK_RATIO * track_font)


def display_rpm(rpm_token: str) -> str:
    for k, v in _RPM_FRACTION_FALLBACK.items():
        rpm_token = rpm_token.replace(k, v)
    return rpm_token


def ellipsize(text: str, max_w: float, font_name: str, font_size: float) -> str:
    if stringWidth(text, font_name, font_size) <= max_w:
        return text
    ellipsis = "…"
    while text and stringWidth(text + ellipsis, font_name, font_size) > max_w:
        text = text[:-1]
    return (text.rstrip() + ellipsis) if text else ellipsis


def group_tracks_by_side(tracks: list[dict]) -> dict[str, list[dict]]:
    sides: dict[str, list[dict]] = {}
    for t in tracks:
        pos = (t.get("position") or "").strip()
        side = "?"
        for ch in pos:
            if ch.isalpha():
                side = ch.upper()
                break
        sides.setdefault(side, []).append(t)
    return sides


def label_for_track(t: dict, compilation: bool, release_artist: str) -> str:
    artist = t.get("artist") or ""
    title = t.get("title") or ""
    if compilation and artist and artist != release_artist:
        return f"{artist} – {title}"
    return title


def column_widths(
    track_font: float, inner_w: float
) -> tuple[float, float, float, float, float]:
    """Return (pos, middle, duration, key, bpm) column widths."""
    bpm_size = bpm_font_for(track_font)
    bpm_digits_w = stringWidth("888", "Helvetica-Bold", bpm_size)
    bpm_col_w = bpm_digits_w + bpm_size * 0.6 + 2
    key_col_w = stringWidth("12B", "Helvetica-Bold", track_font) + 4
    dur_col_w = stringWidth("88:88", "Helvetica", track_font) + 4
    pos_col_w = stringWidth("AA1", "Courier-Bold", track_font) + 4
    middle_w = inner_w - pos_col_w - dur_col_w - key_col_w - bpm_col_w - 4
    return pos_col_w, middle_w, dur_col_w, key_col_w, bpm_col_w


def split_release_into_stickers(release: dict) -> list[dict[str, list[dict]]]:
    sides_map = group_tracks_by_side(release["tracks"])
    stickers: list[dict[str, list[dict]]] = []
    current: dict[str, list[dict]] = {}
    current_count = 0
    for side in sorted(sides_map.keys()):
        tracks = sides_map[side]
        n = len(tracks)
        if current and (
            len(current) >= MAX_SIDES_PER_STICKER
            or current_count + n > MAX_TRACKS_PER_STICKER
        ):
            stickers.append(current)
            current = {}
            current_count = 0
        current[side] = tracks
        current_count += n
    if current:
        stickers.append(current)
    return stickers or [{}]


def fit_track_font(
    sides_map: dict[str, list[dict]],
    compilation: bool,
    release_artist: str,
    inner_w: float,
    inner_h: float,
    show_sides: bool = True,
) -> tuple[float, bool]:
    side_count = len(sides_map) if show_sides else 0
    total = sum(len(v) for v in sides_map.values())
    if total == 0:
        return TRACK_FONT_CANDIDATES[0], False

    labels = [
        label_for_track(t, compilation, release_artist)
        for tracks in sides_map.values()
        for t in tracks
    ]

    last_fit_vertically = TRACK_FONT_CANDIDATES[-1]
    for size in TRACK_FONT_CANDIDATES:
        line_h = size * LINE_GAP
        content_h = (
            side_count * (SIDE_LABEL_PT + 3)
            + max(side_count - 1, 0) * SECTION_GAP
            + total * line_h
        )
        if content_h > inner_h:
            continue
        last_fit_vertically = size
        pos_col_w, middle_w, _, _, _ = column_widths(size, inner_w)
        if not show_sides:
            middle_w += pos_col_w
        max_w = max((stringWidth(lbl, "Helvetica", size) for lbl in labels), default=0)
        if max_w <= middle_w:
            return size, False

    return last_fit_vertically, True


# ---------------------------------------------------------------------------
# Drawer protocol — implemented once per output format (PDF, SVG)
# ---------------------------------------------------------------------------

class Drawer(Protocol):
    """Minimal drawing primitives used by ``draw_sticker``.

    Coordinates are in points with PDF-style y-up origin (bottom-left). The
    SVG adapter is responsible for flipping Y when emitting markup.
    """
    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        font: str,
        size: float,
        color: str,
        anchor: str = "start",
    ) -> None: ...
    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        stroke: str | None = None,
        fill: str | None = None,
        stroke_width: float = 0.5,
    ) -> None: ...
    def line(
        self, x1: float, y1: float, x2: float, y2: float, *, color: str, width: float
    ) -> None: ...
    def circle(self, cx: float, cy: float, r: float, *, fill: str) -> None: ...
    def qr(self, x: float, y: float, size: float, data: str) -> None: ...


# ---------------------------------------------------------------------------
# Composition — single sticker, full page
# ---------------------------------------------------------------------------

def draw_crop_marks(drawer: Drawer, x: float, y: float, layout: LayoutConfig) -> None:
    for cx, cy in [
        (x, y),
        (x + layout.sticker_w, y),
        (x, y + layout.sticker_h),
        (x + layout.sticker_w, y + layout.sticker_h),
    ]:
        drawer.line(cx - CROP_LEN, cy, cx + CROP_LEN, cy, color=GREY, width=0.3)
        drawer.line(cx, cy - CROP_LEN, cx, cy + CROP_LEN, color=GREY, width=0.3)


def draw_sticker_border(drawer: Drawer, x: float, y: float, layout: LayoutConfig) -> None:
    drawer.rect(x, y, layout.sticker_w, layout.sticker_h, stroke=GREY, stroke_width=0.2)


def draw_sticker(
    drawer: Drawer,
    x: float,
    y: float,
    release: dict,
    sides_map: dict[str, list[dict]],
    bpm_tracks_by_pos: dict[str, dict],
    layout: LayoutConfig,
    sticker_count: int = 1,
    qr: bool = True,
    show_artist: bool = True,
    show_title: bool = True,
    show_rpm: bool = True,
    show_key: bool = True,
    show_bpm: bool = True,
    show_duration: bool = True,
    show_track_title: bool = True,
    show_sides: bool = True,
) -> None:
    draw_sticker_border(drawer, x, y, layout)

    inner_x = x + PAD_X
    inner_w = layout.sticker_w - 2 * PAD_X
    top = y + layout.sticker_h - PAD_Y

    release_id = release.get("id")
    if qr and release_id:
        qr_x = x + layout.sticker_w - QR_CORNER_PAD - QR_SIZE
        qr_y = y + layout.sticker_h - QR_CORNER_PAD - QR_SIZE
        drawer.qr(qr_x, qr_y, QR_SIZE, DISCOGS_RELEASE_URL.format(id=release_id))
        header_inner_w = qr_x - QR_GAP - inner_x
        effective_header_h = max(HEADER_H, top - qr_y + 2)
    else:
        header_inner_w = inner_w
        effective_header_h = HEADER_H

    inner_h = layout.sticker_h - 2 * PAD_Y - effective_header_h

    side_labels = sorted(sides_map.keys())
    header_artist = "V/A" if release.get("compilation") else release.get("artist") or ""
    title_text = release.get("title") or ""
    if sticker_count > 1 and side_labels:
        title_text = f"{title_text}  [{'·'.join(side_labels)}]"

    rpm_list = release.get("rpm") or []
    rpm_top = display_rpm(rpm_list[0]) if rpm_list else ""
    rpm_bot = "/".join(display_rpm(r) for r in rpm_list[1:]) if len(rpm_list) > 1 else ""
    artist_baseline = top - HEADER_ARTIST_PT
    title_baseline = artist_baseline - HEADER_TITLE_PT - 1

    artist_text_w = header_inner_w
    title_text_w = header_inner_w
    if show_rpm and rpm_top:
        rpm_top_w = stringWidth(rpm_top, "Helvetica", HEADER_TITLE_PT)
        drawer.text(
            inner_x + header_inner_w, artist_baseline, rpm_top,
            font="Helvetica", size=HEADER_TITLE_PT, color=GREY, anchor="end",
        )
        artist_text_w = header_inner_w - rpm_top_w - 4
    if show_rpm and rpm_bot:
        rpm_bot_w = stringWidth(rpm_bot, "Helvetica", HEADER_TITLE_PT)
        drawer.text(
            inner_x + header_inner_w, title_baseline, rpm_bot,
            font="Helvetica", size=HEADER_TITLE_PT, color=GREY, anchor="end",
        )
        title_text_w = header_inner_w - rpm_bot_w - 4

    artist_line = ellipsize(header_artist or "V/A", artist_text_w, "Helvetica-Bold", HEADER_ARTIST_PT)
    title_line = ellipsize(title_text, title_text_w, "Helvetica-Oblique", HEADER_TITLE_PT)

    if show_artist:
        drawer.text(
            inner_x, artist_baseline, artist_line,
            font="Helvetica-Bold", size=HEADER_ARTIST_PT, color=BLACK,
        )
    if show_title:
        drawer.text(
            inner_x, title_baseline, title_line,
            font="Helvetica-Oblique", size=HEADER_TITLE_PT, color=BLACK,
        )

    if not side_labels:
        return

    compilation = bool(release.get("compilation"))
    release_artist = release.get("artist") or ""
    track_font, must_ellipsize = fit_track_font(
        sides_map, compilation, release_artist, inner_w, inner_h, show_sides=show_sides
    )
    bpm_size = bpm_font_for(track_font)
    line_h = track_font * LINE_GAP
    pos_col_w, middle_w, dur_col_w, key_col_w, bpm_col_w = column_widths(track_font, inner_w)
    if not show_sides:
        middle_w += pos_col_w
        pos_col_w = 0

    bpm_right = inner_x + inner_w
    key_right = bpm_right - bpm_col_w
    dur_right = key_right - key_col_w

    cursor_y = top - effective_header_h

    for idx, side in enumerate(side_labels):
        tracks = sides_map[side]

        if show_sides:
            drawer.text(
                inner_x, cursor_y - SIDE_LABEL_PT, f"{side}-SIDE",
                font="Helvetica-Bold", size=SIDE_LABEL_PT, color=GREY,
            )
            cursor_y -= SIDE_LABEL_PT + 3

        for t in tracks:
            pos = t.get("position", "")
            duration = t.get("duration", "") or ""
            label = label_for_track(t, compilation, release_artist)
            if stringWidth(label, "Helvetica", track_font) > middle_w or must_ellipsize:
                label = ellipsize(label, middle_w, "Helvetica", track_font)

            bpm_info = bpm_tracks_by_pos.get(pos, {}) or {}
            bpm = bpm_info.get("bpm")
            key_cam = bpm_info.get("key_camelot")
            bpm_conf = bpm_info.get("bpm_confidence")

            baseline = cursor_y - track_font

            if show_sides:
                drawer.text(
                    inner_x, baseline, pos,
                    font="Courier-Bold", size=track_font, color=BLACK,
                )
            if show_track_title:
                drawer.text(
                    inner_x + pos_col_w, baseline, label,
                    font="Helvetica", size=track_font, color=BLACK,
                )
            if show_duration:
                drawer.text(
                    dur_right - 4, baseline, duration,
                    font="Helvetica", size=track_font, color=GREY, anchor="end",
                )

            if show_key and key_cam:
                drawer.text(
                    key_right - 2, baseline, key_cam,
                    font="Helvetica-Bold", size=track_font, color=BLACK, anchor="end",
                )

            if show_bpm:
                if bpm:
                    drawer.text(
                        bpm_right, baseline, str(bpm),
                        font="Helvetica-Bold", size=bpm_size, color=BLACK, anchor="end",
                    )
                    if bpm_conf in ("high", "manual"):
                        dot_r = bpm_size * 0.18
                        digits_w = stringWidth(str(bpm), "Helvetica-Bold", bpm_size)
                        dot_cx = bpm_right - digits_w - dot_r - 2
                        dot_cy = baseline + bpm_size * 0.35
                        drawer.circle(dot_cx, dot_cy, dot_r, fill=BLACK)
                else:
                    box_top = baseline + 0.72 * bpm_size
                    box_bottom = baseline - 0.10 * bpm_size
                    box_left = bpm_right - bpm_col_w
                    drawer.rect(
                        box_left, box_bottom, bpm_col_w, box_top - box_bottom,
                        stroke=GREY, stroke_width=0.4,
                    )

            cursor_y -= line_h

        if show_sides and idx != len(side_labels) - 1:
            cursor_y -= SECTION_GAP


def draw_sticker_pages(
    drawer: Drawer,
    releases: list[dict],
    bpm_lookup: dict[int, dict[str, dict]],
    layout: LayoutConfig,
    on_page_break,
    qr: bool = True,
    show_artist: bool = True,
    show_title: bool = True,
    show_rpm: bool = True,
    show_key: bool = True,
    show_bpm: bool = True,
    show_duration: bool = True,
    show_track_title: bool = True,
    show_sides: bool = True,
) -> int:
    """Draw all releases across as many pages as needed.

    ``on_page_break`` is invoked between pages with the page number that
    just finished (1-based). PDF adapter calls ``c.showPage()``; SVG
    adapter would typically only render one page anyway.
    """
    origins = sticker_origins(layout)
    slot = 0
    total_stickers = 0
    page = 1
    for release in releases:
        bpm_tracks = bpm_lookup.get(release["id"], {})
        stickers = split_release_into_stickers(release)
        for sides_map in stickers:
            x, y = origins[slot]
            if not layout.tile_mode:
                draw_crop_marks(drawer, x, y, layout)
            draw_sticker(
                drawer, x, y, release, sides_map, bpm_tracks, layout, len(stickers),
                qr=qr,
                show_artist=show_artist,
                show_title=show_title,
                show_rpm=show_rpm,
                show_key=show_key,
                show_bpm=show_bpm,
                show_duration=show_duration,
                show_track_title=show_track_title,
                show_sides=show_sides,
            )
            slot += 1
            total_stickers += 1
            if slot >= len(origins):
                on_page_break(page)
                page += 1
                slot = 0
    if slot != 0:
        on_page_break(page)
    return total_stickers
