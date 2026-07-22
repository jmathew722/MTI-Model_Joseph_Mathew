"""Geometry-correctness tests for the slot / U-notch improvements (2026-07-21).

Covers the corner-radius (r, r) inset proof the user asked for, the rounded
one-shot profile, pattern-of-slot expansion (the 16247 two-notch case), the
legacy-anchor inference, partial-arc bolt patterns, revolve profile validation,
and the CadQuery slot build (skipped when cadquery is absent). No SolidWorks /
no network.
"""
import math
from types import SimpleNamespace

import pytest

from pipeline.slot_cut import (
    arc_centers,
    expand_slot_patterns,
    _infer_legacy_anchor,
    rounded_profile_from_corners,
)


# --------------------------------------------------------------------------- #
# The (r, r) corner-arc inset — the explicit proof the rounded corner sits at
# the right place relative to the rectangle corner.
# --------------------------------------------------------------------------- #
def _slot(kind="open_notch", r=0.25):
    return SimpleNamespace(corner_radius=r, slot_kind=kind)


def test_arc_center_is_r_r_inset_open_notch():
    # Interior corners first (corner_array convention). Left-opening slot.
    corners = [[0.75, 0.0], [0.75, 1.6], [-0.05, 1.6], [-0.05, 0.0]]
    centers = arc_centers(_slot(r=0.25), corners)
    # Each center is r inset from the sharp corner along BOTH incident edges.
    assert centers[0] == [pytest.approx(0.50), pytest.approx(0.25)]
    assert centers[1] == [pytest.approx(0.50), pytest.approx(1.35)]


def test_arc_centers_all_four_for_closed_slot():
    corners = [[1.0, 1.0], [3.0, 1.0], [3.0, 2.0], [1.0, 2.0]]
    centers = arc_centers(_slot(kind="closed_slot", r=0.2), corners)
    assert len(centers) == 4
    assert centers[0] == [pytest.approx(1.2), pytest.approx(1.2)]
    assert centers[1] == [pytest.approx(2.8), pytest.approx(1.2)]
    assert centers[2] == [pytest.approx(2.8), pytest.approx(1.8)]
    assert centers[3] == [pytest.approx(1.2), pytest.approx(1.8)]


def test_no_radius_returns_no_arc_centers():
    corners = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    assert arc_centers(_slot(r=0.0), corners) == []


def test_rounded_profile_arc_points_lie_on_the_arc():
    corners = [[0.75, 0.0], [0.75, 1.6], [-0.05, 1.6], [-0.05, 0.0]]
    r = 0.25
    prof = rounded_profile_from_corners(corners, r, "open_notch", segments=8)
    centers = arc_centers(_slot(r=r), corners)
    # Every arc point must be exactly r from its corner's arc center — the
    # geometric definition of a tangent fillet arc.
    on_arc = 0
    for px, py in prof:
        for cx, cy in centers:
            if abs(math.hypot(px - cx, py - cy) - r) < 1e-9:
                on_arc += 1
                break
    assert on_arc >= 2 * 9  # 2 corners × (segments+1) arc points minimum


def test_rounded_profile_no_radius_is_the_rectangle():
    corners = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    assert rounded_profile_from_corners(corners, 0.0, "open_notch") == corners


# --------------------------------------------------------------------------- #
# Pattern-of-slot expansion (the 16247 two-notch case)
# --------------------------------------------------------------------------- #
def _sixteen247_resolved():
    return {
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base", "related_dimensions": []},
            {"id": "F002", "type": "extrude_cut", "description": "lower notch",
             "related_dimensions": []},
            {"id": "F003", "type": "pattern", "description": "linear pattern upper notch",
             "parent_feature": "F002", "quantity": 2,
             "anchors": [{"scheme": "chain", "anchor_ref": "F002", "axis": "y",
                          "value": 16.5, "dimension_ids": ["D004"], "semantics": "to_center"}]},
        ],
        "slot_cuts": [
            {"id": "F002", "slot_kind": "open_notch", "open_edge": "left",
             "anchor_edge": "left", "anchor_offset": 0.75,
             "anchor_semantics": "edge_to_centerline", "width": 1.6, "depth": 0.75,
             "corner_radius": 0.531, "thru": True, "thru_basis": "single_view_default"},
        ],
        "build_order": ["F001", "F002", "F003"],
        "dimensions": [{"id": "D004", "applies_to": "slot_center_spacing", "value": 16.5}],
    }


def test_pattern_of_slot_expands_into_explicit_second_slot():
    resolved = _sixteen247_resolved()
    flags = []
    n = expand_slot_patterns(resolved, flags.append)
    assert n == 1  # qty 2 → one extra slot
    slot_ids = {s["id"] for s in resolved["slot_cuts"]}
    assert slot_ids == {"F002", "F002_P1"}
    # The new slot is shifted +16.5 along the left edge (Y).
    new = next(s for s in resolved["slot_cuts"] if s["id"] == "F002_P1")
    assert new["anchor_offset"] == pytest.approx(0.75 + 16.5)
    assert new["open_edge"] == "left" and new["corner_radius"] == 0.531
    # The pattern feature is gone; a backing extrude_cut is in build_order.
    assert not any(f["type"] == "pattern" for f in resolved["features"])
    assert "F002_P1" in resolved["build_order"]
    assert "F003" not in resolved["build_order"]


def test_pattern_expansion_flags_ungrounded_spacing():
    resolved = _sixteen247_resolved()
    resolved["features"][2]["anchors"] = []          # no chain anchor
    resolved["dimensions"] = []                       # no spacing dim either
    flags = []
    assert expand_slot_patterns(resolved, flags.append) == 0
    assert any(f["flag_tier"] == "HIGH" for f in flags)


def test_non_slot_pattern_is_untouched():
    resolved = {
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base"},
            {"id": "F002", "type": "hole", "description": "seed hole"},
            {"id": "F003", "type": "pattern", "description": "hole pattern",
             "parent_feature": "F002", "quantity": 4, "anchors": []},
        ],
        "slot_cuts": [], "build_order": ["F001", "F002", "F003"], "dimensions": [],
    }
    assert expand_slot_patterns(resolved, lambda f: None) == 0
    assert any(f["type"] == "pattern" for f in resolved["features"])


# --------------------------------------------------------------------------- #
# Legacy anchor inference (no more hardcoded top/left/0)
# --------------------------------------------------------------------------- #
def test_legacy_anchor_from_offset_y_is_left_edge():
    feat = {"offset_x": 0.0, "offset_y": 3.5, "related_dimensions": []}
    assert _infer_legacy_anchor(feat, {}) == ("left", "left", 3.5, "edge_to_near_edge")


def test_legacy_anchor_from_offset_x_is_bottom_edge():
    feat = {"offset_x": 2.0, "offset_y": 0.0, "related_dimensions": []}
    assert _infer_legacy_anchor(feat, {}) == ("bottom", "bottom", 2.0, "edge_to_near_edge")


def test_legacy_anchor_falls_back_when_no_evidence():
    feat = {"offset_x": 0.0, "offset_y": 0.0, "related_dimensions": []}
    assert _infer_legacy_anchor(feat, {}) == ("top", "left", 0.0, "edge_to_near_edge")


# --------------------------------------------------------------------------- #
# Partial-arc bolt-circle positions
# --------------------------------------------------------------------------- #
def _bolt_callout(qty, arc):
    from pipeline.schema import HoleCallout

    return HoleCallout(id="H1", type="thru", diameter=0.25, qty=qty, pattern="circular",
                       bolt_circle_diameter=4.0, bolt_circle_center=[5.0, 5.0],
                       start_angle=0.0, arc_angle=arc)


def test_full_circle_positions_use_360_over_qty():
    from pipeline.macro_generator import _circular_positions
    from pipeline.schema import DrawingData

    m = DrawingData(units="inch", confidence=0.9)
    pts = _circular_positions(m, _bolt_callout(4, 360.0))
    angs = sorted(round(math.degrees(math.atan2(y - 5.0, x - 5.0)) % 360, 3) for x, y in pts)
    assert angs == [0.0, 90.0, 180.0, 270.0]


def test_partial_arc_positions_span_ends_inclusive():
    from pipeline.macro_generator import _circular_positions
    from pipeline.schema import DrawingData

    m = DrawingData(units="inch", confidence=0.9)
    pts = _circular_positions(m, _bolt_callout(3, 90.0))  # 3 holes over 90°
    angs = sorted(round(math.degrees(math.atan2(y - 5.0, x - 5.0)) % 360, 3) for x, y in pts)
    assert angs == [0.0, 45.0, 90.0]  # step = 90/(3-1) = 45, both ends included


# --------------------------------------------------------------------------- #
# Revolve profile validation
# --------------------------------------------------------------------------- #
def test_revolve_rejects_negative_radius():
    from pipeline.macro_generator import MacroGenerationError, revolve_sketch_points

    with pytest.raises(MacroGenerationError, match="NEGATIVE radial"):
        revolve_sketch_points([[0.0, 1.0], [1.0, -0.5]])


def test_revolve_rejects_single_axial_station():
    from pipeline.macro_generator import MacroGenerationError, revolve_sketch_points

    with pytest.raises(MacroGenerationError, match="axial station"):
        revolve_sketch_points([[2.0, 1.0], [2.0, 3.0]])


def test_revolve_accepts_valid_profile():
    from pipeline.macro_generator import revolve_sketch_points

    closed, (xmin, xmax) = revolve_sketch_points([[0.0, 5.0], [10.0, 5.0], [10.0, 8.0]])
    assert (xmin, xmax) == (0.0, 10.0)
    assert closed[0] == (0.0, 5.0)


# --------------------------------------------------------------------------- #
# CadQuery slot build (skipped without cadquery)
# --------------------------------------------------------------------------- #
def test_cadquery_builds_slot_rounded_profile():
    pytest.importorskip("cadquery")
    from pipeline.cq_prevalidate import build_solid_from_plan

    plan = {
        "units": "inch",
        "steps": [
            {"seq": 1, "feature_id": "F001", "type": "extrude_boss",
             "dimensions_drawing_units": {"length": 2.0, "width": 6.0, "depth": 0.28},
             "positions_xy": [[0.0, 0.0]]},
            {"seq": 2, "feature_id": "F002", "type": "slot_rect_cut",
             "depth_type": "through_all",
             "slot": {"slot_kind": "open_notch", "open_edge": "left",
                      "corner_radius": 0.25,
                      "corners_drawing_units": [[0.75, 0.0], [0.75, 1.6],
                                                [-0.05, 1.6], [-0.05, 0.0]]}},
            {"seq": 3, "feature_id": "F002_fillets", "type": "slot_corner_fillet",
             "positions_xy": [[0.75, 0.0], [0.75, 1.6]], "dimensions_drawing_units": {"radius": 0.25}},
        ],
    }
    solid = build_solid_from_plan(plan)
    shape = solid.val()
    assert shape.isValid()
    assert shape.Volume() > 0
    # The slot removed material: volume < the full 2.0 x 6.0 x 0.28 in³ block.
    full_mm3 = (2.0 * 25.4) * (6.0 * 25.4) * (0.28 * 25.4)
    assert shape.Volume() < full_mm3
