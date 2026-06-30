"""Tests for the multi-view (separate-image-per-view) pipeline."""
import pytest

from pipeline import extractor
from pipeline.extractor import extract_drawing_data_multiview
from pipeline.macro_generator import generate_macro_package
from pipeline.validator import format_verification_report, run_verification
from pipeline.view_ingest import VIEW_ORDER, classify_view, discover_parts

# Reuse the extractor test doubles.
from tests.test_extractor import FakeClient, make_drawing_dict, tool_response


# --------------------------------------------------------------------------- #
# Ingestion / view classification
# --------------------------------------------------------------------------- #
class TestClassifyView:
    @pytest.mark.parametrize("name,expected", [
        ("front.png", "front"),
        ("01_front_elevation.png", "front"),
        ("TOP.jpg", "top"),
        ("plan_view.png", "top"),
        ("right_side.png", "side"),
        ("03_side.png", "side"),
        ("second_side.png", "second_side"),
        ("left.png", "second_side"),
        ("bottom.png", "bottom"),
        ("05_bottom.tif", "bottom"),
        ("02.png", "top"),       # numeric fallback (2 -> top)
        ("random.png", ""),      # unclassifiable
    ])
    def test_classify(self, name, expected):
        assert classify_view(name) == expected

    def test_view_order_is_canonical(self):
        assert VIEW_ORDER == ("front", "top", "side", "second_side", "bottom")


class TestDiscoverParts:
    def _touch(self, d, names):
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"")

    def test_subfolder_layout(self, tmp_path):
        self._touch(tmp_path / "PARTA", ["01_front.png", "02_top.png", "03_side.png"])
        self._touch(tmp_path / "PARTB", ["front.png", "bottom.png"])
        parts = {p.name: p for p in discover_parts(tmp_path)}
        assert set(parts) == {"PARTA", "PARTB"}
        assert [v for v, _ in parts["PARTA"].ordered_views] == ["front", "top", "side"]
        assert [v for v, _ in parts["PARTB"].ordered_views] == ["front", "bottom"]

    def test_single_folder_is_one_part(self, tmp_path):
        self._touch(tmp_path / "WIDGET", ["front.png", "top.png"])
        parts = discover_parts(tmp_path / "WIDGET")
        assert len(parts) == 1 and parts[0].name == "WIDGET"

    def test_missing_front_warns(self, tmp_path):
        self._touch(tmp_path / "P", ["top.png", "side.png"])
        part = discover_parts(tmp_path)[0]
        assert any("FRONT" in w for w in part.warnings)


# --------------------------------------------------------------------------- #
# Multi-view extraction request shape
# --------------------------------------------------------------------------- #
class TestMultiviewRequest:
    def test_labels_each_view_and_includes_instruction(self, monkeypatch):
        client = FakeClient([tool_response(make_drawing_dict(0.95))])
        monkeypatch.setattr(extractor, "_build_client", lambda *a, **k: client)
        views = [("front", "ZmZmZg==", "image/png"), ("top", "dHR0dA==", "image/png")]
        result = extract_drawing_data_multiview(views)
        assert isinstance(result, dict)
        content = client.messages.last_kwargs["messages"][0]["content"]
        texts = [b["text"] for b in content if b.get("type") == "text"]
        joined = "\n".join(texts)
        assert "FRONT VIEW — sketch on Front Plane" in joined
        assert "TOP VIEW — sketch on Top Plane" in joined
        assert "sketch_plane" in joined  # the multi-view instruction
        images = [b for b in content if b.get("type") == "image"]
        assert len(images) == 2
        assert all(b.get("cache_control") for b in images)

    def test_empty_views_raises(self):
        with pytest.raises(extractor.ExtractionError):
            extract_drawing_data_multiview([])


# --------------------------------------------------------------------------- #
# Per-plane macro generation
# --------------------------------------------------------------------------- #
def _multiview_part() -> dict:
    """Base plate (front) + a hole dimensioned in the top view + one in the side view."""
    return {
        "part_number": "MV-1",
        "units": "inch",
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 2.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"},
            {"id": "D004", "type": "diameter", "value": 0.25, "unit": "inch",
             "applies_to": "hole_diameter", "feature_ref": "F002"},
            {"id": "D005", "type": "diameter", "value": 0.30, "unit": "inch",
             "applies_to": "hole_diameter", "feature_ref": "F003"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "thru", "diameter": 0.25, "feature_ref": "F002", "view": "top"},
            {"id": "H002", "type": "thru", "diameter": 0.30, "feature_ref": "F003", "view": "side"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
             "sketch_plane": "front"},
            {"id": "F002", "type": "hole", "description": "hole from top view",
             "related_dimensions": ["D004"], "sketch_plane": "top"},
            {"id": "F003", "type": "hole", "description": "hole from side view",
             "related_dimensions": ["D005"], "sketch_plane": "side"},
        ],
        "build_order": ["F001", "F002", "F003"],
    }


class TestPerPlaneMacros:
    def test_each_view_builds_on_its_plane(self, tmp_path):
        data = _multiview_part()
        model, report = run_verification(data)
        assert report.ok, str(report)
        pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
        base = next(p for p in pkg.macros_dir.glob("01_*.vba")).read_text(encoding="utf-8")
        top_hole = next(p for p in pkg.macros_dir.glob("02_*.vba")).read_text(encoding="utf-8")
        side_hole = next(p for p in pkg.macros_dir.glob("03_*.vba")).read_text(encoding="utf-8")
        assert 'SelectRefPlane("Front Plane"' in base
        assert 'SelectRefPlane("Top Plane"' in top_hole
        assert 'SelectRefPlane("Right Plane"' in side_hole
        assert generate_macro_package  # sanity

    def test_second_side_and_bottom_map_to_planes(self):
        from pipeline.macro_generator import _plane_for
        from pipeline.schema import Feature

        assert _plane_for(Feature(id="F", type="hole", description="x", sketch_plane="second_side")) == "Right Plane"
        assert _plane_for(Feature(id="F", type="hole", description="x", sketch_plane="bottom")) == "Top Plane"
