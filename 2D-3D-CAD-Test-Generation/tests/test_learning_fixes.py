"""Regression tests for the 2026-07-09 learning-loop fix cycle.

Covers the self-contained, verifiable fixes:
  * Fix 2.3 — shared quantity-language parser + AGGREGATE (group-aware) overview
    count reconciliation (kills the "2 vs 5" false positives).
  * Fix 2.2 — TYP propagation fills dimensionless sibling fillets/chamfers.
  * Fix 2.1 — a fillet/chamfer with no driving dimension surfaces as a CRITICAL
    review flag routed to markup.
  * Fix 1.3 / 4.2 — learning-loop failure fingerprint + rapid-rerun detection.
"""
import json

import pytest

from pipeline.callout_qty import is_typ, parse_quantity
from pipeline.overview_check import cross_check
from pipeline.resolver import resolve_extraction


# --------------------------------------------------------------------------- #
# Fix 2.3a — quantity-language parser
# --------------------------------------------------------------------------- #
class TestQuantityParser:
    @pytest.mark.parametrize("text,expected", [
        (".406 DIA THRU (2) HL'S", 2),
        (".422 DIA 6-HOLES", 6),
        ("1/4-20 UNC 4 PLACES", 4),
        ("(6) HLS", 6),
        ("3 PL", 3),
        ("2 REQD", 2),
        (".531 R", 1),          # no explicit qty -> default 1
        ("", 1),
    ])
    def test_parse_quantity(self, text, expected):
        assert parse_quantity(text) == expected

    def test_is_typ(self):
        assert is_typ("R.531 TYP") and is_typ(".06 x 45 typical")
        assert not is_typ(".406 DIA THRU (2) HL'S")


# --------------------------------------------------------------------------- #
# Fix 2.3b — aggregate group-aware overview count reconciliation
# --------------------------------------------------------------------------- #
def _extraction_with_holes(*qtys: int) -> dict:
    return {
        "part_number": "T", "units": "inch",
        "hole_callouts": [{"id": f"H{i}", "type": "thru", "qty": q}
                          for i, q in enumerate(qtys, 1)],
        "features": [], "dimensions": [], "relationships": {},
    }


class TestAggregateCountCheck:
    def test_multi_group_summing_to_total_is_not_flagged(self):
        # Build has 2+3 = 5 holes; overview lists a 2-hole and a 3-hole callout.
        # The old code compared each group to the total (5) -> two false "mismatch"
        # HIGH flags. The aggregate check must raise NONE.
        extraction = _extraction_with_holes(2, 3)
        overview = {"features": [
            {"kind": "hole", "count": 2, "description": ".406 (2) HL'S"},
            {"kind": "hole", "count": 3, "description": ".250 (3) HL'S"},
        ]}
        items = cross_check(overview, extraction)
        assert not [i for i in items if "count" in i.get("affects", "").lower()], items

    def test_a001821m_case_7_plus_6_equals_13(self):
        extraction = _extraction_with_holes(7, 6)   # total 13
        overview = {"features": [
            {"kind": "hole", "count": 7, "description": "M6 pattern"},
            {"kind": "hole", "count": 6, "description": ".422 6-HOLES"},
        ]}
        items = cross_check(overview, extraction)
        assert not [i for i in items if "count" in i.get("affects", "").lower()]

    def test_genuine_shortfall_is_flagged(self):
        # Overview says 8 holes total but the build only has 5 -> real shortfall.
        extraction = _extraction_with_holes(5)
        overview = {"features": [{"kind": "hole", "count": 8, "description": "8-HOLES"}]}
        items = cross_check(overview, extraction)
        short = [i for i in items if "shortfall" in i.get("what", "").lower()]
        assert short and short[0]["severity"] == "HIGH"

    def test_total_absence_still_critical(self):
        extraction = _extraction_with_holes()  # no holes at all
        overview = {"features": [{"kind": "hole", "count": 3, "description": "3 holes",
                                  "clearly_visible": True}]}
        items = cross_check(overview, extraction)
        assert any(i["severity"] == "CRITICAL" for i in items)


# --------------------------------------------------------------------------- #
# Fix 2.2 — TYP propagation ; Fix 2.1 — missing-dimension review flag
# --------------------------------------------------------------------------- #
def _two_fillets(second_has_radius: bool) -> dict:
    dims = [
        {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
        {"id": "D002", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
        {"id": "D003", "type": "linear", "value": 0.5, "unit": "inch", "applies_to": "thickness"},
        {"id": "R001", "type": "radial", "value": 0.531, "unit": "inch",
         "applies_to": "fillet_radius", "notes": ".531 R. TYP", "feature_ref": "F002"},
    ]
    if second_has_radius:
        dims.append({"id": "R002", "type": "radial", "value": 0.25, "unit": "inch",
                     "applies_to": "fillet_radius", "feature_ref": "F003"})
    return {
        "part_number": "TYP-1", "units": "inch", "confidence": 0.9,
        "general_tolerance": ".XX +/-.01",
        "dimensions": dims,
        "hole_callouts": [],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base plate",
             "related_dimensions": ["D001", "D002"],
             "depth_dimension_id": "D003", "position_known": True},
            {"id": "F002", "type": "fillet", "description": "corner fillet A",
             "related_dimensions": ["R001"], "parent_feature": "F001"},
            {"id": "F003", "type": "fillet", "description": "corner fillet B",
             "related_dimensions": (["R002"] if second_has_radius else []), "parent_feature": "F001"},
        ],
        "build_order": ["F001", "F002", "F003"], "relationships": {},
    }


class TestTypAndMissingDimension:
    def test_typ_fills_sibling_fillet(self):
        res = resolve_extraction(_two_fillets(second_has_radius=False))
        # F003 gained an inferred fillet_radius dimension equal to the TYP value.
        f003 = next(f for f in res.resolved_extraction["features"] if f["id"] == "F003")
        rel_dims = [d for d in res.resolved_extraction["dimensions"] if d["id"] in f003["related_dimensions"]]
        inferred = [d for d in rel_dims if d.get("assumption_basis") == "typ_propagation"]
        assert inferred and abs(inferred[0]["value"] - 0.531) < 1e-6
        # No dimensionless flag for F003 now that it inherited the TYP radius.
        assert not [f for f in res.flags if f.get("source") == "missing_dimension" and f["dimension_id"] == "F003"]

    def test_own_radius_not_overridden_by_typ(self):
        res = resolve_extraction(_two_fillets(second_has_radius=True))
        f003 = next(f for f in res.resolved_extraction["features"] if f["id"] == "F003")
        assert not [d for d in res.resolved_extraction["dimensions"]
                    if d["id"] in f003["related_dimensions"] and d.get("assumption_basis") == "typ_propagation"]

    def test_dimensionless_chamfer_flagged_for_markup(self):
        d = _two_fillets(second_has_radius=False)
        # Add a chamfer with no distance and no TYP chamfer value anywhere.
        d["features"].append({"id": "F004", "type": "chamfer", "description": "edge chamfer",
                              "related_dimensions": [], "parent_feature": "F001"})
        d["build_order"].append("F004")
        res = resolve_extraction(d)
        miss = [f for f in res.flags if f.get("source") == "missing_dimension" and f["dimension_id"] == "F004"]
        assert miss and miss[0]["flag_tier"] == "CRITICAL" and miss[0].get("route_to_markup") is True


# --------------------------------------------------------------------------- #
# Fix 1.3 / 4.2 — learning-loop fingerprint + rapid-rerun detection
# --------------------------------------------------------------------------- #
class TestLearningLoopFingerprint:
    def test_fingerprint_stable_and_number_invariant(self):
        from pipeline.learning_loop import _failure_fingerprint

        a = _failure_fingerprint(["MM-001 FAILED: required 6, measured 5"], [], [])
        b = _failure_fingerprint(["MM-001 FAILED: required 8, measured 7"], [], [])  # only numbers differ
        c = _failure_fingerprint(["totally different reason"], [], [])
        assert a and a == b        # number jitter does not change the fingerprint
        assert a != c
        assert _failure_fingerprint([], [], []) == ""   # clean run -> no fingerprint

    def test_rapid_rerun_marked(self, tmp_path, monkeypatch):
        # write_learning_log skips under pytest by design; drop the guard for this
        # test and use a tmp output dir so nothing touches the real repo.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        from pipeline import learning_loop

        out = tmp_path / "output"
        (out / "P").mkdir(parents=True)
        gate = ["feature F005 FAILED: chamfer F005 has no distance dimension"]
        p1 = learning_loop.write_learning_log(out / "P", "A001821M", "NOT READY", gate, out)
        p2 = learning_loop.write_learning_log(out / "P", "A001821M", "NOT READY", gate, out)
        assert p1 and p2 and p1 != p2
        body2 = p2.read_text(encoding="utf-8")
        assert "RERUN" in body2 and "Fingerprint:" in body2
        # The index notes the rerun.
        idx = (tmp_path / "Learning Loop" / "INDEX.md")
        assert idx.is_file() and "RERUN" in idx.read_text(encoding="utf-8")
