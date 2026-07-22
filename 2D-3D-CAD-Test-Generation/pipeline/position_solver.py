"""Position solver — the single coordinate authority (2026-07-17).

The dimensioning-architecture overhaul (research:
``docs/research/DIMENSIONING_ARCHITECTURE_NOTES.md``, audit:
``docs/AUDIT_DIMENSIONING.md``): every positioned feature carries
:class:`~pipeline.schema.PositionAnchor` records describing WHAT its position
is measured FROM — the drawing's own dimensioning scheme (chain / baseline /
ordinate / coordinate / polar-BSC / datum frame). Absolute coordinates are
DERIVED here, in exactly one place, from the anchor graph:

* the graph is **topologically ordered** (a chain anchor depends on the prior
  feature; everything ultimately grounds in part edges, the origin, a center
  datum, or datum holes);
* chains **accumulate in drawing order**, mirroring the resolver's
  dimension-chain logic;
* every solve emits a full **derivation trace**
  (``"x = part_edge_left(0) + D002(1.56) [baseline]"``) into the build plan
  for audit and the explainer;
* on a value correction, re-solving moves exactly the features anchored —
  directly or transitively — to the changed dimensions (:func:`movers`).

A feature with **no** anchors is the degenerate ``coordinate`` case: its
stored ``offset_x/offset_y`` are wrapped as origin-referenced anchors, so the
current pipeline behavior is the base case of the new system, not a casualty
(golden outputs unchanged).

Also here: the **canonical-frame selection** (Stage 2.5 records which ground
the part uses — datum-hole pair > declared dimension origin > default
lower-left corner) and the pure vector-cross-check math (edge line fitting by
total least squares, perpendicular foot distance, Kåsa circle fit, datum-pair
frame) unit-tested against synthetic geometry.

Standing rules hold: an unresolvable anchor NEVER blocks — the feature falls
back to its stored offsets with a flagged trace.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from pipeline.schema import DrawingData, Feature, PositionAnchor

from utils.logger import get_logger

log = get_logger()

# Anchor grounds that need no other feature solved first.
_EDGE_GROUNDS = ("part_edge_left", "part_edge_right", "part_edge_top",
                 "part_edge_bottom", "origin", "part_center")


# --------------------------------------------------------------------------- #
# Solved output
# --------------------------------------------------------------------------- #
@dataclass
class SolvedPosition:
    feature_id: str
    x: float
    y: float
    trace: list[str] = field(default_factory=list)   # one line per axis
    scheme: str = "coordinate"                        # dominant scheme
    grounded: bool = True    # False = fell back to stored offsets (flagged)

    def to_dict(self) -> dict[str, Any]:
        return {"feature_id": self.feature_id, "x": self.x, "y": self.y,
                "trace": self.trace, "scheme": self.scheme,
                "grounded": self.grounded}


class AnchorGraphError(Exception):
    """A structural defect in the anchor graph (cycle) — callers fall back,
    never block."""


# --------------------------------------------------------------------------- #
# Canonical frame selection (Stage 2.5 records the choice)
# --------------------------------------------------------------------------- #
def canonical_frame(model: DrawingData) -> dict[str, Any]:
    """Which ground the part's coordinates use. Priority: datum-hole pair >
    declared dimension origin (ordinate/origin symbol/datum letters) > the
    default lower-left-corner convention. Recorded in the build-plan header."""
    datum_holes = [h.id for h in model.hole_callouts if getattr(h, "is_datum_hole", False)]
    if datum_holes:
        return {"frame": "datum_hole_pair", "ground": datum_holes[:2],
                "note": "positions ground at datum-hole centers, not part edges"}
    origin_text = (getattr(model, "dimension_origin", "") or "").strip()
    if origin_text:
        return {"frame": "declared_origin", "ground": origin_text,
                "note": "drawing declares its dimension origin"}
    return {"frame": "lower_left_corner", "ground": "part outline lower-left",
            "note": "default drawing-frame convention (no explicit origin found)"}


# --------------------------------------------------------------------------- #
# Anchor derivation (the degenerate wrap + what extraction/resolution provide)
# --------------------------------------------------------------------------- #
def _coordinate_anchors(feat: Feature) -> list[PositionAnchor]:
    """Wrap stored offsets as origin-referenced coordinate anchors — the
    degenerate case equal to today's behavior."""
    return [
        PositionAnchor(scheme="coordinate", anchor_ref="origin",
                       dimension_ids=[], axis="x", value=float(feat.offset_x)),
        PositionAnchor(scheme="coordinate", anchor_ref="origin",
                       dimension_ids=[], axis="y", value=float(feat.offset_y)),
    ]


def anchors_for(feat: Feature) -> list[PositionAnchor]:
    """A feature's effective anchors: explicit ones when present, else the
    degenerate coordinate wrap of its stored offsets."""
    return list(feat.anchors) if feat.anchors else _coordinate_anchors(feat)


# --------------------------------------------------------------------------- #
# The solver
# --------------------------------------------------------------------------- #
def _ground_value(anchor_ref: str, model: DrawingData,
                  envelope: tuple[Optional[float], Optional[float]],
                  axis: str) -> Optional[float]:
    """Coordinate of a non-feature ground along ``axis`` in the drawing frame
    (lower-left origin). Returns None for unknown grounds."""
    length, width = envelope
    if anchor_ref in ("origin", "part_edge_left") and axis == "x":
        return 0.0
    if anchor_ref in ("origin", "part_edge_bottom") and axis == "y":
        return 0.0
    if anchor_ref == "part_edge_right" and axis == "x":
        return float(length) if length else None
    if anchor_ref == "part_edge_top" and axis == "y":
        return float(width) if width else None
    if anchor_ref == "part_center":
        if axis == "x":
            return float(length) / 2.0 if length else None
        return float(width) / 2.0 if width else None
    # part_edge_left on the y axis (etc.) contributes 0 along the other axis.
    if anchor_ref in _EDGE_GROUNDS:
        return 0.0
    return None


def _envelope(model: DrawingData) -> tuple[Optional[float], Optional[float]]:
    length = width = None
    for d in model.dimensions:
        a = (d.applies_to or "").lower()
        if a == "length" and d.value:
            length = float(d.value)
        elif a in ("width", "height") and d.value and width is None:
            width = float(d.value)
    return length, width


def _feature_deps(anchors: list[PositionAnchor]) -> set[str]:
    """Feature ids this anchor set depends on (chain targets, polar centers,
    datum holes resolved to hole feature_refs are handled by the caller)."""
    deps: set[str] = set()
    for a in anchors:
        ref = a.anchor_ref
        if ref in _EDGE_GROUNDS or ref.startswith("DRF_") or ref.startswith("DATUM_HOLE"):
            continue
        deps.add(ref[:-7] if ref.endswith("_center") else ref)
    return deps


def solve_positions(model: DrawingData) -> dict[str, SolvedPosition]:
    """Solve every feature's absolute position from its anchors.

    Topological order over the anchor graph; a cycle or unresolvable anchor
    falls back to the feature's stored offsets with ``grounded=False`` and a
    flagged trace — never a block.
    """
    envelope = _envelope(model)
    feats = {f.id: f for f in model.features}
    anchor_map = {fid: anchors_for(f) for fid, f in feats.items()}
    solved: dict[str, SolvedPosition] = {}

    # Kahn-style topological pass with a fallback sweep for cycles.
    remaining = dict(anchor_map)
    for _ in range(len(remaining) + 1):
        progressed = False
        for fid in list(remaining):
            deps = _feature_deps(remaining[fid]) & set(feats)
            if deps - set(solved):
                continue
            solved[fid] = _solve_one(feats[fid], remaining.pop(fid),
                                     model, envelope, solved)
            progressed = True
        if not remaining or not progressed:
            break
    for fid in remaining:  # cycle members: stored offsets, flagged
        f = feats[fid]
        solved[fid] = SolvedPosition(
            fid, float(f.offset_x), float(f.offset_y),
            trace=[f"UNRESOLVED anchor cycle — fell back to stored offsets "
                   f"({f.offset_x}, {f.offset_y})"],
            scheme="coordinate", grounded=False)
    return solved


def _solve_one(feat: Feature, anchors: list[PositionAnchor], model: DrawingData,
               envelope: tuple[Optional[float], Optional[float]],
               solved: dict[str, SolvedPosition]) -> SolvedPosition:
    x: Optional[float] = None
    y: Optional[float] = None
    trace: list[str] = []
    schemes: list[str] = []
    grounded = True

    # Polar pair (radial + angular) solves both axes at once.
    radial = next((a for a in anchors if a.axis == "radial"), None)
    angular = next((a for a in anchors if a.axis == "angular"), None)
    if radial is not None:
        cx, cy, cref = _polar_center(radial, model, envelope, solved)
        if cx is None:
            grounded = False
            trace.append(f"UNRESOLVED polar center {radial.anchor_ref!r} — "
                         "fell back to stored offsets")
        else:
            ang = math.radians(angular.value if angular is not None else 0.0)
            x = cx + radial.value * math.cos(ang)
            y = cy + radial.value * math.sin(ang)
            dim = ",".join(radial.dimension_ids) or "BSC"
            if angular is None:
                # A radial anchor with no angular partner defaults to 0° — flag
                # it rather than silently collinear-stacking every instance.
                trace.append(
                    f"WARNING: polar anchor {dim} has no angular value — defaulted to 0°; "
                    "verify the instance angle.")
            trace.append(
                f"x,y = {cref}({_fmt(cx)}, {_fmt(cy)}) + {dim}({_fmt(radial.value)}) "
                f"@ {_fmt(angular.value if angular else 0.0)}° [polar_bsc]")
            schemes.append("polar_bsc")

    for a in anchors:
        if a.axis in ("radial", "angular"):
            continue
        base, base_name = _anchor_base(a, model, envelope, solved)
        if base is None:
            grounded = False
            trace.append(f"{a.axis} = UNRESOLVED anchor {a.anchor_ref!r} — "
                         f"fell back to stored offset")
            continue
        sign = -1.0 if a.anchor_ref in ("part_edge_right",) and a.axis == "x" else 1.0
        sign = -1.0 if a.anchor_ref == "part_edge_top" and a.axis == "y" else sign
        val = base + sign * a.value
        dim = ",".join(a.dimension_ids) or _fmt(a.value)
        trace.append(f"{a.axis} = {base_name}({_fmt(base)}) "
                     f"{'-' if sign < 0 else '+'} {dim}({_fmt(a.value)}) [{a.scheme}]")
        schemes.append(a.scheme)
        if a.axis == "x":
            x = val
        else:
            y = val

    if x is None:
        x = float(feat.offset_x)
    if y is None:
        y = float(feat.offset_y)
    dominant = next((s for s in ("datum_frame", "polar_bsc", "chain", "ordinate",
                                 "baseline", "coordinate") if s in schemes),
                    "coordinate")
    return SolvedPosition(feat.id, x, y, trace=trace, scheme=dominant,
                          grounded=grounded)


def _anchor_base(a: PositionAnchor, model: DrawingData,
                 envelope: tuple[Optional[float], Optional[float]],
                 solved: dict[str, SolvedPosition]) -> tuple[Optional[float], str]:
    """(base coordinate along a.axis, human name) for a linear anchor."""
    ref = a.anchor_ref
    if ref.startswith("DRF_"):
        # Datum reference frame grounds at the origin of the frame (datum B/C
        # intersection = the drawing frame origin once the frame is selected).
        return 0.0, ref
    if ref.startswith("DATUM_HOLE"):
        h = _datum_hole(model, ref)
        if h is None:
            return None, ref
        return (h[0] if a.axis == "x" else h[1]), ref
    g = _ground_value(ref, model, envelope, a.axis)
    if g is not None:
        return g, ref
    fid = ref[:-7] if ref.endswith("_center") else ref
    if fid in solved:
        s = solved[fid]
        return (s.x if a.axis == "x" else s.y), fid
    return None, ref


def _polar_center(radial: PositionAnchor, model: DrawingData,
                  envelope: tuple[Optional[float], Optional[float]],
                  solved: dict[str, SolvedPosition],
                  ) -> tuple[Optional[float], Optional[float], str]:
    ref = radial.anchor_ref
    if ref == "part_center":
        length, width = envelope
        if length and width:
            return length / 2.0, width / 2.0, "part_center"
        return None, None, ref
    if ref.startswith("DATUM_HOLE"):
        h = _datum_hole(model, ref)
        return (h[0], h[1], ref) if h else (None, None, ref)
    fid = ref[:-7] if ref.endswith("_center") else ref
    if fid in solved:
        s = solved[fid]
        return s.x, s.y, fid
    return None, None, ref


def _datum_hole(model: DrawingData, ref: str) -> Optional[tuple[float, float]]:
    """Center of DATUM_HOLE_<n> (1-based over is_datum_hole callouts)."""
    datum = [h for h in model.hole_callouts if getattr(h, "is_datum_hole", False)]
    try:
        n = int(ref.rsplit("_", 1)[-1])
    except ValueError:
        n = 1
    if not datum or n < 1 or n > len(datum):
        return None
    h = datum[n - 1]
    if h.instance_positions:
        p = h.instance_positions[0]
        return float(p[0]), float(p[1])
    if h.position_known:
        return float(h.x_position), float(h.y_position)
    return None


def _fmt(v: float) -> str:
    return f"{float(v):.6g}"


# --------------------------------------------------------------------------- #
# Correction propagation
# --------------------------------------------------------------------------- #
def movers(model: DrawingData, changed_dim_ids: set[str] | list[str]) -> set[str]:
    """Feature ids whose position depends — directly or transitively — on any
    of the changed dimensions. Everything else provably does not move."""
    changed = set(changed_dim_ids)
    feats = {f.id: f for f in model.features}
    anchor_map = {fid: anchors_for(f) for fid, f in feats.items()}
    direct = {fid for fid, ans in anchor_map.items()
              if any(set(a.dimension_ids) & changed for a in ans)}
    # Transitive closure over feature-referencing anchors.
    moved = set(direct)
    for _ in range(len(feats)):
        grew = False
        for fid, ans in anchor_map.items():
            if fid in moved:
                continue
            if _feature_deps(ans) & moved:
                moved.add(fid)
                grew = True
        if not grew:
            break
    return moved


# --------------------------------------------------------------------------- #
# Anchor fidelity (Stage 10.6 addition): measured-vs-anchor re-check
# --------------------------------------------------------------------------- #
def verify_anchor_fidelity(model: DrawingData,
                           measured: dict[str, tuple[float, float]],
                           tol: float = 0.01) -> list[dict[str, Any]]:
    """Re-measure each feature's position RELATIVE TO ITS ANCHOR in the built
    model and compare to the anchor value. Catches "right hole, right size,
    measured-from-the-wrong-edge" — the class absolute-XY checks miss.

    ``measured`` maps feature id → measured (x, y) in the drawing frame.
    Returns one finding per checkable anchor: OK or ANCHOR_MISMATCH (with the
    measured relative value); features without measurements are skipped.
    """
    envelope = _envelope(model)
    solved = solve_positions(model)
    findings: list[dict[str, Any]] = []
    for f in model.features:
        if f.id not in measured or not f.anchors:
            continue
        mx, my = measured[f.id]
        for a in f.anchors:
            if a.axis == "angular":
                continue
            if a.axis == "radial":
                c = _polar_center(a, model, envelope, solved)
                if c[0] is None:
                    continue
                rel = math.hypot(mx - c[0], my - c[1])
            else:
                base, _name = _anchor_base(a, model, envelope, solved)
                if base is None:
                    continue
                m = mx if a.axis == "x" else my
                rel = abs(m - base)
            ok = abs(rel - abs(a.value)) <= tol
            findings.append({
                "feature_id": f.id, "axis": a.axis, "scheme": a.scheme,
                "anchor_ref": a.anchor_ref,
                "expected": abs(a.value), "measured_relative": round(rel, 6),
                "status": "OK" if ok else "ANCHOR_MISMATCH",
                "detail": ("" if ok else
                           f"feature measures {_fmt(rel)} from {a.anchor_ref}, "
                           f"drawing says {_fmt(abs(a.value))} "
                           f"(dims {','.join(a.dimension_ids) or '-'})"),
            })
    return findings


# --------------------------------------------------------------------------- #
# Vector cross-check math (pure; unit-tested against synthetic geometry)
# --------------------------------------------------------------------------- #
def fit_edge_line(points: list[tuple[float, float]]) -> tuple[tuple[float, float],
                                                              tuple[float, float]]:
    """Total-least-squares line through a point cloud: returns (centroid,
    unit direction) via the principal component. TLS, not y-on-x regression,
    because drawing edges are frequently near-vertical."""
    n = len(points)
    if n < 2:
        raise ValueError("need >= 2 points to fit an edge line")
    cx = sum(p[0] for p in points) / n
    cy = sum(p[1] for p in points) / n
    sxx = sum((p[0] - cx) ** 2 for p in points)
    syy = sum((p[1] - cy) ** 2 for p in points)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in points)
    # Principal direction of the 2x2 covariance [[sxx, sxy], [sxy, syy]].
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    return (cx, cy), (math.cos(theta), math.sin(theta))


def point_to_line_distance(p: tuple[float, float], line_point: tuple[float, float],
                           line_dir: tuple[float, float]) -> float:
    """Perpendicular foot distance |(p - c) · n̂| — the measured value of
    'feature is d from edge'."""
    nx, ny = -line_dir[1], line_dir[0]
    norm = math.hypot(nx, ny)
    return abs((p[0] - line_point[0]) * nx + (p[1] - line_point[1]) * ny) / norm


def kasa_circle_fit(points: list[tuple[float, float]]
                    ) -> tuple[tuple[float, float], float]:
    """Algebraic least-squares circle (Kåsa): minimize Σ(x²+y²+Dx+Ey+F)².
    Returns (center, radius). Exact for points on a true circle."""
    n = len(points)
    if n < 3:
        raise ValueError("need >= 3 points to fit a circle")
    # Normal equations for [D, E, F].
    sx = sum(p[0] for p in points); sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points); syy = sum(p[1] * p[1] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    sxz = sum(p[0] * (p[0] ** 2 + p[1] ** 2) for p in points)
    syz = sum(p[1] * (p[0] ** 2 + p[1] ** 2) for p in points)
    sz = sum(p[0] ** 2 + p[1] ** 2 for p in points)
    # Solve [[sxx,sxy,sx],[sxy,syy,sy],[sx,sy,n]] · [D,E,F]ᵀ = -[sxz,syz,sz]ᵀ
    import numpy as np
    a = np.array([[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]], dtype=float)
    b = -np.array([sxz, syz, sz], dtype=float)
    d, e, f_ = np.linalg.solve(a, b)
    cx, cy = -d / 2.0, -e / 2.0
    r = math.sqrt(max(cx * cx + cy * cy - f_, 0.0))
    return (cx, cy), r


def datum_pair_frame(c1: tuple[float, float], c2: tuple[float, float]
                     ) -> tuple[tuple[float, float], tuple[float, float],
                                tuple[float, float]]:
    """Frame from a datum-hole pair: origin = hole-1 center, +x = unit vector
    to hole-2, +y = its left normal. Returns (origin, x_hat, y_hat)."""
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    d = math.hypot(dx, dy)
    if d <= 0:
        raise ValueError("datum holes are coincident — no frame")
    x_hat = (dx / d, dy / d)
    y_hat = (-x_hat[1], x_hat[0])
    return c1, x_hat, y_hat


def measure_in_frame(p: tuple[float, float],
                     frame: tuple[tuple[float, float], tuple[float, float],
                                  tuple[float, float]]) -> tuple[float, float]:
    """A point's coordinates in a datum-pair frame (dot products)."""
    origin, x_hat, y_hat = frame
    vx, vy = p[0] - origin[0], p[1] - origin[1]
    return (vx * x_hat[0] + vy * x_hat[1], vx * y_hat[0] + vy * y_hat[1])
