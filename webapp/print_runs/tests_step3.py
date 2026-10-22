from django.test import SimpleTestCase

from sleeve_notes import sticker_layout as L
from sleeve_notes.preview import render_sheet_svg

from print_runs.flow import STOCKS, active_stock


class ActiveStockTests(SimpleTestCase):
    def test_each_stock_recognises_itself(self):
        for key, values in STOCKS.items():
            self.assertEqual(active_stock(values), key)

    def test_unknown_geometry_is_custom(self):
        self.assertEqual(
            active_stock({"tile": True, "tile_cols": 4, "tile_rows": 4}), "custom"
        )
        self.assertEqual(
            active_stock({"tile": False, "sticker_w": 80, "sticker_h": 40}), "custom"
        )


class SheetSvgTests(SimpleTestCase):
    def test_fills_every_slot_and_dashes_the_empty_ones(self):
        layout = L.derive_layout(tile=True, tile_cols=2, tile_rows=5)
        sticker = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {layout.sticker_w:.2f} {layout.sticker_h:.2f}" '
            f'style="width:100%"><text>x</text></svg>'
        )
        svg = render_sheet_svg([sticker, sticker, sticker], layout)
        self.assertEqual(svg.count("<text>x</text>"), 3)
        self.assertEqual(svg.count("stroke-dasharray"), layout.per_page - 3)
