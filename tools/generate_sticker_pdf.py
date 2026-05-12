"""Render the sticker PDF on A4. Sticker size is configurable; grid auto-derived.

Defaults: 96 x 50.8 mm stickers, two per row on A4, fonts auto-shrink to fit.
Pass --sticker-w / --sticker-h (in mm) to change the size. Columns and rows
per page are derived from the chosen size so as many stickers as possible
fit on A4 while keeping the page-edge margin non-negative.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor, black, grey
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

try:
    from tools import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root

from tools import db as dbmod
from tools.fetch_bpm import derive_track_result, load_overrides

ROOT = project_root()
TMP = ROOT / ".tmp"
DEFAULT_PDF_OUT = TMP / "stickers.pdf"

PAGE_W, PAGE_H = A4
DEFAULT_STICKER_W_MM = 96.0
DEFAULT_STICKER_H_MM = 50.8
DEFAULT_GUTTER_MM = 4.0
MIN_PAGE_EDGE = 4 * mm
CROP_LEN = 2.5 * mm
PAD_X = 3.5 * mm
PAD_Y = 2.8 * mm
GREY = HexColor("#888888")

QR_SIZE = 14 * mm
QR_GAP = 1.5 * mm
QR_CORNER_PAD = 1 * mm
DISCOGS_RELEASE_URL = "https://www.discogs.com/release/{id}"

_RPM_FRACTION_FALLBACK = {"⅓": " 1/3", "⅔": " 2/3", "½": " 1/2", "¼": " 1/4", "¾": " 3/4"}

STICKER_W = DEFAULT_STICKER_W_MM * mm
STICKER_H = DEFAULT_STICKER_H_MM * mm
GUTTER_X = DEFAULT_GUTTER_MM * mm
GUTTER_Y = DEFAULT_GUTTER_MM * mm
COLS = 2
ROWS = 3
MARGIN_X = (PAGE_W - COLS * STICKER_W - (COLS - 1) * GUTTER_X) / 2
MARGIN_Y = (PAGE_H - ROWS * STICKER_H - (ROWS - 1) * GUTTER_Y) / 2
TILE_MODE = False


def configure_layout(
    sticker_w_mm: float,
    sticker_h_mm: float,
    tile: bool = False,
    tile_cols: int = 2,
    tile_rows: int = 5,
) -> None:
    """Set module-level layout. Two modes:

    - Default (tile=False): stickers at the requested mm size, with 4 mm
      gutters between them and a 4 mm page edge. Columns and rows are
      auto-derived to maximise the grid that fits on A4.
    - Tile mode (tile=True): exactly tile_cols x tile_rows stickers fill A4
      edge-to-edge. Sticker size is derived as PAGE_W/cols x PAGE_H/rows.
    """
    global STICKER_W, STICKER_H, COLS, ROWS, MARGIN_X, MARGIN_Y
    global GUTTER_X, GUTTER_Y, TILE_MODE

    if tile:
        if tile_cols < 1 or tile_rows < 1:
            raise SystemExit(
                f"--tile-cols / --tile-rows must be >= 1 (got {tile_cols}x{tile_rows})."
            )
        sticker_w = PAGE_W / tile_cols
        sticker_h = PAGE_H / tile_rows
        STICKER_W, STICKER_H = sticker_w, sticker_h
        COLS, ROWS = tile_cols, tile_rows
        GUTTER_X = GUTTER_Y = 0.0
        MARGIN_X = MARGIN_Y = 0.0
        TILE_MODE = True
        return

    sticker_w = sticker_w_mm * mm
    sticker_h = sticker_h_mm * mm
    if sticker_w <= 0 or sticker_h <= 0:
        raise SystemExit(f"Sticker size must be positive (got {sticker_w_mm}x{sticker_h_mm} mm).")
    usable_w = PAGE_W - 2 * MIN_PAGE_EDGE
    usable_h = PAGE_H - 2 * MIN_PAGE_EDGE
    if sticker_w > usable_w or sticker_h > usable_h:
        raise SystemExit(
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
    STICKER_W, STICKER_H = sticker_w, sticker_h
    COLS, ROWS = cols, rows
    GUTTER_X, GUTTER_Y = gutter_x, gutter_y
    MARGIN_X, MARGIN_Y = margin_x, margin_y
    TILE_MODE = False


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


def draw_qr(c: canvas.Canvas, x: float, y: float, size: float, data: str) -> None:
    qr = QrCodeWidget(data, barLevel="M")
    bx, by, bx2, by2 = qr.getBounds()
    w = bx2 - bx
    h = by2 - by
    drawing = Drawing(size, size, transform=[size / w, 0, 0, size / h, -bx, -by])
    drawing.add(qr)
    renderPDF.draw(drawing, c, x, y)


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


def bpm_font_for(track_font: float) -> float:
    return min(BPM_FONT_PT_MAX, BPM_TO_TRACK_RATIO * track_font)


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
    """Group sides into stickers obeying MAX_SIDES_PER_STICKER and MAX_TRACKS_PER_STICKER."""
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
        _, middle_w, _, _, _ = column_widths(size, inner_w)
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

    release_id = release.get("id")
    if release_id:
        qr_x = x + STICKER_W - QR_CORNER_PAD - QR_SIZE
        qr_y = y + STICKER_H - QR_CORNER_PAD - QR_SIZE
        qr_payload = DISCOGS_RELEASE_URL.format(id=release_id)
        draw_qr(c, qr_x, qr_y, QR_SIZE, qr_payload)
        header_inner_w = qr_x - QR_GAP - inner_x
        effective_header_h = max(HEADER_H, top - qr_y + 2)
    else:
        header_inner_w = inner_w
        effective_header_h = HEADER_H

    inner_h = STICKER_H - 2 * PAD_Y - effective_header_h

    side_labels = sorted(sides_map.keys())
    header_artist = "V/A" if release.get("compilation") else release["artist"]
    title_text = release.get("title") or ""
    if sticker_count > 1 and side_labels:
        title_text = f"{title_text}  [{'·'.join(side_labels)}]"

    rpm_list = release.get("rpm") or []
    rpm_label = "/".join(display_rpm(r) for r in rpm_list) if rpm_list else ""
    artist_baseline = top - HEADER_ARTIST_PT

    artist_text_w = header_inner_w
    if rpm_label:
        rpm_w = stringWidth(rpm_label, "Helvetica", HEADER_TITLE_PT)
        c.setFont("Helvetica", HEADER_TITLE_PT)
        c.setFillColor(GREY)
        c.drawRightString(inner_x + header_inner_w, artist_baseline, rpm_label)
        artist_text_w = header_inner_w - rpm_w - 4

    artist_line = ellipsize(header_artist or "V/A", artist_text_w, "Helvetica-Bold", HEADER_ARTIST_PT)
    title_line = ellipsize(title_text, header_inner_w, "Helvetica-Oblique", HEADER_TITLE_PT)

    c.setFillColor(black)
    c.setFont("Helvetica-Bold", HEADER_ARTIST_PT)
    c.drawString(inner_x, artist_baseline, artist_line)
    c.setFont("Helvetica-Oblique", HEADER_TITLE_PT)
    c.drawString(inner_x, artist_baseline - HEADER_TITLE_PT - 1, title_line)

    if not side_labels:
        return

    compilation = bool(release.get("compilation"))
    release_artist = release.get("artist", "")
    track_font, must_ellipsize = fit_track_font(sides_map, compilation, release_artist, inner_w, inner_h)
    bpm_size = bpm_font_for(track_font)
    line_h = track_font * LINE_GAP
    pos_col_w, middle_w, dur_col_w, key_col_w, bpm_col_w = column_widths(track_font, inner_w)

    bpm_right = inner_x + inner_w
    key_right = bpm_right - bpm_col_w
    dur_right = key_right - key_col_w

    cursor_y = top - effective_header_h

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
            if stringWidth(label, "Helvetica", track_font) > middle_w or must_ellipsize:
                label = ellipsize(label, middle_w, "Helvetica", track_font)

            bpm_info = bpm_tracks_by_pos.get(pos, {})
            bpm = bpm_info.get("bpm")
            key_cam = bpm_info.get("key_camelot")
            reason = bpm_info.get("reason")
            bpm_conf = bpm_info.get("bpm_confidence")

            baseline = cursor_y - track_font

            c.setFont("Courier-Bold", track_font)
            c.setFillColor(black)
            c.drawString(inner_x, baseline, pos)

            c.setFont("Helvetica", track_font)
            c.drawString(inner_x + pos_col_w, baseline, label)

            c.setFont("Helvetica", track_font)
            c.setFillColor(GREY)
            c.drawRightString(dur_right - 4, baseline, duration)

            if key_cam:
                c.setFont("Helvetica-Bold", track_font)
                c.setFillColor(black)
                c.drawRightString(key_right - 2, baseline, key_cam)

            c.setFont("Helvetica-Bold", bpm_size)
            if bpm:
                c.setFillColor(black)
                c.drawRightString(bpm_right, baseline, str(bpm))
                if bpm_conf in ("high", "manual"):
                    dot_r = bpm_size * 0.18
                    digits_w = stringWidth(str(bpm), "Helvetica-Bold", bpm_size)
                    dot_cx = bpm_right - digits_w - dot_r - 2
                    dot_cy = baseline + bpm_size * 0.35
                    c.setFillColor(black)
                    c.circle(dot_cx, dot_cy, dot_r, stroke=0, fill=1)
            elif reason == "continuous_mix":
                c.setFillColor(GREY)
                c.drawRightString(bpm_right, baseline, "mix")
            else:
                box_top = baseline + 0.72 * bpm_size
                box_bottom = baseline - 0.10 * bpm_size
                box_left = bpm_right - bpm_col_w
                c.setStrokeColor(GREY)
                c.setLineWidth(0.4)
                c.rect(box_left, box_bottom, bpm_col_w, box_top - box_bottom, stroke=1, fill=0)
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
            if not TILE_MODE:
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


# ---------------------------------------------------------------------------
# Print history (replaces .tmp/printed.json)
# ---------------------------------------------------------------------------

def already_printed_ids(conn) -> set[int]:
    rows = conn.execute("SELECT DISTINCT release_id FROM print_run_releases").fetchall()
    return {int(r["release_id"]) for r in rows}


def append_print_run(conn, release_ids: list[int]) -> int:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute("INSERT INTO print_runs (timestamp) VALUES (?)", (timestamp,))
    pr_id = cur.lastrowid
    for rid in sorted(set(release_ids)):
        conn.execute(
            "INSERT OR IGNORE INTO print_run_releases (print_run_id, release_id) VALUES (?, ?)",
            (pr_id, int(rid)),
        )
    return pr_id


def last_print_timestamp(conn) -> str | None:
    row = conn.execute(
        "SELECT timestamp FROM print_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["timestamp"] if row else None


# ---------------------------------------------------------------------------
# DB → in-memory release dicts (shape the draw code already expects)
# ---------------------------------------------------------------------------

def load_dj_releases(conn) -> list[dict]:
    releases = conn.execute(
        "SELECT id, artist, title, year, compilation, labels, genres, styles, rpm "
        "FROM releases WHERE is_dj_release = 1 ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for r in releases:
        tracks = conn.execute(
            "SELECT position, side, artist, title, duration, duration_s "
            "FROM tracks WHERE release_id = ? ORDER BY position",
            (r["id"],),
        ).fetchall()
        out.append({
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
        })
    return out


def _json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def build_bpm_lookup(conn, releases: list[dict]) -> dict[int, dict[str, dict]]:
    """Map (release_id → position → per-track BPM dict) using cache + overrides."""
    by_rp, by_tk, _ = load_overrides(conn)
    lookup: dict[int, dict[str, dict]] = {}
    for r in releases:
        per_pos: dict[str, dict] = {}
        release_artist = r.get("artist") or "V/A"
        for t in r["tracks"]:
            result = derive_track_result(
                conn,
                r["id"],
                t.get("position", ""),
                t.get("artist") or release_artist,
                t.get("title") or "",
                t.get("duration_s"),
                by_rp,
                by_tk,
            )
            per_pos[t.get("position", "")] = result
        lookup[r["id"]] = per_pos
    return lookup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sticker-w", type=float, default=DEFAULT_STICKER_W_MM,
        help=f"Sticker width in mm (default: {DEFAULT_STICKER_W_MM}).",
    )
    parser.add_argument(
        "--sticker-h", type=float, default=DEFAULT_STICKER_H_MM,
        help=f"Sticker height in mm (default: {DEFAULT_STICKER_H_MM}).",
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
        help="After a successful render, append the rendered release IDs to the "
        "print history (print_runs / print_run_releases tables).",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_PDF_OUT,
        help=f"Output PDF path (default: {DEFAULT_PDF_OUT.relative_to(ROOT)}). "
        "Parent directory is created if needed. A bare filename writes to the "
        "current working directory.",
    )
    args = parser.parse_args(argv)
    pdf_out: Path = args.output.expanduser()
    if pdf_out.is_dir() or str(args.output).endswith(("/", "\\")):
        pdf_out = pdf_out / "stickers.pdf"
    if pdf_out.suffix.lower() != ".pdf":
        pdf_out = pdf_out.with_suffix(".pdf")

    configure_layout(
        args.sticker_w, args.sticker_h,
        tile=args.tile, tile_cols=args.tile_cols, tile_rows=args.tile_rows,
    )

    with dbmod.session() as conn:
        releases = load_dj_releases(conn)
        if not releases:
            print(
                "ERROR: no DJ-filtered releases found. Run "
                "`bpm-stickers fetch && bpm-stickers filter` first.",
                file=sys.stderr,
            )
            return 2

        if args.new_only:
            already = already_printed_ids(conn)
            before = len(releases)
            releases = [r for r in releases if int(r["id"]) not in already]
            skipped = before - len(releases)
            last_ts = last_print_timestamp(conn)
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

        bpm_lookup = build_bpm_lookup(conn, releases)
        pdf_out.parent.mkdir(parents=True, exist_ok=True)

        c = canvas.Canvas(str(pdf_out), pagesize=A4)
        c.setTitle("Discogs DJ Stickers")
        sticker_count = draw_sticker_pages(c, releases, bpm_lookup)
        c.save()

        per_page = COLS * ROWS
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
            and bpm_lookup.get(r["id"], {}).get(t["position"], {}).get("reason") != "continuous_mix"
        )
        layout_note = " edge-to-edge (tile mode)" if TILE_MODE else ""
        print(
            f"Wrote {pdf_out}: {sticker_count} stickers from {len(releases)} releases across "
            f"{pages} sticker page(s) ({per_page}/page; {STICKER_W/mm:.1f}x{STICKER_H/mm:.1f} mm "
            f"on a {COLS}x{ROWS} grid{layout_note}){extra_note}."
        )
        if TILE_MODE:
            cuts = (COLS - 1) + (ROWS - 1)
            print(
                f"  Tile mode: slice each page with {cuts} straight ruler cuts "
                f"({COLS - 1} vertical + {ROWS - 1} horizontal). Print borderless or "
                "expect ~3 mm printer clipping on the outer stickers."
            )
        if missing_count:
            print(f"  {missing_count} tracks have an empty BPM box for handwriting.")

        if args.mark_printed:
            rendered_ids = [int(r["id"]) for r in releases]
            append_print_run(conn, rendered_ids)
            total_in_history = len(already_printed_ids(conn))
            print(
                f"  --mark-printed: appended {len(rendered_ids)} release(s) to "
                f"print history. Total in print history: {total_in_history}."
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
