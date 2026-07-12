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

    def test_dimensionless_chamfer_flagged_for_markup_commit_off(self):
        d = _two_fillets(second_has_radius=False)
        # Add a chamfer with no distance and no TYP chamfer value anywhere.
        d["features"].append({"id": "F004", "type": "chamfer", "description": "edge chamfer",
                              "related_dimensions": [], "parent_feature": "F001"})
        d["build_order"].append("F004")
        res = resolve_extraction(d, commit_mode=False)
        miss = [f for f in res.flags if f.get("source") == "missing_dimension" and f["dimension_id"] == "F004"]
        assert miss and miss[0]["flag_tier"] == "CRITICAL" and miss[0].get("route_to_markup") is True

    def test_dimensionless_chamfer_committed_in_commit_mode(self):
        # Commit-to-extraction (default, 2026-07-12 Task 3 coverage sweep): a
        # chamfer with no distance anywhere BUILDS at a conservative shop-typical
        # value rather than being excluded.
        d = _two_fillets(second_has_radius=False)
        d["features"].append({"id": "F004", "type": "chamfer", "description": "edge chamfer",
                              "related_dimensions": [], "parent_feature": "F001"})
        d["build_order"].append("F004")
        res = resolve_extraction(d)
        assert "F004" in res.resolved_extraction["build_order"]


# --------------------------------------------------------------------------- #
# Fix 1.3 / 4.2 — learning-loop fingerprint + rapid-rerun detection
# --------------------------------------------------------------------------- #
class TestPositionDemotion:
    """Fix 3.1 — undimensioned feature with no symmetry evidence routes to review."""
    def _hole_no_position(self, symmetric: bool = False) -> dict:
        d = _extraction_with_holes()
        d["dimensions"] = [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "linear", "value": 0.5, "unit": "inch", "applies_to": "thickness"},
        ]
        d["confidence"] = 0.9
        d["hole_callouts"] = [{"id": "H1", "type": "thru", "diameter": 0.25, "qty": 1}]
        # F002 has a diameter (so the ONLY thing missing is its position) — isolates
        # the P3 position-exclusion path from the P1 missing-driving-dim path.
        d["dimensions"].append(
            {"id": "D050", "type": "diameter", "value": 0.25, "unit": "inch", "applies_to": "hole_diameter"})
        d["features"] = [
            {"id": "F001", "type": "extrude_boss", "description": "plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003", "position_known": True},
            {"id": "F002", "type": "hole", "description": "a hole", "parent_feature": "F001",
             "related_dimensions": ["D050"]},
        ]
        d["build_order"] = ["F001", "F002"]
        d["relationships"] = ({"symmetry": [{"plane": "Front", "feature_ids": ["F002"]}]}
                              if symmetric else {})
        return d

    def test_no_symmetry_is_excluded_not_center_guessed_commit_off(self):
        # Legacy comparison path (commit_mode=False): position-unresolved cut/hole
        # is EXCLUDED (no parent-center guess), surfaced as a Tab-3 assumption.
        res = resolve_extraction(self._hole_no_position(symmetric=False), commit_mode=False)
        fres = res.feature_resolutions["F002"]
        assert fres.position_assumption == "needs_markup_review"
        assert fres.build_status == "excluded"
        assert "F002" not in res.resolved_extraction["build_order"]
        flag = next(f for f in res.flags if f["dimension_id"] == "F002")
        assert flag.get("source") == "position_unresolved"
        assert flag.get("excluded_from_build") is True
        assert flag["flag_tier"] == "CRITICAL"
        assert "Tab-1" not in flag["human_note"]

    def test_no_symmetry_is_committed_and_built_in_commit_mode(self):
        # Commit-to-extraction (default): the same feature BUILDS at a conservative
        # placement (never excluded, never [0,0]), flagged CRITICAL.
        res = resolve_extraction(self._hole_no_position(symmetric=False))
        fres = res.feature_resolutions["F002"]
        assert fres.position_assumption == "committed_conservative"
        assert "F002" in res.resolved_extraction["build_order"]
        assert "COMMITTED" in fres.human_note

    def test_symmetric_feature_stays_centered_low(self):
        res = resolve_extraction(self._hole_no_position(symmetric=True))
        fres = res.feature_resolutions["F002"]
        assert fres.position_assumption == "centered_on_parent" and fres.flag_tier == "LOW"


class TestDrillSizeAndIllegible:
    """Fix 3.2 — drill-size plausibility + illegible-diameter routing."""
    def test_drill_table(self):
        from pipeline.drill_sizes import is_standard_drill, nearest_drill

        assert is_standard_drill(0.218)      # #2 letter/number drill
        assert is_standard_drill(0.250)      # 1/4"
        assert not is_standard_drill(0.425)  # in a gap between Z (.413) and 7/16 (.4375)
        assert nearest_drill(0.2185)[1] < 0.002

    def test_illegible_nonstandard_diameter_routed(self):
        d = _extraction_with_holes()
        d["confidence"] = 0.9
        d["dimensions"] = [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "linear", "value": 0.5, "unit": "inch", "applies_to": "thickness"},
            # illegible, non-standard diameter with only one candidate -> CRITICAL unverifiable
            {"id": "D009", "type": "diameter", "value": 0.400, "unit": "inch",
             "applies_to": "hole_diameter", "value_unclear": True, "resolution_required": True,
             "ambiguity_reason": "degraded handwriting"},
        ]
        d["features"] = [{"id": "F001", "type": "extrude_boss", "description": "plate",
                          "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
                          "position_known": True}]
        d["build_order"] = ["F001"]
        res = resolve_extraction(d)
        flag = [f for f in res.flags if f.get("source") == "illegible_dimension"]
        assert flag and flag[0]["route_to_markup"] is True and flag[0]["dimension_id"] == "D009"

    def test_illegible_standard_diameter_not_routed(self):
        d = _extraction_with_holes()
        d["confidence"] = 0.9
        d["dimensions"] = [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "linear", "value": 0.5, "unit": "inch", "applies_to": "thickness"},
            {"id": "D009", "type": "diameter", "value": 0.218, "unit": "inch",
             "applies_to": "hole_diameter", "value_unclear": True, "resolution_required": True,
             "ambiguity_reason": "degraded handwriting"},
        ]
        d["features"] = [{"id": "F001", "type": "extrude_boss", "description": "plate",
                          "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
                          "position_known": True}]
        d["build_order"] = ["F001"]
        res = resolve_extraction(d)
        assert not [f for f in res.flags if f.get("source") == "illegible_dimension"]


class TestIncompleteProfile:
    """Fix 2.4 — extrude cut with no diameter and not both sides.

    Legacy (commit_mode=False): routed to markup review. Commit-mode (default):
    the rectangle is derived from the outer profile (profile_delta) and built."""
    def _height_only_cut(self):
        d = _extraction_with_holes()
        d["confidence"] = 0.9
        d["dimensions"] = [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "linear", "value": 0.5, "unit": "inch", "applies_to": "thickness"},
            {"id": "D010", "type": "linear", "value": 0.75, "unit": "inch", "applies_to": "height"},
        ]
        d["features"] = [
            {"id": "F001", "type": "extrude_boss", "description": "plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003", "position_known": True},
            {"id": "F004", "type": "extrude_cut", "description": "notch",
             "related_dimensions": ["D010"], "parent_feature": "F001", "position_known": True},
        ]
        d["build_order"] = ["F001", "F004"]
        return d

    def test_height_only_cut_routed_commit_off(self):
        res = resolve_extraction(self._height_only_cut(), commit_mode=False)
        flag = [f for f in res.flags if f.get("source") == "incomplete_profile" and f["dimension_id"] == "F004"]
        assert flag and flag[0]["route_to_markup"] is True
        assert "F004" not in res.resolved_extraction["build_order"]

    def test_height_only_cut_built_via_profile_delta_in_commit_mode(self):
        res = resolve_extraction(self._height_only_cut())
        assert "F004" in res.resolved_extraction["build_order"]
        # width + length were derived from the outer profile envelope
        f004 = next(f for f in res.resolved_extraction["features"] if f["id"] == "F004")
        dims = {d["id"]: d for d in res.resolved_extraction["dimensions"]}
        derived = [dims[r] for r in f004["related_dimensions"]
                   if dims.get(r, {}).get("assumption_basis") == "profile_delta"]
        assert derived, "expected profile_delta-derived dimensions on F004"


class TestOverviewNonFeatureTaxonomy:
    """Fix 4.1 — thickness/finish/hardware/reference reconcile instead of noise."""
    def test_thickness_view_matching_extrude_depth_no_flag(self):
        extraction = {"dimensions": [{"applies_to": "thickness", "value": 0.105}],
                      "hole_callouts": [], "features": []}
        overview = {"features": [{"kind": "other", "count": 1,
                                  "description": "side view showing .105 thickness"}]}
        items = cross_check(overview, extraction)
        assert not items  # reconciles cleanly, no noise

    def test_thickness_view_mismatch_flagged(self):
        extraction = {"dimensions": [{"applies_to": "thickness", "value": 0.25}],
                      "hole_callouts": [], "features": []}
        overview = {"features": [{"kind": "other", "count": 1,
                                  "description": "edge view .105 thick"}]}
        items = cross_check(overview, extraction)
        assert any("thickness mismatch" in i["what"].lower() for i in items)

    def test_finish_note_downgraded_to_metadata(self):
        extraction = {"dimensions": [], "hole_callouts": [], "features": []}
        overview = {"features": [{"kind": "other", "count": 1, "description": "CFS finish all over"}]}
        items = cross_check(overview, extraction)
        assert items and items[0]["severity"] == "LOW" and "finish" in items[0]["affects"].lower()


class TestCutIntersectionSanity:
    """Fix 1.2c — the pure XY-overlap helper behind the off-solid cut reclassify
    (verified live against SolidWorks: an off-solid hole raises 'positioned
    OUTSIDE the solid' instead of a bare FeatureCut4 None)."""
    def test_bbox_overlap(self):
        from pipeline.solidworks_builder import _bboxes_overlap_xy

        body = (0.0, 0.0, 0.1016, 0.0762)      # 4x3 in, meters
        inside = (0.049, 0.036, 0.055, 0.042)  # a hole near center
        outside = (0.25, 0.25, 0.26, 0.26)     # hole at (10,10) in
        assert _bboxes_overlap_xy(inside, body)
        assert not _bboxes_overlap_xy(outside, body)
        # touching edges count as overlap (tol)
        assert _bboxes_overlap_xy((0.1016, 0.0, 0.11, 0.01), body)


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
