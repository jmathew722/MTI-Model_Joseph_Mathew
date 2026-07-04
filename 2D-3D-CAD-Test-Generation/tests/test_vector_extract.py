"""Tests for pipeline.vector_extract — exact hole geometry from DXF/PDF/raster."""
import math

import pytest

from pipeline.vector_extract.dxf_holes import extract_dxf_geometry
from pipeline.vector_extract.pdf_holes import extract_pdf_geometry, fit_circle
from pipeline.vector_extract.geometry import group_full_circles


# --------------------------------------------------------------------------- #
# DXF (ezdxf)
# --------------------------------------------------------------------------- #
def _make_dxf(tmp_path, insunits=1):
    """A 4x2 (drawing-unit) plate outline with two exact holes, one arc-pair
    hole, one block-insert hole, and a TEXT callout."""
    import ezdxf

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = insunits
    msp = doc.modelspace()
    # Part outline: closed LWPOLYLINE rectangle with lower-left at (10, 20).
    msp.add_lwpolyline([(10, 20), (14, 20), (14, 22), (10, 22)], close=True)
    # Two plain CIRCLE holes.
    msp.add_circle((10.5, 20.5), radius=0.125)
    msp.add_circle((13.5, 21.5), radius=0.125)
    # One hole drawn as two half ARCs (same center/radius).
    msp.add_arc((12.0, 21.0), radius=0.25, start_angle=0, end_angle=180)
    msp.add_arc((12.0, 21.0), radius=0.25, start_angle=180, end_angle=360)
    # One hole inside a block, inserted with an offset.
    blk = doc.blocks.new(name="HOLEBLK")
    blk.add_circle((0, 0), radius=0.125)
    msp.add_blockref("HOLEBLK", insert=(11.0, 21.5))
    msp.add_text("4X ⌀.250 THRU", dxfattribs={"insert": (12.2, 21.1)})
    path = tmp_path / "plate.dxf"
    doc.saveas(path)
    return path


class TestDXF:
    def test_exact_circle_centers(self, tmp_path):
        geom = extract_dxf_geometry(_make_dxf(tmp_path))
        centers = sorted((round(c.cx, 9), round(c.cy, 9), round(c.r, 9)) for c in geom.circles)
        assert (10.5, 20.5, 0.125) in centers
        assert (13.5, 21.5, 0.125) in centers

    def test_arc_pair_groups_to_full_circle(self, tmp_path):
        geom = extract_dxf_geometry(_make_dxf(tmp_path))
        assert any(c.meta == "ARC-group" and abs(c.cx - 12.0) < 1e-9
                   and abs(c.cy - 21.0) < 1e-9 and abs(c.r - 0.25) < 1e-9
                   for c in geom.circles)

    def test_insert_circle_transformed(self, tmp_path):
        geom = extract_dxf_geometry(_make_dxf(tmp_path))
        ins = [c for c in geom.circles if c.meta.startswith("INSERT:")]
        assert len(ins) == 1
        assert abs(ins[0].cx - 11.0) < 1e-9 and abs(ins[0].cy - 21.5) < 1e-9

    def test_insunits_inches(self, tmp_path):
        geom = extract_dxf_geometry(_make_dxf(tmp_path, insunits=1))
        assert geom.native_units_to_mm == 25.4

    def test_insunits_mm(self, tmp_path):
        geom = extract_dxf_geometry(_make_dxf(tmp_path, insunits=4))
        assert geom.native_units_to_mm == 1.0

    def test_outline_from_closed_polyline(self, tmp_path):
        geom = extract_dxf_geometry(_make_dxf(tmp_path))
        boxes = [o for o in geom.outlines if o.meta != "loose-bbox"]
        assert any(abs(o.x0 - 10) < 1e-9 and abs(o.y0 - 20) < 1e-9
                   and abs(o.width - 4) < 1e-9 and abs(o.height - 2) < 1e-9 for o in boxes)

    def test_positioned_text(self, tmp_path):
        geom = extract_dxf_geometry(_make_dxf(tmp_path))
        assert any("THRU" in t.text for t in geom.texts)

    def test_unreadable_file_flags_fallback(self, tmp_path):
        bad = tmp_path / "junk.dxf"
        bad.write_text("this is not a dxf")
        geom = extract_dxf_geometry(bad)
        assert geom.is_raster
        assert any("falling back" in n for n in geom.notes)

    def test_missing_dwg_converter_flags_fallback(self, tmp_path):
        # A DWG without the ODA converter must degrade LOUDLY, not silently.
        fake = tmp_path / "part.dwg"
        fake.write_bytes(b"AC1032 fake dwg")
        geom = extract_dxf_geometry(fake)
        if geom.is_raster:  # no ODA on this machine (the expected CI case)
            assert any("FALLBACK" in n or "converted" in n for n in geom.notes)


class TestArcGrouping:
    def test_four_quarter_arcs_group(self):
        arcs = [(1.0, 2.0, 0.5, a, a + 90) for a in (0, 90, 180, 270)]
        assert group_full_circles(arcs) == [(1.0, 2.0, 0.5)]

    def test_fillet_arcs_do_not_group(self):
        arcs = [(0, 0, 0.5, 0, 90), (10, 0, 0.5, 90, 180)]  # distinct centers
        assert group_full_circles(arcs) == []


# --------------------------------------------------------------------------- #
# Vector PDF (PyMuPDF)
# --------------------------------------------------------------------------- #
def _make_pdf(tmp_path, with_circles=True):
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    # Part outline rect (page frame, y-down): x 50..350, y 50..250.
    page.draw_rect(fitz.Rect(50, 50, 350, 250), color=(0, 0, 0), width=1)
    if with_circles:
        # draw_circle emits the standard 4-Bézier circle encoding.
        page.draw_circle(fitz.Point(100, 100), 12, color=(0, 0, 0), width=0.8)
        page.draw_circle(fitz.Point(300, 200), 12, color=(0, 0, 0), width=0.8)
        # Concentric counterbore signature.
        page.draw_circle(fitz.Point(200, 150), 8, color=(0, 0, 0), width=0.8)
        page.draw_circle(fitz.Point(200, 150), 16, color=(0, 0, 0), width=0.8)
        # Centerline cross through the first hole.
        page.draw_line(fitz.Point(80, 100), fitz.Point(120, 100))
        page.draw_line(fitz.Point(100, 80), fitz.Point(100, 120))
    page.insert_text(fitz.Point(210, 145), "2X ⌀6.35 THRU")
    path = tmp_path / "plate.pdf"
    doc.save(str(path))
    doc.close()
    return path


class TestVectorPDF:
    def test_circle_fit_centers_exact(self, tmp_path):
        geom = extract_pdf_geometry(_make_pdf(tmp_path))
        # Page height 300; y flips: (100,100) -> (100,200); (300,200) -> (300,100).
        def has(cx, cy, r):
            return any(math.hypot(c.cx - cx, c.cy - cy) < 0.05 and abs(c.r - r) < 0.05
                       for c in geom.circles)
        assert has(100, 200, 12)
        assert has(300, 100, 12)

    def test_concentric_pair_both_detected(self, tmp_path):
        geom = extract_pdf_geometry(_make_pdf(tmp_path))
        at_center = [c for c in geom.circles if math.hypot(c.cx - 200, c.cy - 150) < 0.05]
        assert sorted(round(c.r) for c in at_center) == [8, 16]

    def test_centerline_cross_marks_center(self, tmp_path):
        geom = extract_pdf_geometry(_make_pdf(tmp_path))
        marked = [c for c in geom.circles if c.center_marked]
        assert any(math.hypot(c.cx - 100, c.cy - 200) < 0.05 for c in marked)

    def test_outline_rect_extracted(self, tmp_path):
        geom = extract_pdf_geometry(_make_pdf(tmp_path))
        boxes = [o for o in geom.outlines if o.meta.startswith("re#")]
        assert any(abs(o.width - 300) < 0.01 and abs(o.height - 200) < 0.01 for o in boxes)

    def test_positioned_words(self, tmp_path):
        geom = extract_pdf_geometry(_make_pdf(tmp_path))
        assert any("THRU" in t.text for t in geom.texts)

    def test_raster_pdf_flagged(self, tmp_path):
        import fitz

        # A page that is only an embedded image = a scan.
        src = fitz.open()
        p = src.new_page(width=200, height=200)
        p.draw_rect(fitz.Rect(20, 20, 180, 180), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
        pix = p.get_pixmap(dpi=72)
        src.close()
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.insert_image(fitz.Rect(0, 0, 200, 200), pixmap=pix)
        path = tmp_path / "scan.pdf"
        doc.save(str(path))
        doc.close()
        geom = extract_pdf_geometry(path)
        assert geom.is_raster
        assert any("RASTER" in n for n in geom.notes)


class TestCircleFit:
    def test_exact_circle(self):
        pts = [(5 + 3 * math.cos(t), 7 + 3 * math.sin(t))
               for t in [i * math.pi / 8 for i in range(16)]]
        cx, cy, r, resid = fit_circle(pts)
        assert abs(cx - 5) < 1e-9 and abs(cy - 7) < 1e-9 and abs(r - 3) < 1e-9
        assert resid < 1e-9

    def test_collinear_points_rejected(self):
        assert fit_circle([(0, 0), (1, 1), (2, 2), (3, 3)]) is None

    def test_ellipse_fails_circularity(self):
        pts = [(6 * math.cos(t), 2 * math.sin(t))
               for t in [i * math.pi / 8 for i in range(16)]]
        fit = fit_circle(pts)
        assert fit is None or fit[3] > 0.02  # residual must reject it


# --------------------------------------------------------------------------- #
# Raster fallback (OpenCV Hough)
# --------------------------------------------------------------------------- #
class TestRasterHough:
    def test_hough_finds_synthetic_holes(self, tmp_path):
        import numpy as np
        import cv2

        from pipeline.vector_extract.raster_holes import extract_raster_geometry

        img = np.full((600, 800), 255, dtype=np.uint8)
        cv2.rectangle(img, (100, 100), (700, 500), 0, 2)          # part outline
        for (x, y) in ((200, 200), (600, 200), (200, 400), (600, 400)):
            cv2.circle(img, (x, y), 30, 0, 2)                      # 4 hole rings
        path = tmp_path / "drawing.png"
        cv2.imwrite(str(path), img)

        geom = extract_raster_geometry(path)
        assert geom.is_raster and geom.source_kind == "raster"
        # y-up flip: image (200,200) -> (200, 400) in the drawing frame.
        expect = [(200, 400), (600, 400), (200, 200), (600, 200)]
        for ex, ey in expect:
            assert any(abs(c.cx - ex) <= 3 and abs(c.cy - ey) <= 3 for c in geom.circles), \
                f"missing hole near {(ex, ey)}: {[(round(c.cx), round(c.cy)) for c in geom.circles]}"
        assert any("RASTER" in n for n in geom.notes)  # loud, flagged fallback

    def test_callout_crops_positions(self, tmp_path):
        import numpy as np
        import cv2

        from pipeline.vector_extract.raster_holes import callout_crops, extract_raster_geometry

        img = np.full((300, 400), 255, dtype=np.uint8)
        cv2.circle(img, (200, 150), 25, 0, 2)
        path = tmp_path / "one_hole.png"
        cv2.imwrite(str(path), img)
        geom = extract_raster_geometry(path)
        crops = callout_crops(path, geom)
        assert crops and all(len(c["bbox"]) == 4 for c in crops)
