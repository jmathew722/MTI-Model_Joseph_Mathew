"""Dimensioning-architecture overhaul: position solver + anchor plumbing.

Covers the four golden dimensioning schemes (baseline plate, chain strip,
polar-BSC flywheel, datum-hole fixture plate), correction propagation
(movers), the vector cross-check math to 1e-6, anchor-fidelity verification
(the wrong-edge class), and the macro-generation integration (annotation
block + audit check + coordinate_frame header). The pure-coordinate case is
the degenerate base case — asserted by the untouched golden macro suite.
"""
import math

import pytest

from pipeline.macro_generator import generate_macro_package
from pipeline.macro_audit import check_anchor_annotations
from pipeline.position_solver import (
    anchors_for,
    canonical_frame,
    datum_pair_frame,
    fit_edge_line,
    kasa_circle_fit,
    measure_in_frame,
    movers,
    point_to_line_distance,
    solve_positions,
    verify_anchor_fidelity,
)
from pipeline.schema import DrawingData
from pipeline.validator import format_verification_report, run_verification
from tests.test_golden_macros import _golden_drawing


def _model(**overrides) -> DrawingData:
    base = {
        "part_number": "ANCHOR-TEST",
        "units": "inch",
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 11.0, "unit": "inch",
             "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 6.25, "unit": "inch",
             "applies_to": "width"},
        ],
        "features": [],
    }
    base.update(overrides)
    return DrawingData.model_validate(base)


def _feat(fid, ftype="hole", anchors=None, offset_x=0.0, offset_y=0.0):
    return {"id": fid, "type": ftype, "description": f"{fid} feature",
            "offset_x": offset_x, "offset_y": offset_y,
            "anchors": anchors or []}


def _anchor(scheme, ref, axis, value, dims=None, semantics="to_center"):
    return {"scheme": scheme, "anchor_ref": ref, "axis": axis, "value": value,
            "dimension_ids": dims or [], "semantics": semantics}


# --------------------------------------------------------------------------- #
# Baseline-dimensioned plate (A001581E style: everything from the edges)
# --------------------------------------------------------------------------- #
def test_baseline_plate_solves_and_traces():
    m = _model(features=[
        _feat("F002", anchors=[
            _anchor("baseline", "part_edge_left", "x", 1.56, ["D003"]),
            _anchor("baseline", "part_edge_bottom", "y", 2.0, ["D004"]),
        ]),
    ])
    sol = solve_positions(m)["F002"]
    assert (sol.x, sol.y) == (1.56, 2.0)
    assert sol.scheme == "baseline" and sol.grounded
    assert any("x = part_edge_left(0) + D003(1.56) [baseline]" == t for t in sol.trace)


def test_far_edge_baseline_measures_back_from_right_and_top():
    m = _model(features=[
        _feat("F002", anchors=[
            _anchor("baseline", "part_edge_right", "x", 1.5, ["D005"],
                    semantics="to_center"),
            _anchor("baseline", "part_edge_top", "y", 0.75, ["D006"]),
        ]),
    ])
    sol = solve_positions(m)["F002"]
    assert sol.x == pytest.approx(11.0 - 1.5)
    assert sol.y == pytest.approx(6.25 - 0.75)
    assert any("part_edge_right" in t and "-" in t for t in sol.trace)


# --------------------------------------------------------------------------- #
# Chain-dimensioned strip: accumulation + correction propagation
# --------------------------------------------------------------------------- #
def _chain_model():
    return _model(features=[
        _feat("F002", anchors=[
            _anchor("baseline", "part_edge_left", "x", 1.0, ["D003"]),
            _anchor("baseline", "part_edge_bottom", "y", 3.0, ["D004"]),
        ]),
        _feat("F003", anchors=[
            _anchor("chain", "F002", "x", 2.75, ["D005"]),
            _anchor("baseline", "part_edge_bottom", "y", 3.0, ["D004"]),
        ]),
        _feat("F004", anchors=[
            _anchor("chain", "F003", "x", 2.75, ["D006"]),
            _anchor("baseline", "part_edge_bottom", "y", 3.0, ["D004"]),
        ]),
    ])


def test_chain_accumulates_in_drawing_order():
    sols = solve_positions(_chain_model())
    assert sols["F002"].x == pytest.approx(1.0)
    assert sols["F003"].x == pytest.approx(3.75)
    assert sols["F004"].x == pytest.approx(6.50)
    assert sols["F004"].scheme == "chain"
    assert any("F003(3.75) + D006(2.75) [chain]" in t for t in sols["F004"].trace)


def test_correction_moves_downstream_chain_only():
    """Change D005 (the F002→F003 link): F003 and F004 move; F002 does not.
    Change D003 (the baseline start of the chain): everything chained moves."""
    m = _chain_model()
    assert movers(m, {"D005"}) == {"F003", "F004"}
    assert movers(m, {"D003"}) == {"F002", "F003", "F004"}
    assert movers(m, {"D004"}) == {"F002", "F003", "F004"}  # shared y baseline
    assert movers(m, {"D999"}) == set()


def test_correction_propagation_resolves_new_values():
    """The re-solve after a correction lands every mover at its new place."""
    m = _chain_model()
    before = solve_positions(m)
    # Correct D005: 2.75 → 3.05 (the first chain link).
    m.features[1].anchors[0].value = 3.05
    after = solve_positions(m)
    assert after["F002"].x == before["F002"].x            # not a mover
    assert after["F003"].x == pytest.approx(4.05)          # direct mover
    assert after["F004"].x == pytest.approx(6.80)          # transitive mover


# --------------------------------------------------------------------------- #
# Polar / bolt-circle (the 164-C flywheel class)
# --------------------------------------------------------------------------- #
def test_polar_bsc_solves_radius_and_angle_from_part_center():
    m = _model(features=[
        _feat("F002", anchors=[
            _anchor("polar_bsc", "part_center", "radial", 4.0, ["D010"]),
            _anchor("polar_bsc", "part_center", "angular", 90.0, ["D011"]),
        ]),
    ])
    sol = solve_positions(m)["F002"]
    assert sol.x == pytest.approx(11.0 / 2.0)
    assert sol.y == pytest.approx(6.25 / 2.0 + 4.0)
    assert sol.scheme == "polar_bsc"
    assert any("polar_bsc" in t and "90" in t for t in sol.trace)


def test_polar_center_may_be_another_feature():
    m = _model(features=[
        _feat("F001", "extrude_boss", anchors=[
            _anchor("coordinate", "origin", "x", 5.0),
            _anchor("coordinate", "origin", "y", 5.0),
        ]),
        _feat("F002", anchors=[
            _anchor("polar_bsc", "F001_center", "radial", 2.0, ["D012"]),
            _anchor("polar_bsc", "F001_center", "angular", 0.0),
        ]),
    ])
    sol = solve_positions(m)["F002"]
    assert (sol.x, sol.y) == (pytest.approx(7.0), pytest.approx(5.0))


# --------------------------------------------------------------------------- #
# Datum-hole fixture plate
# --------------------------------------------------------------------------- #
def _datum_model():
    return _model(
        hole_callouts=[
            {"id": "H001", "type": "thru", "diameter": 0.25, "qty": 1,
             "is_datum_hole": True, "position_known": True,
             "x_position": 2.0, "y_position": 2.0},
            {"id": "H002", "type": "thru", "diameter": 0.25, "qty": 1,
             "is_datum_hole": True, "position_known": True,
             "x_position": 9.0, "y_position": 2.0},
        ],
        features=[
            _feat("F003", anchors=[
                _anchor("datum_frame", "DATUM_HOLE_1", "x", 3.5, ["D020"],
                        semantics="true_position"),
                _anchor("datum_frame", "DATUM_HOLE_1", "y", 1.25, ["D021"],
                        semantics="true_position"),
            ]),
        ])


def test_datum_hole_frame_selected_and_solved():
    m = _datum_model()
    frame = canonical_frame(m)
    assert frame["frame"] == "datum_hole_pair"
    assert frame["ground"] == ["H001", "H002"]
    sol = solve_positions(m)["F003"]
    assert (sol.x, sol.y) == (pytest.approx(5.5), pytest.approx(3.25))
    assert sol.scheme == "datum_frame"


def test_declared_origin_beats_default_corner():
    m = _model(dimension_origin="ordinate zero at lower-left corner")
    assert canonical_frame(m)["frame"] == "declared_origin"
    assert canonical_frame(_model())["frame"] == "lower_left_corner"


# --------------------------------------------------------------------------- #
# Degenerate case + failure behavior (never block)
# --------------------------------------------------------------------------- #
def test_anchorless_feature_wraps_offsets_as_coordinate_scheme():
    m = _model(features=[_feat("F002", offset_x=4.5, offset_y=1.0)])
    ans = anchors_for(m.features[0])
    assert [a.scheme for a in ans] == ["coordinate", "coordinate"]
    sol = solve_positions(m)["F002"]
    assert (sol.x, sol.y) == (4.5, 1.0)
    assert sol.grounded


def test_anchor_cycle_falls_back_to_offsets_flagged():
    m = _model(features=[
        _feat("F002", offset_x=1.0, anchors=[_anchor("chain", "F003", "x", 1.0)]),
        _feat("F003", offset_x=2.0, anchors=[_anchor("chain", "F002", "x", 1.0)]),
    ])
    sols = solve_positions(m)
    assert not sols["F002"].grounded and not sols["F003"].grounded
    assert sols["F002"].x == 1.0  # stored offset, never a block
    assert any("UNRESOLVED" in t for t in sols["F002"].trace)


# --------------------------------------------------------------------------- #
# Vector cross-check math (synthetic DXF → 1e-6 agreement)
# --------------------------------------------------------------------------- #
def test_edge_fit_and_perpendicular_distance_exact():
    # Near-vertical left edge (x=0): TLS must not degenerate.
    pts = [(0.0, y / 10.0) for y in range(11)]
    c, d = fit_edge_line(pts)
    assert point_to_line_distance((1.56, 3.0), c, d) == pytest.approx(1.56, abs=1e-9)


def test_kasa_circle_fit_exact_on_true_circle():
    center, r = (3.25, 2.5), 1.75
    pts = [(center[0] + r * math.cos(t), center[1] + r * math.sin(t))
           for t in [i * math.pi / 6 for i in range(12)]]
    (cx, cy), rr = kasa_circle_fit(pts)
    assert (cx, cy, rr) == (pytest.approx(3.25, abs=1e-9),
                            pytest.approx(2.5, abs=1e-9),
                            pytest.approx(1.75, abs=1e-9))


def test_datum_pair_frame_measurement():
    frame = datum_pair_frame((2.0, 2.0), (9.0, 2.0))
    assert measure_in_frame((5.5, 3.25), frame) == (pytest.approx(3.5),
                                                    pytest.approx(1.25))


def test_synthetic_dxf_vector_measurement_agrees_with_anchors():
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (0, 6.25))          # left edge
    msp.add_circle((1.56, 2.0), 0.125)       # the anchored hole
    lines = [e for e in msp if e.dxftype() == "LINE"]
    circles = [e for e in msp if e.dxftype() == "CIRCLE"]
    s, e = lines[0].dxf.start, lines[0].dxf.end
    edge_pts = [(s.x, s.y), (e.x, e.y)]
    c, d = fit_edge_line(edge_pts)
    cc = circles[0].dxf.center
    center = (cc.x, cc.y)
    measured = point_to_line_distance(center, c, d)
    assert measured == pytest.approx(1.56, abs=1e-6)  # OCR'd "1.56" verified


# --------------------------------------------------------------------------- #
# Anchor fidelity: the wrong-edge class
# --------------------------------------------------------------------------- #
def test_anchor_fidelity_catches_wrong_edge_measurement():
    m = _model(features=[
        _feat("F002", anchors=[
            _anchor("baseline", "part_edge_left", "x", 1.56, ["D003"]),
            _anchor("baseline", "part_edge_bottom", "y", 2.0, ["D004"]),
        ]),
    ])
    # Built correctly:
    ok = verify_anchor_fidelity(m, {"F002": (1.56, 2.0)})
    assert all(f["status"] == "OK" for f in ok)
    # Built 1.56 from the RIGHT edge instead (absolute XY looks plausible):
    bad = verify_anchor_fidelity(m, {"F002": (11.0 - 1.56, 2.0)})
    x_findings = [f for f in bad if f["axis"] == "x"]
    assert x_findings[0]["status"] == "ANCHOR_MISMATCH"
    assert "part_edge_left" in x_findings[0]["detail"]


# --------------------------------------------------------------------------- #
# Macro-generation integration
# --------------------------------------------------------------------------- #
def _anchored_golden():
    data = _golden_drawing()
    # Give the mounting-hole feature explicit baseline anchors.
    for f in data["features"]:
        if f["id"] == "F002":
            f["anchors"] = [
                _anchor("baseline", "part_edge_left", "x", 0.5, ["D004"]),
                _anchor("baseline", "part_edge_bottom", "y", 1.0, ["D004"]),
            ]
    return data


def test_macro_carries_anchor_annotations_and_plan_carries_frame(tmp_path):
    data = _anchored_golden()
    model, report = run_verification(data)
    assert report.ok, str(report)
    pkg = generate_macro_package(model, data,
                                 format_verification_report(model, report), tmp_path)
    import json
    plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
    assert plan["coordinate_frame"]["frame"] == "lower_left_corner"
    hole_step = next(s for s in plan["steps"] if s["feature_id"] == "F002")
    assert hole_step["anchors"][0]["scheme"] == "baseline"
    assert hole_step["position_derivation"]
    macro = (pkg.macros_dir / hole_step["macro_file"]).read_text(encoding="utf-8")
    assert "DIMENSION ANCHORS (F002)" in macro
    assert "D004" in macro and "part_edge_left" in macro
    # The audit check passes on the emitted package…
    assert check_anchor_annotations(model, pkg, pkg.macros_dir) == []
    # …and fails loudly when the annotation is stripped.
    stripped = "\n".join(l for l in macro.splitlines()
                         if "ANCHOR" not in l and "DERIVED" not in l)
    (pkg.macros_dir / hole_step["macro_file"]).write_text(stripped, encoding="utf-8")
    errors = check_anchor_annotations(model, pkg, pkg.macros_dir)
    assert errors and "DIMENSION ANCHORS" in errors[0]


def test_pure_coordinate_parts_get_degenerate_anchors_in_plan_only(tmp_path):
    """Regression: an anchorless (coordinate-scheme) part gets plan-side
    anchors/derivation but NO macro-text annotation — the golden macro suite
    separately asserts the VBA is byte-identical."""
    data = _golden_drawing()
    model, report = run_verification(data)
    pkg = generate_macro_package(model, data,
                                 format_verification_report(model, report), tmp_path)
    import json
    plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
    hole_step = next(s for s in plan["steps"] if s["feature_id"] == "F002")
    assert hole_step["anchors"][0]["scheme"] == "coordinate"
    macro = (pkg.macros_dir / hole_step["macro_file"]).read_text(encoding="utf-8")
    assert "DIMENSION ANCHORS" not in macro
