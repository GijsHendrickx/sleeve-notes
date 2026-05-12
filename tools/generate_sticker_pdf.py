"""Render the sticker PDF (96x50.8mm grid, 2 per row, auto-shrink fonts)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reportlab.lib.colors import HexColor, black, grey, red
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
DJ_IN = TMP / "dj_releases.json"
BPM_IN = TMP / "bpm_results.json"
PDF_OUT = TMP / "stickers.pdf"

PAGE_W, PAGE_H = A4
STICKER_W = 96 * mm
STICKER_H = 50.8 * mm
COLS = 2
GUTTER_X = 4 * mm
GUTTER_Y = 4 * mm
MARGIN_X = (PAGE_W - COLS * STICKER_W - (COLS - 1) * GUTTER_X) / 2
ROWS = int((PAGE_H - 2 * 8 * mm + GUTTER_Y) // (STICKER_H + GUTTER_Y))
MARGIN_Y = (PAGE_H - ROWS * STICKER_H - (ROWS - 1) * GUTTER_Y) / 2
CROP_LEN = 2.5 * mm
PAD_X = 3.5 * mm
PAD_Y = 2.8 * mm
GREY = HexColor("#888888")


def sticker_origins() -> list[tuple[float, float]]:
    origins: list[tuple[float, float]] = []
    for row in range(ROWS):
        for col in range(COLS):
            x = MARGIN_X + col * (STICKER_W + GUTTER_X)
            y = PAGE_H - MARGIN_Y - (row + 1) * STICKER_H - row * GUTTER_Y
            origins.append((x, y))
    return origins


def draw_crop_marks(c: canvas.Canvas, x: float, y: float) -> None:
    c.setStrokeColor(grey)
    c.setLineWidth(0.3)
    for cx, cy in [(x, y), (x + STICKER_W, y), (x, y + STICKER_H), (x + STICKER_W, y + STICKER_H)]:
        c.line(cx - CROP_LEN, cy, cx + CROP_LEN, cy)
        c.line(cx, cy - CROP_LEN, cx, cy + CROP_LEN)


def draw_sticker_border(c: canvas.Canvas, x: float, y: float) -> None:
    c.setStrokeColor(grey)
    c.setLineWidth(0.2)
    c.rect(x, y, STICKER_W, STICKER_H, stroke=1, fill=0)


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


HEADER_ARTIST_PT = 11.0
HEADER_TITLE_PT = 9.0
SIDE_LABEL_PT = 8.0
BPM_FONT_PT = 12.0
LINE_GAP = 1.35
SECTION_GAP = 3.0
HEADER_H = HEADER_ARTIST_PT + 2 + HEADER_TITLE_PT + 4
TRACK_FONT_CANDIDATES = (10.0, 9.0, 8.0, 7.5, 7.0, 6.5, 6.0)
MAX_SIDES_PER_STICKER = 2
MAX_TRACKS_PER_STICKER = 8


def label_for_track(t: dict, compilation: bool, release_artist: str) -> str:
    artist = t.get("artist") or ""
    title = t.get("title") or ""
    if compilation and artist and artist != release_artist:
        return f"{artist} – {title}"
    return title


def column_widths(track_font: float, inner_w: float) -> tuple[float, float, float, float]:
    bpm_col_w = stringWidth("888", "Helvetica-Bold", BPM_FONT_PT) + 2
    dur_col_w = stringWidth("88:88", "Helvetica", track_font) + 4
    pos_col_w = stringWidth("AA1", "Courier-Bold", track_font) + 4
    middle_w = inner_w - pos_col_w - dur_col_w - bpm_col_w - 4
    return pos_col_w, middle_w, dur_col_w, bpm_col_w


def split_release_into_stickers(release: dict) -> list[dict[str, list[dict]]]:
    """Group sides into stickers obeying MAX_SIDES_PER_STICKER and MAX_TRACKS_PER_STICKER.

    A single side that already exceeds MAX_TRACKS_PER_STICKER stays on one sticker
    (you can't split a side mid-tracklist); the font will auto-shrink to fit.
    """
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
) -> tuple[float, bool]:
    """Find largest font that fits all content vertically AND all titles horizontally.

    Returns (font_size, must_ellipsize). must_ellipsize=True only when titles
    still overflow at the smallest font; we then ellipsize them.
    """
    side_count = len(sides_map)
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
        _, middle_w, _, _ = column_widths(size, inner_w)
        max_w = max((stringWidth(lbl, "Helvetica", size) for lbl in labels), default=0)
        if max_w <= middle_w:
            return size, False

    return last_fit_vertically, True


def draw_sticker(
    c: canvas.Canvas,
    x: float,
    y: float,
    release: dict,
    sides_map: dict[str, list[dict]],
    bpm_tracks_by_pos: dict[str, dict],
    sticker_idx: int = 0,
    sticker_count: int = 1,
) -> None:
    draw_sticker_border(c, x, y)

    inner_x = x + PAD_X
    inner_w = STICKER_W - 2 * PAD_X
    top = y + STICKER_H - PAD_Y
    inner_h = STICKER_H - 2 * PAD_Y - HEADER_H

    side_labels = sorted(sides_map.keys())
    header_artist = "V/A" if release.get("compilation") else release["artist"]
    title_text = release.get("title") or ""
    if sticker_count > 1 and side_labels:
        title_text = f"{title_text}  [{'·'.join(side_labels)}]"

    artist_line = ellipsize(header_artist or "V/A", inner_w, "Helvetica-Bold", HEADER_ARTIST_PT)
    title_line = ellipsize(title_text, inner_w, "Helvetica-Oblique", HEADER_TITLE_PT)

    c.setFillColor(black)
    c.setFont("Helvetica-Bold", HEADER_ARTIST_PT)
    c.drawString(inner_x, top - HEADER_ARTIST_PT, artist_line)
    c.setFont("Helvetica-Oblique", HEADER_TITLE_PT)
    c.drawString(inner_x, top - HEADER_ARTIST_PT - HEADER_TITLE_PT - 1, title_line)

    if not side_labels:
        return

    compilation = bool(release.get("compilation"))
    release_artist = release.get("artist", "")
    track_font, must_ellipsize = fit_track_font(sides_map, compilation, release_artist, inner_w, inner_h)
    line_h = track_font * LINE_GAP
    pos_col_w, middle_w, dur_col_w, bpm_col_w = column_widths(track_font, inner_w)

    cursor_y = top - HEADER_H

    for idx, side in enumerate(side_labels):
        tracks = sides_map[side]

        c.setFillColor(GREY)
        c.setFont("Helvetica-Bold", SIDE_LABEL_PT)
        c.drawString(inner_x, cursor_y - SIDE_LABEL_PT, f"{side}-SIDE")
        c.setFillColor(black)
        cursor_y -= SIDE_LABEL_PT + 3

        for t in tracks:
            pos = t.get("position", "")
            duration = t.get("duration", "") or ""
            label = label_for_track(t, compilation, release_artist)
            # Final horizontal bounds guard — ellipsize whenever the label exceeds the column.
            if stringWidth(label, "Helvetica", track_font) > middle_w or must_ellipsize:
                label = ellipsize(label, middle_w, "Helvetica", track_font)

            bpm_info = bpm_tracks_by_pos.get(pos, {})
            bpm = bpm_info.get("bpm")
            reason = bpm_info.get("reason")

            baseline = cursor_y - track_font

            c.setFont("Courier-Bold", track_font)
            c.setFillColor(black)
            c.drawString(inner_x, baseline, pos)

            c.setFont("Helvetica", track_font)
            c.drawString(inner_x + pos_col_w, baseline, label)

            c.setFont("Helvetica", track_font)
            c.setFillColor(GREY)
            c.drawRightString(inner_x + inner_w - bpm_col_w - 4, baseline, duration)

            c.setFont("Helvetica-Bold", BPM_FONT_PT)
            if bpm:
                c.setFillColor(black)
                c.drawRightString(inner_x + inner_w, baseline, str(bpm))
            elif reason == "continuous_mix":
                c.setFillColor(GREY)
                c.drawRightString(inner_x + inner_w, baseline, "mix")
            else:
                c.setFillColor(red)
                c.drawRightString(inner_x + inner_w, baseline, "?")
            c.setFillColor(black)

            cursor_y -= line_h

        if idx != len(side_labels) - 1:
            cursor_y -= SECTION_GAP


def draw_sticker_pages(c: canvas.Canvas, releases: list[dict], bpm_lookup: dict[int, dict]) -> int:
    origins = sticker_origins()
    slot = 0
    total_stickers = 0
    for release in releases:
        bpm_tracks = bpm_lookup.get(release["id"], {})
        stickers = split_release_into_stickers(release)
        for sticker_idx, sides_map in enumerate(stickers):
            x, y = origins[slot]
            draw_crop_marks(c, x, y)
            draw_sticker(c, x, y, release, sides_map, bpm_tracks, sticker_idx, len(stickers))
            slot += 1
            total_stickers += 1
            if slot >= len(origins):
                c.showPage()
                slot = 0
    if slot != 0:
        c.showPage()
    return total_stickers


def collect_missing(releases: list[dict], bpm_lookup: dict[int, dict]) -> list[dict]:
    missing: list[dict] = []
    for release in releases:
        per_pos = bpm_lookup.get(release["id"], {})
        for t in release["tracks"]:
            info = per_pos.get(t["position"], {})
            if info.get("bpm") is None and info.get("reason") != "continuous_mix":
                missing.append(
                    {
                        "release_artist": "V/A" if release.get("compilation") else release["artist"],
                        "release_title": release["title"],
                        "position": t["position"],
                        "artist": t.get("artist") or release["artist"],
                        "title": t["title"],
                    }
                )
    return missing


def draw_fill_in_pages(c: canvas.Canvas, missing: list[dict]) -> None:
    if not missing:
        return
    col_pad = 12 * mm
    row_h = 9 * mm
    header_h = 14 * mm
    rows_per_page = int((PAGE_H - 2 * col_pad - header_h) // row_h)

    def page_header():
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(col_pad, PAGE_H - col_pad, "BPM fill-in page")
        c.setFont("Helvetica", 9)
        c.setFillColor(GREY)
        c.drawString(col_pad, PAGE_H - col_pad - 14, "Tracks without an automatic BPM hit. Write the BPM in the right-hand column.")
        c.setFillColor(black)

    def draw_row(y: float, item: dict) -> None:
        release = f"{item['release_artist']} – {item['release_title']}"
        track = f"{item['position']}  {item['artist']} – {item['title']}"
        c.setFont("Helvetica-Bold", 8)
        c.drawString(col_pad, y + row_h - 12, ellipsize(release, PAGE_W - 2 * col_pad - 30 * mm, "Helvetica-Bold", 8))
        c.setFont("Helvetica", 8)
        c.drawString(col_pad, y + row_h - 22, ellipsize(track, PAGE_W - 2 * col_pad - 30 * mm, "Helvetica", 8))
        box_x = PAGE_W - col_pad - 22 * mm
        c.setStrokeColor(grey)
        c.setLineWidth(0.4)
        c.rect(box_x, y + 2, 18 * mm, row_h - 4, stroke=1, fill=0)
        c.setFont("Helvetica", 6)
        c.setFillColor(GREY)
        c.drawString(box_x + 1, y + row_h - 5, "BPM")
        c.setFillColor(black)
        c.setStrokeColor(HexColor("#cccccc"))
        c.line(col_pad, y, PAGE_W - col_pad, y)

    page_header()
    cursor_y = PAGE_H - col_pad - header_h - row_h
    drawn = 0
    for item in missing:
        if drawn >= rows_per_page:
            c.showPage()
            page_header()
            cursor_y = PAGE_H - col_pad - header_h - row_h
            drawn = 0
        draw_row(cursor_y, item)
        cursor_y -= row_h
        drawn += 1
    c.showPage()


def build_bpm_lookup(bpm_results: list[dict]) -> dict[int, dict[str, dict]]:
    lookup: dict[int, dict[str, dict]] = {}
    for release in bpm_results:
        per_pos: dict[str, dict] = {}
        for t in release["tracks"]:
            per_pos[t["position"]] = t
        lookup[release["id"]] = per_pos
    return lookup


def main() -> int:
    if not DJ_IN.exists() or not BPM_IN.exists():
        print("ERROR: run fetch_discogs_collection.py → filter_dj_releases.py → fetch_bpm.py first.", file=sys.stderr)
        return 2

    with DJ_IN.open("r", encoding="utf-8") as f:
        releases = json.load(f)
    with BPM_IN.open("r", encoding="utf-8") as f:
        bpm_results = json.load(f)

    bpm_lookup = build_bpm_lookup(bpm_results)
    TMP.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(PDF_OUT), pagesize=A4)
    c.setTitle("Discogs DJ Stickers")

    sticker_count = draw_sticker_pages(c, releases, bpm_lookup)

    missing = collect_missing(releases, bpm_lookup)
    draw_fill_in_pages(c, missing)

    c.save()
    per_page = COLS * ROWS
    pages = (sticker_count + per_page - 1) // per_page
    extra = sticker_count - len(releases)
    extra_note = f" (incl. {extra} extra stickers from multi-disc / >8-track splits)" if extra else ""
    print(f"Wrote {PDF_OUT}: {sticker_count} stickers from {len(releases)} releases across {pages} sticker page(s) ({per_page}/page){extra_note}.")
    if missing:
        print(f"  {len(missing)} tracks need manual BPM entry (fill-in pages appended).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
