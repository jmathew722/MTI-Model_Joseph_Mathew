"""Tests for pipeline.overview_check — the final overview cross-check pass."""
from pathlib import Path

from pipeline.overview_check import (
    _build_inventory,
    cross_check,
    run_overview_check,
)


def _extraction(**over):
    base = {
        "part_number": "TEST-1",
        "dimensions": [
            {"dimension_id": "D001", "value": 100.0, "resolved_value": 100.0},
            {"dimension_id": "D002", "value": 50.0},
            {"dimension_id": "D003", "value": 10.0},
        ],
        "hole_callouts": [
            {"hole_id": "H001", "type": "thru", "diameter": 6.0, "qty": 4},
        ],
        "features": [
            {"feature_id": "F001", "type": "extrude_boss", "description": "base plate"},
            {"feature_id": "F002", "type": "hole", "description": "mounting holes"},
            {"feature_id": "F003", "type": "fillet", "description": "corner fillets"},
        ],
    }
    base.update(over)
    return base


class TestInventory:
    def test_holes_counted_from_callout_qty(self):
        inv = _build_inventory(_extraction())
        assert inv["hole"] == 4

    def test_fillet_and_boss_from_features(self):
        inv = _build_inventory(_extraction())
        assert inv["fillet"] == 1
        assert inv["boss"] == 1

    def test_slot_matched_by_description_keyword(self):
        ex = _extraction(features=[
            {"feature_id": "F001", "type": "extrude_cut", "description": "keyway slot"},
        ])
        inv = _build_inventory(ex)
        assert inv["slot"] == 1
        assert inv["cutout"] == 1  # extrude_cut also counts as a cutout

    def test_hole_features_counted_when_no_callouts(self):
        ex = _extraction(hole_callouts=[])
        assert _build_inventory(ex)["hole"] == 1


class TestCrossCheck:
    def test_clearly_missing_feature_is_critical(self):
        overview = {"features": [
            {"kind": "slot", "count": 1, "description": "keyway on the left face",
             "clearly_visible": True},
        ]}
        items = cross_check(overview, _extraction())
        assert len(items) == 1
        assert items[0]["severity"] == "CRITICAL"
        assert items[0]["source"] == "overview"
        assert "keyway" in items[0]["what"]

    def test_possible_missing_feature_is_medium(self):
        overview = {"features": [
            {"kind": "rib", "count": 1, "description": "possible rib",
             "clearly_visible": False},
        ]}
        items = cross_check(overview, _extraction())
        assert items[0]["severity"] == "MEDIUM"

    def test_count_mismatch_is_high(self):
        overview = {"features": [
            {"kind": "hole", "count": 6, "description": "six mounting holes"},
        ]}
        items = cross_check(overview, _extraction())  # build has 4
        assert items[0]["severity"] == "HIGH"
        assert "6" in items[0]["what"] and "4" in items[0]["what"]

    def test_matched_feature_produces_no_item(self):
        overview = {"features": [
            {"kind": "hole", "count": 4, "description": "mounting holes"},
            {"kind": "fillet", "count": 4, "description": "corner fillets"},
        ]}
        # fillets are not count-checked (overview counts unreliable) — presence
        # suffices; holes match exactly.
        assert cross_check(overview, _extraction()) == []

    def test_extra_build_features_never_flagged(self):
        # Build has fillets/boss the overview doesn't mention -> fine.
        overview = {"features": [{"kind": "hole", "count": 4, "description": "holes"}]}
        assert cross_check(overview, _extraction()) == []

    def test_other_kind_is_informational(self):
        overview = {"features": [
            {"kind": "other", "count": 1, "description": "engraved logo"},
        ]}
        items = cross_check(overview, _extraction())
        assert items[0]["severity"] == "MEDIUM"

    def test_envelope_mismatch_is_high_and_unit_converted_match_passes(self):
        ex = _extraction()
        # 100 matches D001 exactly; 3.937in matches 100mm via conversion; 77 matches nothing.
        overview = {"features": [], "envelope": {"width": 3.937, "height": 77.0, "units": "inch"}}
        items = cross_check(overview, ex)
        assert len(items) == 1
        assert items[0]["severity"] == "HIGH"
        assert "height" in items[0]["what"]

    def test_sorted_most_urgent_first(self):
        overview = {"features": [
            {"kind": "hole", "count": 6, "description": "holes"},              # HIGH
            {"kind": "slot", "count": 1, "description": "slot", "clearly_visible": True},  # CRITICAL
        ]}
        items = cross_check(overview, _extraction())
        assert [i["severity"] for i in items] == ["CRITICAL", "HIGH"]


class TestRunWrapper:
    def test_no_overview_image_skips_gracefully(self):
        items, note = run_overview_check(None, _extraction())
        assert items == []
        assert note.startswith("skipped")

    def test_missing_file_skips_gracefully(self, tmp_path):
        items, note = run_overview_check(tmp_path / "nope.jpg", _extraction())
        assert items == []
        assert "no overview image" in note

    def test_api_failure_never_raises(self, tmp_path, monkeypatch):
        from PIL import Image

        img = tmp_path / "ov.png"
        Image.new("RGB", (200, 200), "white").save(img)
        import pipeline.overview_check as oc

        monkeypatch.setattr(oc, "extract_overview_features",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
        items, note = run_overview_check(img, _extraction())
        assert items == []
        assert "skipped" in note and "api down" in note
