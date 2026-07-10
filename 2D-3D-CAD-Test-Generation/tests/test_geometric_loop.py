"""Tests for Phase B — the geometric build->measure->correct->rebuild loop
(pipeline.reconciliation). Loop behavior is exercised with injected build/verify
functions so no SolidWorks or STL is needed; the correction policy and
systematic-transform detection are pure and tested directly.
"""
from pathlib import Path

from pipeline.reconciliation import (
    classify_transform,
    geometric_correction_loop,
    plan_corrections,
    _apply_transform_to_plan,
)


def _plan(positions):
    """A build plan with one base + one hole step carrying the given positions."""
    return {
        "units": "inch", "unit_factor_to_meters": 0.0254,
        "steps": [
            {"seq": 1, "type": "extrude_boss", "feature_id": "F001",
             "dimensions_drawing_units": {"length": 4.0, "width": 2.0, "depth": 0.25},
             "positions_xy": [[0.0, 0.0]]},
            {"seq": 2, "type": "hole", "feature_id": "F002",
             "dimensions_drawing_units": {"diameter": 0.25}, "positions_xy": positions,
             "depth_type": "through_all"},
        ],
    }


# --------------------------------------------------------------------------- #
# classify_transform (pure)
# --------------------------------------------------------------------------- #
class TestClassifyTransform:
    def test_origin_offset(self):
        mis = [{"expected": (1.0, 1.0), "measured": (1.3, 1.2)},
               {"expected": (3.0, 1.0), "measured": (3.3, 1.2)}]
        t = classify_transform(mis)
        assert t and t["kind"] == "origin_offset"
        assert abs(t["dx"] - 0.3) < 1e-6 and abs(t["dy"] - 0.2) < 1e-6

    def test_axis_swap(self):
        mis = [{"expected": (1.0, 4.0), "measured": (4.0, 1.0)},
               {"expected": (2.0, 3.0), "measured": (3.0, 2.0)}]
        t = classify_transform(mis)
        assert t and t["kind"] == "axis_swap"

    def test_uniform_scale(self):
        mis = [{"expected": (1.0, 1.0), "measured": (1.1, 1.1)},
               {"expected": (3.0, 2.0), "measured": (3.3, 2.2)}]
        t = classify_transform(mis)
        assert t and t["kind"] == "uniform_scale"
        assert abs(t["factor"] - 1.1) < 1e-3

    def test_one_off_is_not_systematic(self):
        # Two features with UNRELATED errors -> no systematic transform.
        mis = [{"expected": (1.0, 1.0), "measured": (1.3, 1.2)},
               {"expected": (3.0, 1.0), "measured": (2.1, 0.4)}]
        assert classify_transform(mis) is None

    def test_single_misplaced_never_systematic(self):
        assert classify_transform([{"expected": (1.0, 1.0), "measured": (1.3, 1.2)}]) is None


class TestApplyTransform:
    def test_origin_offset_precompensates(self):
        plan = _plan([[1.0, 1.0], [3.0, 1.0]])
        fixed, affected = _apply_transform_to_plan(plan, {"kind": "origin_offset", "dx": 0.3, "dy": 0.2})
        assert "F002" in affected
        # emitted positions shifted to CANCEL the +0.3/+0.2 the build introduced
        assert fixed["steps"][1]["positions_xy"] == [[0.7, 0.8], [2.7, 0.8]]
        # original plan untouched (deep copy)
        assert plan["steps"][1]["positions_xy"] == [[1.0, 1.0], [3.0, 1.0]]


# --------------------------------------------------------------------------- #
# plan_corrections (pure)
# --------------------------------------------------------------------------- #
class TestPlanCorrections:
    def test_systematic_misplaced_uses_transform_fix(self):
        verif = {"mismatches": [
            {"feature_id": "F002", "classification": "MISPLACED",
             "expected": {"x": 1.0, "y": 1.0}, "measured": {"x": 1.3, "y": 1.2}},
            {"feature_id": "F003", "classification": "MISPLACED",
             "expected": {"x": 3.0, "y": 1.0}, "measured": {"x": 3.3, "y": 1.2}},
        ], "extras": []}
        transform, corrs = plan_corrections(verif, _plan([[1.0, 1.0]]))
        assert transform and transform["kind"] == "origin_offset"
        assert all(c.action == "transform_fix" for c in corrs)

    def test_missing_is_flagged_not_fabricated(self):
        verif = {"mismatches": [{"feature_id": "F002", "classification": "MISSING",
                                 "expected": {"x": 1.0, "y": 1.0}, "measured": None}],
                 "extras": []}
        _, corrs = plan_corrections(verif, _plan([[1.0, 1.0]]))
        assert corrs[0].action == "flag"


# --------------------------------------------------------------------------- #
# The loop (injected build/verify)
# --------------------------------------------------------------------------- #
def _report(features_ok, mismatches=(), extras=()):
    feats = [{"feature_id": f, "classification": "OK"} for f in features_ok]
    feats += [{"feature_id": m["feature_id"], "classification": m["classification"]} for m in mismatches]
    return {"ok": not (mismatches or extras),
            "features": feats,
            "mismatches": list(mismatches),
            "extras": list(extras),
            "summary": {"ok": len(features_ok), "mismatches": len(mismatches)}}


class TestLoop:
    def test_all_pass_first_iteration(self, tmp_path):
        def build_fn(plan, part_dir, it):
            return tmp_path / f"m{it}.stl"

        def verify_fn(stl, plan, part_dir, **kw):
            return _report(["F001", "F002"])

        res = geometric_correction_loop(
            build_fn=build_fn, build_plan=_plan([[1.0, 1.0]]), part_dir=tmp_path,
            part="P", verify_fn=verify_fn)
        assert res.final_status == "READY"
        assert res.iterations_used == 1

    def test_systematic_offset_corrected_within_cap(self, tmp_path):
        # Iteration 1: hole is off by (0.3, 0.2) with two instances -> systematic.
        # After the loop pre-compensates the plan, iteration 2 verifies OK.
        def verify_fn(stl, plan, part_dir, **kw):
            pts = plan["steps"][1]["positions_xy"]
            corrected = pts[0][0] < 0.9  # plan shifted from [1.0,..] toward [0.7,..]
            if corrected:
                return _report(["F001", "F002", "F003"])
            return _report(["F001"], mismatches=[
                {"feature_id": "F002", "classification": "MISPLACED",
                 "expected": {"x": 1.0, "y": 1.0}, "measured": {"x": 1.3, "y": 1.2}},
                {"feature_id": "F003", "classification": "MISPLACED",
                 "expected": {"x": 3.0, "y": 1.0}, "measured": {"x": 3.3, "y": 1.2}},
            ])

        res = geometric_correction_loop(
            build_fn=lambda plan, pd, it: tmp_path / f"m{it}.stl",
            build_plan=_plan([[1.0, 1.0], [3.0, 1.0]]), part_dir=tmp_path,
            part="P", verify_fn=verify_fn)
        assert res.final_status == "READY", res.as_dict()
        assert res.iterations_used == 2
        assert res.transforms_applied and res.transforms_applied[0]["kind"] == "origin_offset"

    def test_oscillation_stops_immediately(self, tmp_path):
        # F002 passes iter1, a correction is applied for F003, then F002 regresses.
        state = {"it": 0}

        def verify_fn(stl, plan, part_dir, **kw):
            state["it"] += 1
            if state["it"] == 1:
                return _report(["F001", "F002"], mismatches=[
                    {"feature_id": "F003", "classification": "MISPLACED",
                     "expected": {"x": 3.0, "y": 1.0}, "measured": {"x": 3.3, "y": 1.2}},
                    {"feature_id": "F004", "classification": "MISPLACED",
                     "expected": {"x": 2.0, "y": 1.0}, "measured": {"x": 2.3, "y": 1.2}},
                ])
            # F002 (previously OK) now fails -> oscillation.
            return _report(["F001", "F003", "F004"], mismatches=[
                {"feature_id": "F002", "classification": "MISPLACED",
                 "expected": {"x": 1.0, "y": 1.0}, "measured": {"x": 0.6, "y": 0.6}}])

        res = geometric_correction_loop(
            build_fn=lambda plan, pd, it: tmp_path / f"m{it}.stl",
            build_plan=_plan([[1.0, 1.0]]), part_dir=tmp_path, part="P", verify_fn=verify_fn)
        assert res.final_status == "READY_WITH_OPEN_ITEMS"
        assert "oscillation" in res.stopped_reason

    def test_unfixable_missing_stops_no_progress(self, tmp_path):
        def verify_fn(stl, plan, part_dir, **kw):
            return _report(["F001"], mismatches=[
                {"feature_id": "F002", "classification": "MISSING",
                 "expected": {"x": 1.0, "y": 1.0}, "measured": None}])

        res = geometric_correction_loop(
            build_fn=lambda plan, pd, it: tmp_path / f"m{it}.stl",
            build_plan=_plan([[1.0, 1.0]]), part_dir=tmp_path, part="P", verify_fn=verify_fn)
        assert res.final_status == "READY_WITH_OPEN_ITEMS"
        assert res.iterations_used == 1  # stops at once; never fabricates
        assert "no applicable geometric correction" in res.stopped_reason
        assert res.unresolved


class TestReportWrite:
    def test_writes_geometric_loop_report(self, tmp_path):
        res = geometric_correction_loop(
            build_fn=lambda plan, pd, it: tmp_path / f"m{it}.stl",
            build_plan=_plan([[1.0, 1.0]]), part_dir=tmp_path, part="P",
            verify_fn=lambda *a, **k: _report(["F001", "F002"]))
        p = res.write(tmp_path, "P")
        assert p.is_file()
        import json
        d = json.loads(p.read_text())
        assert d["final_status"] == "READY" and "iteration_ledger" in d
