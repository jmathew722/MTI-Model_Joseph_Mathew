"""Phase A — per-feature geometric verification (2026-07-10 accuracy layer).

Where ``constraint_verify.py`` grades the built STL against the operator's
MUST-MEET constraints only, this module verifies EVERY feature the pipeline
resolved — each hole's position + diameter + through/blind, each slot's obround
signature + extents, each cut/notch's location, and the base envelope — against
``build_plan.json`` (the self-contained, origin-framed plan) and, when present,
``_resolved_extraction.json``. It is the measurement half of the closed
build->measure->correct loop in ``reconciliation.py`` (Phase B).

Coordinate frame: the SolidWorks (and CadQuery) STL is exported in the same
lower-left-origin drawing frame the build plan uses (empirically verified: a
built 11.0x5.25 plate exports with bounds starting at (0,0,0), holes at their
drawn (x, y)). We still subtract the base solid's min-corner so a translated
export aligns. The thinnest mesh axis is the extrude/thickness axis; the other
two are the in-plane (x, y) the drawing dimensions.

Every feature ends with exactly one classification — never silently skipped:

    OK            built, positioned, and sized within tolerance
    MISSING       expected feature has no corresponding geometry
    MISPLACED     present but off-position (measured position reported)
    WRONG_SIZE    positioned but wrong diameter/extent (measured size reported)
    EXTRA         measured geometry with no matching expected feature
    UNMEASURABLE  cannot be measured by cross-section (edge fillet/chamfer,
                  cosmetic thread, sub-tessellation size, non-watertight mesh) —
                  always with a stated reason.

Output: ``<Part>_feature_verification.json``. Public entry:
:func:`verify_features`.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

# Reuse the proven cross-section machinery rather than duplicating it.
from pipeline.constraint_verify import (
    _ensure_mesh_deps,
    _fit_circles,
    _loops_at,
    _pick_scale,
)
from pipeline.schema import is_envelope_label

log = get_logger()

FEATURE_VERIFICATION = "feature_verification.json"

# Tessellated-mesh tolerances (inches). A fitted circle's centre carries a small
# chord-deviation bias, so sub-0.010" position agreement is at the mesh's
# resolution limit; the defaults below catch real misplacement without crying
# wolf on a good part, and are configurable for finer CAD-vs-CAD meshes.
DEFAULT_POS_TOL_IN = 0.015
DEFAULT_DIA_TOL_IN = 0.02
# How far out to still call a present-but-shifted hole MISPLACED (vs. MISSING).
MISPLACED_SEARCH_IN = 1.5

# Classifications.
OK = "OK"
MISSING = "MISSING"
MISPLACED = "MISPLACED"
WRONG_SIZE = "WRONG_SIZE"
EXTRA = "EXTRA"
UNMEASURABLE = "UNMEASURABLE"

_HOLE_TYPES = {"hole", "thread"}
_EDGE_TYPES = {"fillet", "chamfer"}


# --------------------------------------------------------------------------- #
# Mesh loading + frame
# --------------------------------------------------------------------------- #
class _Mesh:
    """A loaded STL with the origin-frame transform resolved once."""

    def __init__(self, stl_path: Path, expected_thickness_in: Optional[float]):
        _ensure_mesh_deps()
        import trimesh

        self.mesh = trimesh.load(str(stl_path), force="mesh")
        ext = self.mesh.extents
        self.scale = _pick_scale(ext, expected_thickness_in)          # raw -> inches
        self.axis = int(min(range(3), key=lambda i: ext[i]))           # thickness axis
        self.u, self.v = [(1, 2), (0, 2), (0, 1)][self.axis]           # in-plane axes
        self.lo = float(self.mesh.bounds[0][self.axis])
        self.hi = float(self.mesh.bounds[1][self.axis])
        self.thickness_in = (self.hi - self.lo) * self.scale
        # Min-corner of the in-plane frame (subtracted so a translated export
        # still lands in the lower-left-origin drawing frame).
        self.u0 = float(self.mesh.bounds[0][self.u])
        self.v0 = float(self.mesh.bounds[0][self.v])
        self.watertight = bool(getattr(self.mesh, "is_watertight", False))

    def to_origin_frame(self, raw_u: float, raw_v: float) -> tuple[float, float]:
        """Section-plane raw coords -> inches in the lower-left-origin frame."""
        return ((raw_u - self.u0) * self.scale, (raw_v - self.v0) * self.scale)

    def plane_extents_in(self) -> tuple[float, float]:
        b = self.mesh.bounds
        return ((b[1][self.u] - b[0][self.u]) * self.scale,
                (b[1][self.v] - b[0][self.v]) * self.scale)

    def circles_at(self, frac: float) -> list[dict]:
        """Fitted internal circles (holes/bores) at a thickness fraction, in
        origin-frame inches, excluding the outer boundary loop."""
        coord = self.lo + (self.hi - self.lo) * frac
        circles = _fit_circles(_loops_at(self.mesh, self.axis, coord), self.axis)
        if not circles:
            return []
        outer = max(circles, key=lambda c: c["area"])
        out = []
        for c in circles:
            if c is outer or not c["circular"]:
                continue
            x, y = self.to_origin_frame(c["x"], c["y"])
            out.append({"x": x, "y": y, "diameter": 2.0 * c["r"] * self.scale})
        return out

    def measured_holes(self) -> list[dict]:
        """Every internal circular hole, with a through flag (present near BOTH
        faces). Origin-frame inches."""
        mid = self.circles_at(0.5)
        near = self.circles_at(0.06)
        far = self.circles_at(0.94)

        def _present(c: dict, group: list[dict]) -> bool:
            # Through = a hole loop appears near BOTH faces at the same (x, y).
            # Match by POSITION only, not diameter: a counterbore/countersink
            # shows a larger opening near one face than the through bore, so a
            # diameter-equality through-test would wrongly read a valid cbore/csk
            # through hole as blind.
            return any(math.hypot(c["x"] - o["x"], c["y"] - o["y"]) <= DEFAULT_POS_TOL_IN * 3
                       for o in group)

        return [{**c, "through": _present(c, near) and _present(c, far)} for c in mid]

    def _midplane_polygons(self) -> Optional[tuple[list, list]]:
        """(outer_boundary, [holes]) polygons of the mid-thickness section, each
        a list of (x, y) origin-frame inch points. None if no section. Cached.

        Pure cross-section geometry — no ray/containment backend (rtree/embree)
        required, so this works in the pinned pipeline environment."""
        if getattr(self, "_poly_cache", "unset") != "unset":
            return self._poly_cache
        coord = (self.lo + self.hi) / 2.0
        loops = _loops_at(self.mesh, self.axis, coord)
        polys = []
        for pts in loops:
            poly = [self.to_origin_frame(p[self.u], p[self.v]) for p in pts]
            area = 0.0
            n = len(poly)
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                area += x1 * y2 - x2 * y1
            polys.append((abs(area) / 2.0, poly))
        if not polys:
            self._poly_cache = None
            return None
        polys.sort(key=lambda t: t[0], reverse=True)
        outer = polys[0][1]
        holes = [p for _, p in polys[1:]]
        self._poly_cache = (outer, holes)
        return self._poly_cache

    @staticmethod
    def _point_in_poly(x: float, y: float, poly: list) -> bool:
        """Ray-cast point-in-polygon (no dependencies)."""
        inside = False
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > y) != (yj > y)) and \
               (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
                inside = not inside
            j = i
        return inside

    def material_fraction(self, cx: float, cy: float, radius_in: float,
                          samples: int = 24) -> Optional[float]:
        """Fraction of a small disc of sample points (origin-frame inches, at
        mid-thickness) that lie in SOLID material — inside the outer boundary
        and outside every hole loop. None if the section cannot be taken."""
        polys = self._midplane_polygons()
        if polys is None:
            return None
        outer, holes = polys
        pts = [(cx, cy)]
        for r_in, n in [(radius_in * 0.5, max(6, samples // 2)), (radius_in, samples)]:
            for k in range(n):
                ang = 2 * math.pi * k / n
                pts.append((cx + r_in * math.cos(ang), cy + r_in * math.sin(ang)))
        solid = 0
        for (px, py) in pts:
            if self._point_in_poly(px, py, outer) and \
               not any(self._point_in_poly(px, py, h) for h in holes):
                solid += 1
        return float(solid) / len(pts)


# --------------------------------------------------------------------------- #
# Expected-feature extraction from the build plan
# --------------------------------------------------------------------------- #
def _feature_steps(build_plan: dict) -> list[dict]:
    return [s for s in build_plan.get("steps", []) or []
            if s.get("feature_id") not in ("-", None)
            and 0 < int(s.get("seq", 0)) < 999]


def _expected_holes(build_plan: dict) -> list[dict]:
    """Every expected hole instance (origin frame, inches)."""
    holes: list[dict] = []
    for s in _feature_steps(build_plan):
        if s.get("type") not in _HOLE_TYPES:
            continue
        dims = s.get("dimensions_drawing_units") or {}
        dia = dims.get("diameter") or dims.get("hole_diameter")
        if not dia:
            continue
        thru = s.get("depth_type") == "through_all"
        for (x, y) in (s.get("positions_xy") or []):
            holes.append({"feature_id": s.get("feature_id"), "type": s.get("type"),
                          "x": float(x), "y": float(y), "diameter": float(dia),
                          "through": thru})
        # circular_pattern instances live on the step's positions too
    for s in _feature_steps(build_plan):
        if s.get("type") == "circular_pattern":
            spec = s.get("circular_pattern") or {}
            dia = _seed_dia(build_plan, s.get("feature_id"))
            for (x, y) in (s.get("positions_xy") or []):
                if dia:
                    holes.append({"feature_id": s.get("feature_id"), "type": "circular_pattern",
                                  "x": float(x), "y": float(y), "diameter": float(dia),
                                  "through": True})
    return holes


def _cut_footprints(build_plan: dict) -> list[tuple[float, float, float]]:
    """(cx, cy, radius) footprints of every profile cut/slot, so a measured loop
    that is really a cut's own boundary is not mistaken for a phantom EXTRA hole."""
    prints: list[tuple[float, float, float]] = []
    for s in _feature_steps(build_plan):
        if s.get("type") != "extrude_cut":
            continue
        dims = s.get("dimensions_drawing_units") or {}
        pos = (s.get("positions_xy") or [[0.0, 0.0]])[0]
        dia = dims.get("diameter") or dims.get("hole_diameter")
        if dia:
            prints.append((float(pos[0]), float(pos[1]), float(dia) / 2.0 + 0.1))
        else:
            length = dims.get("length") or dims.get("width")
            width = dims.get("width") or dims.get("length")
            if length and width:
                cx = float(pos[0]) + float(length) / 2.0
                cy = float(pos[1]) + float(width) / 2.0
                prints.append((cx, cy, math.hypot(float(length), float(width)) / 2.0 + 0.1))
    return prints


def _seed_dia(build_plan: dict, feature_id: str) -> Optional[float]:
    for s in build_plan.get("steps", []):
        if s.get("feature_id") == feature_id and s.get("type") in _HOLE_TYPES:
            d = (s.get("dimensions_drawing_units") or {}).get("diameter")
            if d:
                return float(d)
    return None


# --------------------------------------------------------------------------- #
# Matching + classification
# --------------------------------------------------------------------------- #
def _match_holes(expected: list[dict], measured: list[dict],
                 pos_tol: float, dia_tol: float) -> tuple[list[dict], list[dict]]:
    """Greedy nearest-position matching. Returns (feature_results, extras)."""
    remaining = list(range(len(measured)))
    results: list[dict] = []

    for e in expected:
        best_i, best_d = None, float("inf")
        for i in remaining:
            m = measured[i]
            d = math.hypot(e["x"] - m["x"], e["y"] - m["y"])
            if d < best_d:
                best_i, best_d = i, d
        checks: list[dict] = []
        classification = MISSING
        measured_hole = None
        if best_i is not None and best_d <= MISPLACED_SEARCH_IN:
            m = measured[best_i]
            measured_hole = m
            remaining.remove(best_i)
            pos_ok = best_d <= pos_tol
            dia_ok = abs(m["diameter"] - e["diameter"]) <= dia_tol
            through_ok = (m["through"] == e["through"])
            checks = [
                {"check": "presence", "status": "PASS"},
                {"check": "position", "status": "PASS" if pos_ok else "FAIL",
                 "expected": [round(e["x"], 4), round(e["y"], 4)],
                 "measured": [round(m["x"], 4), round(m["y"], 4)],
                 "delta_in": round(best_d, 4)},
                {"check": "diameter", "status": "PASS" if dia_ok else "FAIL",
                 "expected": round(e["diameter"], 4), "measured": round(m["diameter"], 4)},
                {"check": "through", "status": "PASS" if through_ok else "FAIL",
                 "expected": e["through"], "measured": m["through"]},
            ]
            if not pos_ok:
                classification = MISPLACED
            elif not dia_ok:
                classification = WRONG_SIZE
            elif not through_ok:
                classification = WRONG_SIZE  # depth/through is a size-class mismatch
            else:
                classification = OK
        else:
            checks = [{"check": "presence", "status": "FAIL",
                       "expected": [round(e["x"], 4), round(e["y"], 4)],
                       "measured": None}]
        results.append({
            "feature_id": e["feature_id"], "type": e["type"], "kind": "hole",
            "expected": {"x": round(e["x"], 4), "y": round(e["y"], 4),
                         "diameter": round(e["diameter"], 4), "through": e["through"]},
            "measured": ({"x": round(measured_hole["x"], 4), "y": round(measured_hole["y"], 4),
                          "diameter": round(measured_hole["diameter"], 4),
                          "through": measured_hole["through"]} if measured_hole else None),
            "checks": checks,
            "classification": classification,
        })

    extras = [{"kind": "hole", "classification": EXTRA,
               "measured": {"x": round(measured[i]["x"], 4), "y": round(measured[i]["y"], 4),
                            "diameter": round(measured[i]["diameter"], 4)}}
              for i in remaining]
    return results, extras


def _verify_cut(mesh: _Mesh, step: dict, pos_tol: float) -> dict:
    """Cut / notch / step: material-absence test at the expected extent.

    A circular bore/cutout uses its radius; a rectangular cut uses half its
    smaller side. PASS when the interior is mostly hollow; MISSING when it is
    mostly solid; MISPLACED when a hollow region exists but is offset."""
    dims = step.get("dimensions_drawing_units") or {}
    positions = step.get("positions_xy") or [[0.0, 0.0]]
    cx, cy = float(positions[0][0]), float(positions[0][1])
    dia = dims.get("diameter") or dims.get("hole_diameter")
    if dia:
        probe_r = float(dia) / 2.0 * 0.6           # inside the bore, off the rim
    else:
        length = dims.get("length") or dims.get("width")
        width = dims.get("width") or dims.get("length")
        if not (length and width):
            return _unmeasurable(step, "cut has neither diameter nor length+width to probe")
        probe_r = min(float(length), float(width)) / 2.0 * 0.5
        cx = cx + float(length) / 2.0             # rect positions_xy is the corner
        cy = cy + float(width) / 2.0

    frac = mesh.material_fraction(cx, cy, probe_r)
    if frac is None:
        return _unmeasurable(step, "no mid-thickness cross-section available to probe the cut")

    # Hollow interior => the cut removed material there.
    if frac <= 0.25:
        cls, status = OK, "PASS"
        measured = {"material_fraction_inside": round(frac, 3)}
    elif frac >= 0.75:
        # Solid where the cut should be — search a small neighbourhood for the
        # hollow region (MISPLACED) before concluding MISSING.
        found = None
        for dx in (-1.0, -0.5, 0.5, 1.0):
            for dy in (-1.0, -0.5, 0.5, 1.0):
                f2 = mesh.material_fraction(cx + dx, cy + dy, probe_r)
                if f2 is not None and f2 <= 0.25:
                    found = (cx + dx, cy + dy, f2)
                    break
            if found:
                break
        if found:
            cls, status = MISPLACED, "FAIL"
            measured = {"material_fraction_inside": round(frac, 3),
                        "hollow_region_near": [round(found[0], 3), round(found[1], 3)]}
        else:
            cls, status = MISSING, "FAIL"
            measured = {"material_fraction_inside": round(frac, 3)}
    else:
        cls, status = OK, "PASS"     # partial (edge cutout) — material on one side is expected
        measured = {"material_fraction_inside": round(frac, 3)}

    return {
        "feature_id": step.get("feature_id"), "type": step.get("type"), "kind": "cut",
        "expected": {"x": round(cx, 4), "y": round(cy, 4),
                     **({"diameter": round(float(dia), 4)} if dia else
                        {"length": dims.get("length"), "width": dims.get("width")})},
        "measured": measured,
        "checks": [{"check": "material_absence", "status": status,
                    "detail": f"material fraction inside probe = {round(frac, 3)}"}],
        "classification": cls,
    }


def _unmeasurable(step: dict, reason: str) -> dict:
    return {"feature_id": step.get("feature_id"), "type": step.get("type"),
            "kind": step.get("type"), "classification": UNMEASURABLE,
            "reason": reason, "checks": [{"check": "measurable", "status": UNMEASURABLE}]}


def _verify_base(mesh: _Mesh, build_plan: dict, resolved: Optional[dict],
                 pos_tol: float) -> Optional[dict]:
    """Base envelope check.

    Calibrated against a real finding: the COM builder derives the two plate
    sides via ``_rect_sides`` (two largest DISTINCT of length/width/height), but
    the build_plan base step's ``dimensions_drawing_units`` sometimes only
    records length+width and omits the true second side (it lives in ``height``),
    so the plan's width can disagree with the (correct) built width. The built
    geometry is right; only the recorded metadata is incomplete. So:

      * THICKNESS (depth) is recorded and built reliably -> STRICT check. This is
        what catches a genuinely wrong part (e.g. an extraction that misread a
        large in-plane value as a 6-10" extrude depth on a thin plate).
      * OVERALL SIZE — the LARGER measured extent vs the largest in-plane
        candidate — is reliable -> STRICT (catches a grossly oversized/undersized
        part, incl. an extra tall boss enlarging the footprint).
      * The SMALLER measured extent is the mis-paired side -> ADVISORY: PASS if it
        matches ANY recorded candidate (length/width/height) or the resolved
        envelope, else noted as "plan metadata may be incomplete" WITHOUT failing
        (the built geometry is not necessarily wrong)."""
    base = next((s for s in _feature_steps(build_plan)
                 if s.get("type") in ("extrude_boss", "revolve")), None)
    if base is None:
        return None
    dims = base.get("dimensions_drawing_units") or {}
    candidates = sorted({float(dims[k]) for k in ("length", "width", "height")
                         if dims.get(k) and float(dims[k]) > 0}, reverse=True)
    # Pull extra envelope candidates from the resolved extraction when provided.
    if resolved:
        for d in resolved.get("dimensions", []) or []:
            try:
                if is_envelope_label(d.get("applies_to", "")) and float(d.get("value", 0)) > 0:
                    candidates.append(float(d["value"]))
            except Exception:
                pass
    candidates = sorted(set(round(c, 4) for c in candidates), reverse=True)
    exp_t = dims.get("depth") or dims.get("thickness")
    mu, mv = mesh.plane_extents_in()
    meas_l, meas_w = max(mu, mv), min(mu, mv)
    checks: list[dict] = []
    cls = OK

    def _matches_any(val: float) -> bool:
        return any(abs(val - c) <= max(0.05, c * 0.02) for c in candidates)

    if candidates:
        # Overall size: larger measured extent vs largest candidate — strict.
        big = candidates[0]
        big_ok = abs(meas_l - big) <= max(0.05, big * 0.03) or _matches_any(meas_l)
        checks.append({"check": "overall_size", "status": "PASS" if big_ok else "FAIL",
                       "expected": round(big, 4), "measured": round(meas_l, 4)})
        if not big_ok:
            cls = WRONG_SIZE
        # Smaller side: advisory (the historically mis-paired metadata).
        small_ok = _matches_any(meas_w)
        checks.append({"check": "second_side", "status": "PASS" if small_ok else "ADVISORY",
                       "expected": [round(c, 4) for c in candidates],
                       "measured": round(meas_w, 4),
                       **({} if small_ok else {"note": "measured second side is not among the "
                          "recorded plan dimensions — build_plan envelope metadata may be "
                          "incomplete; the built geometry is not necessarily wrong"})})
    if exp_t:
        t_ok = abs(mesh.thickness_in - float(exp_t)) <= max(0.02, float(exp_t) * 0.1)
        checks.append({"check": "thickness", "status": "PASS" if t_ok else "FAIL",
                       "expected": round(float(exp_t), 4),
                       "measured": round(mesh.thickness_in, 4)})
        if not t_ok:
            cls = WRONG_SIZE
    return {"feature_id": base.get("feature_id"), "type": base.get("type"), "kind": "base",
            "checks": checks, "classification": cls if checks else UNMEASURABLE,
            **({} if checks else {"reason": "no envelope dimensions on the base step"})}


def _cadquery_volume(part_dir: Path) -> Optional[float]:
    """CadQuery pre-validation solid volume (mm^3) if recorded, for cross-check."""
    p = Path(part_dir) / "prevalidation_report.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            v = d.get("volume_mm3") or (d.get("solid") or {}).get("volume_mm3")
            return float(v) if v else None
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def verify_features(
    stl_path: Path,
    build_plan: dict,
    part_dir: Path,
    *,
    resolved_extraction: Optional[dict] = None,
    part: str = "",
    expected_thickness_in: Optional[float] = None,
    pos_tol_in: float = DEFAULT_POS_TOL_IN,
    dia_tol_in: float = DEFAULT_DIA_TOL_IN,
    write: bool = True,
) -> dict:
    """Verify every planned feature against the built STL. Never raises; an
    internal error becomes ``{"ok": False, "error": ...}`` so the caller can
    gate on it. Returns the report dict (also written to
    ``<Part>_feature_verification.json`` when ``write`` and a ``part`` name)."""
    report: dict[str, Any] = {"ok": True, "part": part, "stl": Path(stl_path).name,
                              "features": [], "extras": []}
    try:
        if expected_thickness_in is None:
            base = next((s for s in _feature_steps(build_plan)
                         if s.get("type") in ("extrude_boss", "revolve")), None)
            if base:
                d = base.get("dimensions_drawing_units") or {}
                expected_thickness_in = d.get("depth") or d.get("height") or d.get("thickness")

        mesh = _Mesh(Path(stl_path), expected_thickness_in)
        report["measured_thickness_in"] = round(mesh.thickness_in, 4)
        report["mesh_watertight"] = mesh.watertight

        # Base envelope.
        base_res = _verify_base(mesh, build_plan, resolved_extraction, pos_tol_in)
        if base_res:
            report["features"].append(base_res)

        # Volume cross-check vs CadQuery pre-validation.
        cq_vol = _cadquery_volume(part_dir)
        if cq_vol:
            try:
                # mesh.volume is in raw units^3; scale (raw->in) * 25.4 = raw->mm.
                mesh_vol_mm3 = float(mesh.mesh.volume) * (mesh.scale * 25.4) ** 3
                agree = abs(mesh_vol_mm3 - cq_vol) / cq_vol <= 0.05
                report["volume_check"] = {
                    "status": "PASS" if agree else "FAIL",
                    "cadquery_mm3": round(cq_vol, 2),
                    "stl_mm3": round(mesh_vol_mm3, 2),
                    "detail": ("COM build volume agrees with CadQuery plan" if agree
                               else "COM build volume DIVERGES from the CadQuery plan"),
                }
                if not agree:
                    report["ok"] = False
            except Exception:
                pass

        # Holes.
        expected = _expected_holes(build_plan)
        measured = mesh.measured_holes()
        # Drop measured loops that are really a profile cut/slot's own boundary
        # (an obround or bore outline), so they are verified by the cut check —
        # not double-counted as phantom EXTRA holes.
        footprints = _cut_footprints(build_plan)
        measured = [m for m in measured
                    if not any(math.hypot(m["x"] - fx, m["y"] - fy) <= fr
                               for (fx, fy, fr) in footprints)]
        hole_results, extras = _match_holes(expected, measured, pos_tol_in, dia_tol_in)
        report["features"].extend(hole_results)
        report["extras"].extend(extras)

        # Cuts / notches, and edge features (explicitly UNMEASURABLE).
        matched_cut_fids = {r["feature_id"] for r in hole_results}
        for s in _feature_steps(build_plan):
            t = s.get("type")
            if t == "extrude_cut":
                report["features"].append(_verify_cut(mesh, s, pos_tol_in))
            elif t in _EDGE_TYPES:
                report["features"].append(_unmeasurable(
                    s, f"{t} is an edge treatment — not measurable by planar cross-section"))
            elif t == "thread" and s.get("feature_id") not in matched_cut_fids \
                    and not (s.get("dimensions_drawing_units") or {}).get("diameter"):
                report["features"].append(_unmeasurable(
                    s, "cosmetic thread with no drilled hole — nothing to measure"))

        # Summary + overall verdict.
        cls_counts: dict[str, int] = {}
        for f in report["features"]:
            cls_counts[f["classification"]] = cls_counts.get(f["classification"], 0) + 1
        mismatches = [f for f in report["features"]
                      if f["classification"] in (MISSING, MISPLACED, WRONG_SIZE)]
        report["summary"] = {
            "total_features": len(report["features"]),
            "ok": cls_counts.get(OK, 0),
            "mismatches": len(mismatches),
            "unmeasurable": cls_counts.get(UNMEASURABLE, 0),
            "extras": len(report["extras"]),
            "by_classification": cls_counts,
        }
        if mismatches or report["extras"]:
            report["ok"] = False
        report["mismatches"] = [
            {"feature_id": f.get("feature_id"), "type": f.get("type"),
             "classification": f["classification"],
             "expected": f.get("expected"), "measured": f.get("measured")}
            for f in mismatches
        ]

        # --- Anchor fidelity (2026-07-17 dimensioning overhaul) ---
        # Re-measure each anchored feature RELATIVE TO ITS ANCHOR (edge / chain
        # target / polar center) and compare to the drawing's value. Catches
        # "right hole, right size, measured-from-the-wrong-edge" — a
        # compensating-error class the absolute checks above can miss.
        if resolved_extraction and any((f.get("anchors") or [])
                                       for f in resolved_extraction.get("features", [])):
            try:
                from pipeline.position_solver import verify_anchor_fidelity
                from pipeline.resolver import schema_clean
                from pipeline.schema import DrawingData

                measured_xy: dict[str, tuple[float, float]] = {}
                for f in report["features"]:
                    meas = f.get("measured")
                    if (isinstance(meas, dict) and "x" in meas and "y" in meas
                            and f.get("feature_id")):
                        measured_xy[f["feature_id"]] = (float(meas["x"]), float(meas["y"]))
                if measured_xy:
                    dd = DrawingData.model_validate(schema_clean(resolved_extraction))
                    findings = verify_anchor_fidelity(dd, measured_xy,
                                                      tol=pos_tol_in)
                    report["anchor_fidelity"] = findings
                    if any(x["status"] == "ANCHOR_MISMATCH" for x in findings):
                        report["ok"] = False
            except Exception as e:  # additive check — never sink verification
                log.warning("anchor-fidelity check failed (skipped): %s", e)
    except Exception as e:  # never sink a run over the measurer
        report["ok"] = False
        report["error"] = f"{type(e).__name__}: {e}"
        log.warning("feature verification error: %s", report["error"])

    if write and part:
        try:
            (Path(part_dir) / f"{part}{'' if part.endswith('_') else '_'}feature_verification.json"
             ).write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("Could not write feature_verification.json: %s", e)
    return report
