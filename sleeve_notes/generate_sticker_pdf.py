"""Sticker PDF render primitives + CLI delegator.

The DB-aware top-level entry point lives at
``webapp/print_runs/management/commands/render_print_run.py`` (ORM-bound)
and ``webapp/print_runs/services/render.py`` (the function the web app
calls too). This module keeps only the pure pieces both reuse:

- ``PdfDrawer`` — ReportLab adapter for the ``Drawer`` protocol in
  ``sticker_layout``.
- ``DEFAULT_SETTINGS`` + ``normalize_settings`` — settings shape / coercion.

``main()`` exists so the legacy ``sleeve-notes render`` entry point keeps
working — it boots Django and hands argv to ``manage.py render_print_run``.
"""

from __future__ import annotations

import sys

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

from sleeve_notes import django_setup  # noqa: F401  ensures Django is set up
from sleeve_notes import sticker_layout as L


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
