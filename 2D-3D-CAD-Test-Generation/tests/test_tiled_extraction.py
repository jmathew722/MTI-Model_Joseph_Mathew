"""Tests for Workstream 2 — tiled high-resolution extraction.

The VLM calls are injected (fake extract_fn/global_fn) so nothing here makes a
paid API call. Rasterization is exercised against a real golden PDF when one is
present; the trigger/tiling/stitch/datum logic is pure.
"""
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
from PIL import Image

from utils.tiled_extraction import (
    Tile,
    datum_anchor,
    ink_density,
    make_tiles,
    should_tile,
    stitch,
    tiled_extract,
)


def _blank(w=200, h=200):
    return Image.new("L", (w, h), 255)


def _with_ink(w=200, h=200, lines=10):
    img = Image.new("L", (w, h), 255)
    a = np.asarray(img).copy()
    for i in range(lines):
        a[i * (h // lines), :] = 0  # horizontal black lines
    return Image.fromarray(a, "L")


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #
class TestShouldTile:
    def test_blank_image_fires(self):
        t = should_tile(image=_blank())
        assert t.fire and any("blank" in r for r in t.reasons)

    def test_low_confidence_fires(self):
        t = should_tile(extraction={"confidence": 0.4, "dimensions": []})
        assert t.fire and any("confidence" in r for r in t.reasons)

    def test_many_unclear_fires(self):
        dims = [{"value_unclear": True}] * 3 + [{"value_unclear": False}]
        t = should_tile(extraction={"confidence": 0.9, "dimensions": dims})
        assert t.fire and any("unclear" in r for r in t.reasons)

    def test_large_sheet_fires(self):
        t = should_tile(page_area_sqin=17 * 22, raster_long_edge=2576)
        assert t.fire and any("large sheet" in r for r in t.reasons)

    def test_clean_small_drawing_does_not_fire(self):
        t = should_tile(image=_with_ink(lines=40),
                        extraction={"confidence": 0.9,
                                    "dimensions": [{"value_unclear": False}]},
                        page_area_sqin=8.5 * 11)
        assert not t.fire

    def test_bool_protocol(self):
        assert bool(should_tile(image=_blank())) is True


class TestInkDensity:
    def test_blank_is_near_zero(self):
        assert ink_density(_blank()) < 0.005

    def test_inked_is_higher(self):
        assert ink_density(_with_ink(lines=40)) > ink_density(_blank())


# --------------------------------------------------------------------------- #
# Tiling
# --------------------------------------------------------------------------- #
class TestMakeTiles:
    def test_small_image_one_tile(self):
        tiles = make_tiles(1000, 800, tile=1500)
        assert len(tiles) == 1 and tiles[0].x1 == 1000 and tiles[0].y1 == 800

    def test_overlap_and_full_coverage(self):
        tiles = make_tiles(4000, 3000, tile=1500, overlap=0.22)
        assert len(tiles) > 1
        # last tile clamps to the edge (full coverage)
        assert max(t.x1 for t in tiles) == 4000
        assert max(t.y1 for t in tiles) == 3000
        # neighbours overlap: step < tile
        xs = sorted({t.x0 for t in tiles})
        assert xs[1] - xs[0] < 1500

    def test_tiles_are_sheet_coordinates(self):
        tiles = make_tiles(4000, 1500, tile=1500)
        # a right-hand tile has a non-zero x0 offset
        assert any(t.x0 > 0 for t in tiles)


# --------------------------------------------------------------------------- #
# Stitch + dedupe
# --------------------------------------------------------------------------- #
class TestStitch:
    def test_same_anchor_same_value_merges(self):
        dims = [{"id": "D1", "anchor": [100, 100], "value": 4.0, "tile": "r0c0"},
                {"id": "D1b", "anchor": [104, 98], "value": 4.0, "tile": "r0c1"}]
        merged = stitch(dims, tol_px=25)
        assert len(merged) == 1
        assert set(merged[0]["source_tiles"]) == {"r0c0", "r0c1"}
        assert not merged[0].get("value_unclear")

    def test_same_anchor_diff_value_keeps_both_as_candidates(self):
        dims = [{"id": "D1", "anchor": [100, 100], "value": 4.0, "tile": "r0c0"},
                {"id": "D1b", "anchor": [102, 101], "value": 4.5, "tile": "r0c1"}]
        merged = stitch(dims, tol_px=25)
        assert len(merged) == 1
        assert merged[0]["value_unclear"] is True
        assert sorted(merged[0]["possible_values"]) == [4.0, 4.5]

    def test_far_anchors_stay_separate(self):
        dims = [{"anchor": [100, 100], "value": 4.0}, {"anchor": [900, 900], "value": 2.0}]
        assert len(stitch(dims, tol_px=25)) == 2

    def test_unanchored_deduped_by_value(self):
        dims = [{"applies_to": "length", "value": 4.0},
                {"applies_to": "length", "value": 4.0},
                {"applies_to": "width", "value": 2.0}]
        assert len(stitch(dims)) == 2


class TestDatumAnchor:
    def test_reexpresses_relative_to_origin(self):
        # origin at (0, 200) [lower-left in image coords], +Y up
        dims = [{"anchor": [50, 150]}]
        out = datum_anchor(dims, (0.0, 200.0), px_per_unit=10.0)
        assert out[0]["anchor_from_datum"] == [5.0, 5.0]  # (50-0)/10, (200-150)/10


# --------------------------------------------------------------------------- #
# Orchestration with injected (fake) VLM calls — no paid API
# --------------------------------------------------------------------------- #
class TestTiledExtractInjected:
    def test_end_to_end_with_fakes(self, tmp_path, monkeypatch):
        import utils.tiled_extraction as te

        big = _with_ink(4000, 3000, lines=60)
        monkeypatch.setattr(te, "adaptive_render", lambda *a, **k: (big, 600, 3.0))

        def fake_global(img):
            return {"views": [{"view_type": "Front"}], "datum_origin_px": [0.0, img.height]}

        calls = {"n": 0}

        def fake_extract(tile_img, ctx):
            calls["n"] += 1
            # each tile reports one dim with a tile-local anchor
            return {"dimensions": [{"id": f"D{calls['n']}", "anchor": [10, 10],
                                    "value": 1.0, "applies_to": "length"}]}

        res = tiled_extract(tmp_path / "x.pdf", 1, extract_fn=fake_extract,
                            global_fn=fake_global, cache_dir=tmp_path)
        assert calls["n"] >= 1                       # tiles were extracted
        assert res["tiled"]["tiles_sent"] >= 1
        assert res["dimensions"]                     # stitched output present
        # anchors were shifted into SHEET coordinates + datum-anchored
        assert all("anchor_from_datum" in d for d in res["dimensions"])
        # cache written
        assert list(tmp_path.glob("tiled_*.json"))

    def test_blank_tiles_skipped(self, tmp_path, monkeypatch):
        import utils.tiled_extraction as te
        # A mostly-blank sheet with ink only in the top-left tile.
        img = Image.new("L", (4000, 3000), 255)
        a = np.asarray(img).copy()
        a[0:1400, 0:1400] = np.where(np.arange(1400) % 20 == 0, 0, 255)[None, :]
        img = Image.fromarray(a, "L")
        monkeypatch.setattr(te, "adaptive_render", lambda *a, **k: (img, 600, 3.0))
        sent = []

        def fake_extract(tile_img, ctx):
            sent.append(ctx["tile"])
            return {"dimensions": []}

        res = tiled_extract(tmp_path / "x.pdf", 1, extract_fn=fake_extract, global_fn=None)
        # not every tile was sent — blank margins skipped
        assert res["tiled"]["tiles_sent"] < res["tiled"]["tiles_total"]


# --------------------------------------------------------------------------- #
# Real rasterization against a golden PDF (no API), if available
# --------------------------------------------------------------------------- #
def _find_pdf():
    for base in ("../Test2", "../DrawingPDFs", "../NewVerifiedDrawings", ".."):
        p = Path(base)
        if p.exists():
            for pdf in p.rglob("*.pdf"):
                return pdf
            for pdf in p.rglob("*.PDF"):
                return pdf
    return None


class TestRealRaster:
    def test_adaptive_render_on_real_pdf(self):
        pytest.importorskip("fitz")
        pdf = _find_pdf()
        if pdf is None:
            pytest.skip("no golden PDF available")
        from utils.tiled_extraction import adaptive_render, median_line_width_px
        img, dpi, lw = adaptive_render(pdf, 1, dpis=(150, 300))
        assert img.width > 0 and img.height > 0
        assert dpi in (150, 300)
        assert lw >= 0.0
