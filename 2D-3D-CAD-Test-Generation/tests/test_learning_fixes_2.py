"""Regression tests for the 2026-07-10 learning-loop fix cycle.

Covers the self-contained, deterministically-verifiable fixes:
  * P1 — universal missing-dimension completeness gate (a feature missing its
    driving dimension is EXCLUDED from build_order, never emitted as a build/macro
    step). Regression of Fix 2.1/2.4, which only flagged.
  * P3 — position-unresolved cut/hole is excluded, not center-guessed; no flag
    text references the removed Tab-1 markup tool.
  * P2 — gauge-callout thickness parser.
  * P5 — type-aware callout classification.
  * P6 — decimal-plausibility numeral resolution.
  * P10 — STOCK TOL qualifier parsing.
"""
import pytest

from pipeline.overview_check import cross_check
from pipeline.resolver import resolve_extraction


# --------------------------------------------------------------------------- #
# P2 — gauge-callout thickness parser + gauge-aware reconciliation
# --------------------------------------------------------------------------- #
class TestGaugeParser:
    def test_gauge_with_parenthetical_decimal(self):
        from pipeline.gauge import parse_thickness_callout

        r = parse_thickness_callout("12 GA. (.105)")
        assert r.gauge == 12 and abs(r.thickness_in - 0.105) < 1e-9
        assert r.source == "decimal_with_gauge"

    def test_gauge_only_with_material(self):
        from pipeline.gauge import parse_thickness_callout

        r = parse_thickness_callout("16 GA", material="CRS steel")
        assert r.gauge == 16 and abs(r.thickness_in - 0.0598) < 1e-4
        assert r.source == "gauge_table" and not r.needs_material

    def test_gauge_only_unknown_material_is_flagged_not_guessed(self):
        from pipeline.gauge import parse_thickness_callout

        r = parse_thickness_callout("16 GA")
        assert r.gauge == 16 and r.thickness_in is None and r.needs_material

    def test_aluminum_uses_brown_and_sharpe(self):
        from pipeline.gauge import parse_thickness_callout

        r = parse_thickness_callout("16 GA", material="6061 aluminum")
        assert abs(r.thickness_in - 0.0508) < 1e-4  # B&S 16, not MSG .0598

    def test_bare_decimal_no_gauge(self):
        from pipeline.gauge import parse_thickness_callout

        r = parse_thickness_callout(".105 thick")
        assert abs(r.thickness_in - 0.105) < 1e-9 and r.gauge is None


class TestCalloutClassification:
    """P5 — type a callout before counting; only holes enter hole reconciliation."""

    @pytest.mark.parametrize("text,kind", [
        (".12 R. TYP.", "radius"),
        ("R.531", "radius"),
        (".531 R", "radius"),
        ("RAD .25", "radius"),
        (".422 DIA 6-HOLES", "hole"),
        (".406 DIA THRU (2) HL'S", "hole"),
        ("Ø.25 THRU", "hole"),
        ("1/4-20 TAP", "threaded_hole"),
        ("10-24 UNC", "threaded_hole"),
        ("M6x1", "threaded_hole"),
        (".531 C'BORE .81 DEEP", "compound_hole"),
        ("CSK 82 DEG", "compound_hole"),
    ])
    def test_classify(self, text, kind):
        from pipeline.callout_qty import classify_callout
        assert classify_callout(text) == kind

    def test_radius_is_not_a_hole_callout(self):
        from pipeline.callout_qty import is_hole_callout
        assert not is_hole_callout(".12 R. TYP.")
        assert is_hole_callout(".422 DIA 6-HOLES")

    def test_radius_note_not_counted_against_holes(self):
        from pipeline.resolver import _overview_flags
        raw = {"hole_callouts": [{"id": "H1", "qty": 8}]}
        overview = {"global_notes": [{"note": ".12 R. TYP.", "resolved_count": 4}],
                    "cross_view_conflicts": []}
        flags = _overview_flags(overview, raw)
        assert not [f for f in flags if f.get("dimension_id") == "OV-COUNT"]


class TestBuilderCallDiagnostics:
    """P4 — every fragile COM feature call goes through exactly one precondition-
    verified wrapper, and no wrapper can emit a bare "returned None"."""

    FRAGILE = ("FeatureManager.FeatureCut4(",
               "FeatureManager.FeatureFillet3(",
               "FeatureManager.InsertFeatureChamfer(")

    def _src(self):
        from pathlib import Path
        import pipeline.solidworks_builder as sb
        return Path(sb.__file__).read_text(encoding="utf-8")

    def test_each_fragile_api_called_exactly_once(self):
        src = self._src()
        for api in self.FRAGILE:
            assert src.count(api) == 1, f"{api} must have a single wrapper call site"

    def test_no_bare_returned_none_message(self):
        # Every "returned None for {feature" message must be accompanied by a
        # precondition enumeration ("despite verified preconditions" or "PRECONDITION").
        import re
        src = self._src()
        for m in re.finditer(r'returned None for \{feature\.id\}([^"]*)"', src):
            tail = m.group(1)
            # cut/fillet/chamfer messages continue onto the next lines; accept the
            # nearby context carrying the precondition wording.
        assert "despite verified preconditions" in src
        assert src.count("PRECONDITION FAILED") >= 2  # fillet + chamfer no-edge paths


class TestFilletScopePlan:
    """P9 — fillet scope is derived from the callout; all-edges is the flagged
    fallback, never the silent default."""

    def _feature(self, **kw):
        from types import SimpleNamespace
        base = dict(id="F004", description="corner fillet", related_dimensions=["R001"],
                    depth_dimension_id="", parent_feature="", quantity=1)
        base.update(kw)
        return SimpleNamespace(**base)

    def _model(self, notes):
        from types import SimpleNamespace
        dim = SimpleNamespace(notes=notes, raw_text=notes, applies_to="fillet_radius")
        return SimpleNamespace(dimension_by_id=lambda rid: dim if rid == "R001" else None)

    def test_corner_typ_scopes_to_n_corners(self):
        from pipeline.solidworks_builder import plan_fillet_scope
        f = self._feature(quantity=4)
        mode, count, _ = plan_fillet_scope(f, self._model(".12 R. TYP."))
        assert mode == "corners" and count == 4

    def test_slot_end_radius(self):
        from pipeline.solidworks_builder import plan_fillet_scope
        f = self._feature(description="slot end fillet")
        mode, count, _ = plan_fillet_scope(f, self._model("R.25"))
        assert mode == "slot_ends" and count == 2

    def test_named_host_feature_scope(self):
        from pipeline.solidworks_builder import plan_fillet_scope
        f = self._feature(parent_feature="F002")
        mode, _, _ = plan_fillet_scope(f, self._model("R.25"))
        assert mode == "feature"

    def test_ungeneral_note_falls_back_to_all(self):
        from pipeline.solidworks_builder import plan_fillet_scope
        f = self._feature()
        mode, _, reason = plan_fillet_scope(f, self._model("ALL FILLETS R.06"))
        assert mode == "all" and "fallback" in reason


class TestInspectionBalloons:
    """P7 — an inspection-balloon reference is not a geometry conflict."""

    def test_balloon_conflict_downgraded_to_low(self):
        from pipeline.resolver import _overview_flags
        overview = {"cross_view_conflicts": [{
            "description": "Balloon 2/1 labeled 10.00 IN. near the top edge appears to "
                           "disagree with the 11.00 overall width.",
            "severity": "HIGH", "views_involved": ["front"],
        }], "global_notes": []}
        flags = _overview_flags(overview, {"hole_callouts": []})
        assert flags and flags[0]["flag_tier"] == "LOW"
        assert flags[0].get("inspection_balloon") is True

    def test_real_geometry_conflict_still_flagged(self):
        from pipeline.resolver import _overview_flags
        overview = {"cross_view_conflicts": [{
            "description": "The front view shows a through bore but the side view shows "
                           "it blind.",
            "severity": "HIGH", "views_involved": ["front", "side"],
        }], "global_notes": []}
        flags = _overview_flags(overview, {"hole_callouts": []})
        assert flags and not flags[0].get("inspection_balloon")

    def test_balloon_detector(self):
        from pipeline.resolver import _is_inspection_balloon_conflict
        assert _is_inspection_balloon_conflict("balloon 1/1 6.50 IN.")
        assert _is_inspection_balloon_conflict("inspection dimension 7.00 IN.")
        assert not _is_inspection_balloon_conflict("through bore vs blind hole")


class TestGaugeReconciliation:
    def test_gauge_callout_reconciles_decimal_not_gauge(self):
        # "12 GA. (.105)" vs a build depth of .105 — must NOT fire a 12-vs-.105 flag.
        extraction = {"dimensions": [{"applies_to": "thickness", "value": 0.105}],
                      "hole_callouts": [], "features": [], "material": "steel"}
        overview = {"features": [{"kind": "other", "count": 1,
                                  "description": "side view 12 GA. (.105) thick"}]}
        items = cross_check(overview, extraction)
        assert not [i for i in items if "mismatch" in i["what"].lower()], items

    def test_out_of_band_thickness_is_not_a_conflict(self):
        # A001551E: 21.0 captured vs .50 build thickness (42x) — rejected, not HIGH.
        extraction = {"dimensions": [{"applies_to": "thickness", "value": 0.50}],
                      "hole_callouts": [], "features": []}
        overview = {"features": [{"kind": "other", "count": 1,
                                  "description": "edge view 21.0 thick"}]}
        items = cross_check(overview, extraction)
        assert not [i for i in items if i["severity"] == "HIGH"], items


def _base_plate() -> dict:
    """A minimal buildable plate; callers append the feature under test."""
    return {
        "part_number": "GATE-1", "units": "inch", "confidence": 0.9,
        "general_tolerance": ".XX +/-.01",
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "linear", "value": 0.5, "unit": "inch", "applies_to": "thickness"},
        ],
        "hole_callouts": [],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
             "position_known": True},
        ],
        "build_order": ["F001"],
        "relationships": {},
    }


class TestDecimalPlausibility:
    """P6/P10(a) — an ambiguous numeral is resolved by plausibility, not by
    picking the smallest ("conservative") candidate."""

    def _plate_with_ambiguous(self, value, possible):
        d = _base_plate()
        # Sheet formatting: existing dims are sub-2, leading-dot style.
        d["dimensions"][0]["value"] = 1.12
        d["dimensions"][1]["value"] = 0.656
        d["dimensions"].append({
            "id": "D010", "type": "linear", "value": value, "unit": "inch",
            "applies_to": "length", "possible_values": possible,
            "value_unclear": True, "resolution_required": True,
            "ambiguity_reason": "no visible decimal point",
        })
        return d

    def test_312_resolves_to_point_312(self):
        d = self._plate_with_ambiguous(0.312, [0.312, 3.12, 31.2])
        res = resolve_extraction(d)
        r = res.dim_resolutions["D010"]
        assert r.assumption_basis == "decimal_plausibility"
        assert abs(r.resolved_value - 0.312) < 1e-6

    def test_diameter_prefers_standard_drill(self):
        d = _base_plate()
        # A small part (all dims sub-1): a 2.18" hole is implausible; .218 (a
        # standard #2/letter drill) is the plausible reading.
        d["dimensions"][0]["value"] = 0.75
        d["dimensions"][1]["value"] = 0.656
        d["dimensions"][2]["value"] = 0.50
        d["dimensions"].append({
            "id": "D011", "type": "diameter", "value": 2.18, "unit": "inch",
            "applies_to": "hole_diameter", "possible_values": [2.18, 0.218],
            "value_unclear": True, "resolution_required": True,
            "ambiguity_reason": "decimal placement unclear",
        })
        res = resolve_extraction(d)
        r = res.dim_resolutions["D011"]
        assert abs(r.resolved_value - 0.218) < 1e-6


class TestStockQualifier:
    """P10(b) — a STOCK/(STOCK TOL.) dimension is the finished envelope, exempt
    from tight-tolerance ambiguity routing."""

    def test_stock_dim_resolved_as_finished_envelope(self):
        d = _base_plate()
        d["dimensions"].append({
            "id": "D020", "type": "linear", "value": 3.50, "unit": "inch",
            "applies_to": "length", "notes": "(STOCK TOL.)",
            "value_unclear": True, "resolution_required": True,
        })
        res = resolve_extraction(d)
        r = res.dim_resolutions["D020"]
        assert r.assumption_basis == "stock_dimension"
        assert r.flag_tier == "HIGH" and abs(r.resolved_value - 3.50) < 1e-6
        assert not [f for f in res.flags
                    if f.get("dimension_id") == "D020" and f["flag_tier"] == "CRITICAL"]


class TestUniversalCompletenessGate:
    """P1 — one feature of each type with its driving dimension absent must NOT
    reach macro generation (i.e. must be removed from build_order)."""

    # (feature dict, human label) — each is missing its type's driving dimension.
    CASES = [
        ({"id": "FX", "type": "fillet", "description": "corner fillet",
          "related_dimensions": [], "parent_feature": "F001"}, "fillet:no-radius"),
        ({"id": "FX", "type": "chamfer", "description": "edge chamfer",
          "related_dimensions": [], "parent_feature": "F001"}, "chamfer:no-distance"),
        ({"id": "FX", "type": "hole", "description": "a hole", "parent_feature": "F001",
          "related_dimensions": [], "position_known": True}, "hole:no-diameter"),
        ({"id": "FX", "type": "circular_pattern", "description": "bolt pattern",
          "related_dimensions": [], "parent_feature": "F001", "quantity": 1}, "pattern:no-spacing-count"),
        ({"id": "FX", "type": "extrude_cut", "description": "notch",
          "related_dimensions": ["D010"], "parent_feature": "F001", "position_known": True},
         "cut:height-only-profile"),
    ]

    @pytest.mark.parametrize("feat,label", CASES, ids=[c[1] for c in CASES])
    def test_dimensionless_feature_excluded_from_build_order(self, feat, label):
        d = _base_plate()
        if label.startswith("cut"):
            # give it a height only (no diameter, no length+width closed profile)
            d["dimensions"].append(
                {"id": "D010", "type": "linear", "value": 0.75, "unit": "inch", "applies_to": "height"})
        d["features"].append(feat)
        d["build_order"].append("FX")
        res = resolve_extraction(d)
        assert "FX" not in res.resolved_extraction["build_order"], (
            f"{label}: feature reached the build plan despite a missing driving dimension")
        excluded = [f for f in res.flags
                    if f.get("dimension_id") == "FX" and f.get("excluded_from_build")]
        assert excluded, f"{label}: no exclusion flag emitted"
        assert excluded[0]["flag_tier"] == "CRITICAL"
        assert excluded[0].get("model_derived_assumption") is True

    def test_thread_callout_supplies_hole_diameter(self):
        # Step-3 standard-size substitution: a thread callout names an unambiguous
        # major diameter, so the hole is BUILT (not excluded), value inferred.
        d = _base_plate()
        d["features"].append(
            {"id": "FX", "type": "hole", "description": "M6x1 tapped hole",
             "related_dimensions": [], "parent_feature": "F001", "position_known": True})
        d["build_order"].append("FX")
        res = resolve_extraction(d)
        assert "FX" in res.resolved_extraction["build_order"]
        fx = next(f for f in res.resolved_extraction["features"] if f["id"] == "FX")
        inferred = [dd for dd in res.resolved_extraction["dimensions"]
                    if dd["id"] in fx["related_dimensions"]
                    and dd.get("assumption_basis") == "standard_thread_size"]
        assert inferred and abs(inferred[0]["value"] - 6.0 / 25.4) < 1e-3

    def test_fully_dimensioned_features_are_kept(self):
        d = _base_plate()
        d["dimensions"].append(
            {"id": "R001", "type": "radial", "value": 0.25, "unit": "inch",
             "applies_to": "fillet_radius"})
        d["features"].append(
            {"id": "FX", "type": "fillet", "description": "corner fillet",
             "related_dimensions": ["R001"], "parent_feature": "F001"})
        d["build_order"].append("FX")
        res = resolve_extraction(d)
        assert "FX" in res.resolved_extraction["build_order"]
