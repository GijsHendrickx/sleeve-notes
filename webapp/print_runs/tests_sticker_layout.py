"""Engine-level checks on sticker geometry: the readability promise.

Fixtures are the two real tracklists (public Discogs metadata) where a
render of the collection broke it: Angelic's AAA1 drawn over the title,
and Romanthony's remix names cut off with "…" at the smallest font.
"""

from django.test import SimpleTestCase
from reportlab.pdfbase.pdfmetrics import stringWidth

from sleeve_notes import sticker_layout as L
from sleeve_notes.preview import render_release_stickers_svg


ANGELIC = {
    "id": 14893,
    "artist": "Angelic",
    "title": "It's My Turn",
    "compilation": False,
    "rpm": ["33⅓"],
    "tracks": [
        {"position": "A1", "title": "It's My Turn (Rank 1 Mix)", "duration": "7:58"},
        {"position": "A2", "title": "It's My Turn (Ariel Mix)", "duration": "7:35"},
        {"position": "AA1", "title": "It's My Turn (4 Strings Mix)", "duration": "8:04"},
        {"position": "AAA1", "title": "It's My Turn (Original Mix)", "duration": "7:59"},
    ],
}

ROMANTHONY = {
    "id": 14892,
    "artist": "Romanthony Presents Naida",
    "title": "Do You Think You Can Love Me",
    "compilation": False,
    "rpm": ["33⅓"],
    "tracks": [
        {"position": "A1", "title": "Do You Think You Can Love Me (Lookin' Glasgow Remix Vox #40)", "duration": ""},
        {"position": "A2", "title": "Do You Think You Can Love Me (Soundsystem Vocal #40)", "duration": ""},
        {"position": "B1", "title": "Do You Think You Can Love Me (Tru-Skool Vocal #14)", "duration": ""},
        {"position": "B2", "title": "Do You Think You Can Love Me (DJ MK Vocal #66)", "duration": ""},
    ],
}


class RecordingDrawer:
    def __init__(self):
        self.texts: list[dict] = []

    def text(self, x, y, text, *, font, size, color, anchor="start"):
        self.texts.append(dict(x=x, y=y, text=text, font=font, size=size, anchor=anchor))

    def rect(self, *a, **kw): pass
    def line(self, *a, **kw): pass
    def circle(self, *a, **kw): pass
    def qr(self, *a, **kw): pass


def draw(release, layout=None, **kw):
    layout = layout or L.derive_layout()
    drawer = RecordingDrawer()
    fit = L.draw_sticker(
        drawer, 0.0, 0.0, release, L.group_tracks_by_side(release["tracks"]), {}, layout, **kw
    )
    return drawer, fit


def track_rows(drawer, layout=None):
    """Position texts and title lines: everything drawn below the header."""
    layout = layout or L.derive_layout()
    below_header = layout.sticker_h - L.PAD_Y - L.HEADER_H
    positions = [t for t in drawer.texts if t["font"] == "Courier-Bold"]
    titles = [
        t for t in drawer.texts
        if t["font"] == "Helvetica" and t["anchor"] == "start" and t["y"] < below_header
    ]
    return positions, titles


class PositionColumnTests(SimpleTestCase):
    def test_long_vinyl_position_does_not_overlap_the_title(self):
        drawer, _ = draw(ANGELIC)
        positions, titles = track_rows(drawer)
        self.assertEqual([p["text"] for p in positions], ["A1", "A2", "AA1", "AAA1"])
        for pos in positions:
            title = next(t for t in titles if abs(t["y"] - pos["y"]) < 0.01)
            pos_right = pos["x"] + stringWidth(pos["text"], pos["font"], pos["size"])
            self.assertLess(pos_right, title["x"], f"{pos['text']} runs into its title")

    def test_titles_share_one_left_edge(self):
        drawer, _ = draw(ANGELIC)
        _, titles = track_rows(drawer)
        self.assertEqual(len({round(t["x"], 3) for t in titles}), 1)

    def test_ordinary_positions_keep_the_old_column(self):
        for size in L.TRACK_FONT_CANDIDATES:
            self.assertEqual(
                L.column_widths(size, 250, ["A1", "B2", "AA1"]),
                L.column_widths(size, 250),
            )


class LongTitleTests(SimpleTestCase):
    def test_no_track_text_below_the_floor(self):
        drawer, fit = draw(ROMANTHONY)
        _, titles = track_rows(drawer)
        self.assertTrue(titles)
        for t in titles:
            self.assertGreaterEqual(t["size"], L.TRACK_FONT_FLOOR)
        self.assertGreaterEqual(fit.tracks.font, L.TRACK_FONT_FLOOR)

    def test_wraps_instead_of_cutting_off(self):
        drawer, fit = draw(ROMANTHONY)
        _, titles = track_rows(drawer)
        drawn = " ".join(t["text"] for t in titles)
        self.assertNotIn("…", drawn)
        for track in ROMANTHONY["tracks"]:
            self.assertIn(track["title"], drawn)
        self.assertTrue(fit.tracks.wrapped)
        self.assertEqual(fit.tracks.truncated, ())
        self.assertTrue(fit.uses_fallback)
        self.assertTrue(fit.lossless)

    def test_wrapped_lines_stay_inside_the_title_column_and_the_sticker(self):
        layout = L.derive_layout()
        drawer, fit = draw(ROMANTHONY, layout)
        _, titles = track_rows(drawer, layout)
        left = titles[0]["x"]
        for t in titles:
            self.assertLessEqual(
                stringWidth(t["text"], t["font"], t["size"]), fit.tracks.middle_w + 0.01
            )
            self.assertAlmostEqual(t["x"], left)
            self.assertGreaterEqual(t["y"], L.PAD_Y - 0.01)
        self.assertLessEqual(max(t["x"] for t in titles), layout.sticker_w)

    def test_svg_preview_shows_the_same_lines(self):
        svg = "".join(render_release_stickers_svg(ROMANTHONY, {}, L.derive_layout()))
        self.assertNotIn("…", svg)
        self.assertIn("#40)", svg)

    def test_truncation_only_with_the_flag(self):
        release = {
            **ROMANTHONY,
            "tracks": [
                {"position": f"A{i}", "title": "Endless Remix " * 30, "duration": ""}
                for i in range(1, 5)
            ],
        }
        layout = L.derive_layout(70, 42.4)
        drawer, fit = draw(release, layout)
        _, titles = track_rows(drawer, layout)
        cut = [t for t in titles if "…" in t["text"]]
        self.assertTrue(cut)
        self.assertTrue(fit.tracks.truncated)
        self.assertFalse(fit.lossless)
        for t in titles:
            self.assertGreaterEqual(t["size"], L.TRACK_FONT_FLOOR)

    def test_a_sticker_that_fits_uses_no_fallback(self):
        _, fit = draw(ANGELIC)
        self.assertEqual(fit.tracks.font, L.TRACK_FONT_CANDIDATES[0])
        self.assertFalse(fit.uses_fallback)

    def test_hidden_titles_never_count_as_cut(self):
        _, fit = draw(ROMANTHONY, show_track_title=False)
        self.assertFalse(fit.uses_fallback)


class WrapTextTests(SimpleTestCase):
    def test_breaks_on_words_and_flags_only_real_loss(self):
        lines, truncated = L.wrap_text("one two three four", 40, "Helvetica", 10, 4)
        self.assertEqual(" ".join(lines), "one two three four")
        self.assertFalse(truncated)
        lines, truncated = L.wrap_text("one two three four", 40, "Helvetica", 10, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertTrue(truncated)

    def test_hard_breaks_a_word_wider_than_the_line(self):
        lines, truncated = L.wrap_text("Supercalifragilistic", 30, "Helvetica", 10, 5)
        self.assertEqual("".join(lines), "Supercalifragilistic")
        self.assertFalse(truncated)
        for line in lines:
            self.assertLessEqual(stringWidth(line, "Helvetica", 10), 30)
