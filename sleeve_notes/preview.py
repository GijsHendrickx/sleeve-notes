"""Single-release sticker preview as inline SVG.

The PDF renderer and this previewer share the layout/composition logic in
``sleeve_notes.sticker_layout`` via the ``Drawer`` protocol. ``SvgDrawer`` is
the only thing here that's PDF-format-specific — the rest of a sticker's
geometry is reused verbatim.

Coordinates from the layout module are PDF y-up. We render onto an SVG
canvas sized in points (matching ``viewBox``) and flip Y in each draw call.
"""

from __future__ import annotations

from html import escape

import qrcode
from reportlab.lib.units import mm

from sleeve_notes import sticker_layout as L


_PDF_FONT_TO_SVG = {
    "Helvetica": ('Helvetica, Arial, sans-serif', "normal", "normal"),
    "Helvetica-Bold": ('Helvetica, Arial, sans-serif', "bold", "normal"),
    "Helvetica-Oblique": ('Helvetica, Arial, sans-serif', "normal", "italic"),
    "Courier-Bold": ("'Courier New', Courier, monospace", "bold", "normal"),
}


def _font_attrs(font: str, size: float) -> str:
    family, weight, style = _PDF_FONT_TO_SVG.get(font, ('Helvetica, Arial, sans-serif', "normal", "normal"))
    return (
        f'font-family="{family}" font-size="{size:.2f}" '
        f'font-weight="{weight}" font-style="{style}"'
    )


class SvgDrawer:
    """``Drawer`` adapter emitting SVG markup. Y is flipped vs. PDF coords.

    ``offset_x`` / ``offset_y`` shift incoming PDF coordinates so the
    sticker's bottom-left sits at the viewBox origin. ``height`` is the
    SVG viewBox height (used for the Y-flip).
    """

    def __init__(self, width: float, height: float, offset_x: float = 0.0, offset_y: float = 0.0) -> None:
        self.width = width
        self.height = height
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.elements: list[str] = []

    def _x(self, x: float) -> float:
        return x - self.offset_x

    def _y(self, y: float) -> float:
        return self.height - (y - self.offset_y)

    def text(self, x, y, text, *, font, size, color, anchor="start"):
        anchor_attr = 'text-anchor="end"' if anchor == "end" else 'text-anchor="start"'
        self.elements.append(
            f'<text x="{self._x(x):.2f}" y="{self._y(y):.2f}" fill="{color}" '
            f'{anchor_attr} {_font_attrs(font, size)}>{escape(text)}</text>'
        )

    def rect(self, x, y, w, h, *, stroke=None, fill=None, stroke_width=0.5):
        attrs = [
            f'x="{self._x(x):.2f}"',
            f'y="{self._y(y + h):.2f}"',
            f'width="{w:.2f}"',
            f'height="{h:.2f}"',
        ]
        attrs.append(f'fill="{fill}"' if fill else 'fill="none"')
        if stroke:
            attrs.append(f'stroke="{stroke}"')
            attrs.append(f'stroke-width="{stroke_width:.2f}"')
        else:
            attrs.append('stroke="none"')
        self.elements.append(f'<rect {" ".join(attrs)} />')

    def line(self, x1, y1, x2, y2, *, color, width):
        self.elements.append(
            f'<line x1="{self._x(x1):.2f}" y1="{self._y(y1):.2f}" '
            f'x2="{self._x(x2):.2f}" y2="{self._y(y2):.2f}" '
            f'stroke="{color}" stroke-width="{width:.2f}" />'
        )

    def circle(self, cx, cy, r, *, fill):
        self.elements.append(
            f'<circle cx="{self._x(cx):.2f}" cy="{self._y(cy):.2f}" '
            f'r="{r:.2f}" fill="{fill}" />'
        )

    def qr(self, x, y, size, data):
        q = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=0,
        )
        q.add_data(data)
        q.make(fit=True)
        matrix = q.get_matrix()
        n = len(matrix)
        cell = size / n
        # Anchor at top-left in SVG coords (= top of QR in PDF coords = y + size).
        top_left_x = self._x(x)
        top_left_y = self._y(y + size)
        rects: list[str] = []
        for row_i, row in enumerate(matrix):
            for col_i, val in enumerate(row):
                if not val:
                    continue
                rects.append(
                    f'<rect x="{top_left_x + col_i * cell:.3f}" '
                    f'y="{top_left_y + row_i * cell:.3f}" '
                    f'width="{cell:.3f}" height="{cell:.3f}" fill="#000" />'
                )
        self.elements.append("".join(rects))

    def to_svg(self) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {self.width:.2f} {self.height:.2f}" '
            f'width="{self.width / mm:.2f}mm" height="{self.height / mm:.2f}mm" '
            f'style="display:block;background:#fff">'
            + "".join(self.elements)
            + "</svg>"
        )


def render_release_stickers_svg(
    release: dict,
    bpm_lookup_for_release: dict[str, dict],
    layout: L.LayoutConfig,
    *,
    qr: bool = True,
    show_artist: bool = True,
    show_title: bool = True,
    show_rpm: bool = True,
    show_key: bool = True,
    show_bpm: bool = True,
    show_duration: bool = True,
    show_track_title: bool = True,
    show_sides: bool = True,
) -> list[str]:
    """Render every sticker for one release as a list of inline SVG strings.

    Most releases produce one sticker. Multi-disc or >8-track releases
    produce several; the caller decides how to lay them out.
    """
    stickers = L.split_release_into_stickers(release)
    svgs: list[str] = []
    for sides_map in stickers:
        drawer = SvgDrawer(width=layout.sticker_w, height=layout.sticker_h)
        L.draw_sticker(
            drawer, 0.0, 0.0, release, sides_map, bpm_lookup_for_release, layout, len(stickers),
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
        svgs.append(drawer.to_svg())
    return svgs
