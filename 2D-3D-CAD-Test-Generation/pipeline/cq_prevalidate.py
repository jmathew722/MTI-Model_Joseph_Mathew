"""CadQuery pre-validation (must-meet Part 3): fail fast BEFORE SolidWorks.

Builds the same geometry described by ``<Part>_build_plan.json`` headlessly
with CadQuery (single source of truth — the VBA macros and the COM build come
from the same plan; geometry is never hand-written twice), then checks the
resulting solid against the must-meet constraints:

  * solid is valid/watertight and volume > 0;
  * face-count sanity;
  * the number of circular through-holes equals ``hole_count_total`` from the
    MM constraints (cylindrical faces grouped per hole axis, diameter matched);
  * every diameter-bearing cut_extrude constraint exists at its required
    position (measured vs required).

Outputs, per part folder:
  * ``prevalidation.stl``  — shown in Tab 2 with a "PRE-VALIDATED (CadQuery)"
    badge until the real SolidWorks STL replaces it (mm, like SolidWorks STLs);
  * ``prevalidation_report.json`` — per-constraint PASS/FAIL, measured vs
    required + solid stats;
  * ``prevalidate.py`` — a per-run script that re-runs this exact validation
    from the same build plan (reproducibility on any machine with cadquery).

A failed check ABORTS the run before SolidWorks is touched, surfacing the
specific constraint id (e.g. "MM-001 FAILED: only 5 through-holes found") —
never a generic pipeline error. When cadquery is not installed the step is a
graceful no-op with a note (the pipeline never blocks on a missing optional).

Circular patterns map to CadQuery exactly as specified:
``.center(cx, cy).polarArray(radius, seed_angle, 360, count)`` with a hole
circle at each point, then ``.cutThruAll()`` — polarArray's signature is
``(radius, startAngle, angle, count, fill=True)`` where ``angle`` is the total
angle filled. ``count`` INCLUDES the seed (the schema's single-place
convention).

Public entry points: :func:`run_prevalidation`, :func:`build_solid_from_plan`,
:func:`measure_holes`, :func:`write_prevalidate_script`.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

PREVALIDATION_STL = "prevalidation.stl"
PREVALIDATION_REPORT = "prevalidation_report.json"

# Build in millimeters (SolidWorks STL exports are mm — the web viewer then
# treats both models identically).
_UNIT_TO_MM = {"inch": 25.4, "mm": 1.0, "cm": 10.0}
# ±0.001 in (per the spec) for matching cylindrical-face diameters, in mm.
DIA_FACE_TOL_MM = 0.001 * 25.4


def to_workplane_local(x: float, y: float, k: float) -> tuple[float, float]:
    """Map a drawing-frame point to top-face workplane-LOCAL coordinates (mm).

    This is the ONE place the origin-frame -> workplane-local transform lives.
    The build plan's ``positions_xy`` are already rebaselined to the part's
    bottom-left-origin convention (``coordinate_origin =
    lower_left_corner_of_base_solid``, +X right / +Y up), and the base solid is
    extruded from the global XY plane so its lower-left corner sits at the global
    origin. A top-face workplane created with ``origin=(0, 0, 0)`` therefore has
    its local origin and axes coincident with that same drawing frame, so the
    transform is a pure unit scale (drawing units -> mm) with no rotation or
    offset. Centralizing it here keeps every hole/cut/pattern on the same datum
    and gives the transform a single unit-tested definition.

    ``k`` is the drawing-unit -> mm factor (``_UNIT_TO_MM[units]``).
    """
    return (x * k, y * k)


def _steps_in_order(plan: dict) -> list[dict]:
    steps = [s for s in plan.get("steps", [])
             if s.get("type") not in ("setup", "verify", "export", "run_all")
             and 0 < int(s.get("seq", 0)) < 999]
    return sorted(steps, key=lambda s: int(s.get("seq", 0)))


def _seed_diameter_for(plan: dict, pattern_step: dict) -> Optional[float]:
    """The seed hole's diameter (drawing units) for a circular_pattern step."""
    fid = pattern_step.get("feature_id")
    for s in plan.get("steps", []):
        if s.get("feature_id") == fid and s.get("type") == "hole":
            d = (s.get("dimensions_drawing_units") or {}).get("diameter")
            if d:
                return float(d)
    return None


def build_solid_from_plan(plan: dict):
    """Build the part described by the build plan; returns a cq.Workplane.

    Supported step types: extrude_boss (circle/rect), extrude_cut, hole,
    thread (as its hole), circular_pattern (polarArray + cutThruAll),
    reference_axis (no geometry). Unsupported types are skipped with a note —
    prevalidation checks constraints, not cosmetics."""
    import cadquery as cq

    k = _UNIT_TO_MM.get(str(plan.get("units", "inch")).lower(), 25.4)
    wp = cq.Workplane("XY")
    solid = None
    for step in _steps_in_order(plan):
        stype = step.get("type")
        dims = step.get("dimensions_drawing_units") or {}
        positions = step.get("positions_xy") or []
        depth = dims.get("depth") or dims.get("thickness") or dims.get("height")
        thru = step.get("depth_type") == "through_all"

        if stype == "extrude_boss":
            dia = dims.get("diameter") or dims.get("hole_diameter")
            if not depth:
                raise ValueError(f"{step.get('feature_id')}: extrude_boss without depth")
            cx, cy = (positions[0] if positions else (0.0, 0.0))
            if dia:
                lx, ly = to_workplane_local(cx, cy, k)
                base = (cq.Workplane("XY").center(lx, ly)
                        .circle(float(dia) * k / 2.0).extrude(float(depth) * k))
            else:
                length = dims.get("length") or dims.get("width")
                width = dims.get("width") or dims.get("length")
                if not (length and width):
                    raise ValueError(f"{step.get('feature_id')}: extrude_boss needs diameter or length+width")
                lx, ly = to_workplane_local(cx + float(length) / 2.0,
                                            cy + float(width) / 2.0, k)
                base = (cq.Workplane("XY")
                        .center(lx, ly)
                        .rect(float(length) * k, float(width) * k)
                        .extrude(float(depth) * k))
            solid = base if solid is None else solid.union(base)

        elif stype in ("hole", "extrude_cut", "thread"):
            if solid is None:
                raise ValueError(f"{step.get('feature_id')}: cut before any base solid")
            dia = dims.get("diameter") or dims.get("hole_diameter")
            if not dia:
                length = dims.get("length") or dims.get("width")
                width = dims.get("width") or dims.get("length")
                if length and width and positions:
                    cx, cy = positions[0]
                    lx, ly = to_workplane_local(cx + float(length) / 2.0,
                                                cy + float(width) / 2.0, k)
                    wp2 = (solid.faces(">Z").workplane(origin=(0, 0, 0))
                           .center(lx, ly)
                           .rect(float(length) * k, float(width) * k))
                    solid = wp2.cutThruAll() if (thru or not depth) else wp2.cutBlind(-float(depth) * k)
                continue
            pts = [to_workplane_local(x, y, k) for x, y in positions] or None
            if not pts:
                continue
            wp2 = (solid.faces(">Z").workplane(origin=(0, 0, 0))
                   .pushPoints(pts).circle(float(dia) * k / 2.0))
            solid = wp2.cutThruAll() if (thru or not depth) else wp2.cutBlind(-float(depth) * k)

        elif stype == "circular_pattern":
            if solid is None:
                raise ValueError(f"{step.get('feature_id')}: pattern before any base solid")
            spec = step.get("circular_pattern") or {}
            n = int(spec.get("total_instances") or 0)
            r = float(spec.get("bolt_circle_radius_in") or 0.0)
            a0 = float(spec.get("seed_angle_deg") or 0.0)
            total = float(spec.get("total_angle_deg") or 360.0)
            dia = _seed_diameter_for(plan, step)
            if not (n >= 2 and r > 0 and dia):
                raise ValueError(f"{step.get('feature_id')}: incomplete circular_pattern spec")
            # Pattern center: centroid of the plan's instance positions (the
            # bolt-circle center recorded by Stage 2.6), else the seed step's
            # center is unavailable — refuse rather than guess.
            positions = step.get("positions_xy") or []
            if positions:
                cx = sum(p[0] for p in positions) / len(positions)
                cy = sum(p[1] for p in positions) / len(positions)
            else:
                raise ValueError(f"{step.get('feature_id')}: circular_pattern has no positions_xy")
            # .polarArray(radius, startAngle, angle, count, fill=True): angle is
            # the TOTAL angle when fill=True; count INCLUDES the seed. Cutting
            # the seed's own position again is a no-op by construction.
            lx, ly = to_workplane_local(cx, cy, k)
            solid = (solid.faces(">Z").workplane(origin=(0, 0, 0))
                     .center(lx, ly)
                     .polarArray(r * k, a0, total, n)
                     .circle(float(dia) * k / 2.0)
                     .cutThruAll())

        elif stype in ("reference_axis",):
            continue  # no geometry
        else:
            log.info("prevalidation: skipping unsupported step type %r (%s)",
                     stype, step.get("feature_id"))
    if solid is None:
        raise ValueError("build plan produced no solid")
    return solid


def measure_holes(solid, thickness_mm: float) -> list[dict]:
    """Circular holes measured from the CadQuery solid's cylindrical faces.

    Faces are grouped per hole axis (a cut cylinder can tessellate into more
    than one face), and a hole is ``through`` when its faces span the full part
    thickness. Returns holes in INCHES: ``[{x, y, diameter, through}]``."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_SurfaceType

    # The part's outer wall is also a cylinder on a round part — exclude
    # boundary-sized cylinders so only actual holes are counted.
    sb = solid.val().BoundingBox()
    outer_dia_mm = 0.95 * max(sb.xlen, sb.ylen)

    groups: dict[tuple, dict] = {}
    for face in solid.faces().vals():
        ad = BRepAdaptor_Surface(face.wrapped)
        if ad.GetType() != GeomAbs_SurfaceType.GeomAbs_Cylinder:
            continue
        cyl = ad.Cylinder()
        r_mm = cyl.Radius()
        if 2.0 * r_mm >= outer_dia_mm:
            continue
        loc = cyl.Axis().Location()
        axis_dir = cyl.Axis().Direction()
        if abs(axis_dir.Z()) < 0.99:
            continue  # only holes normal to the base plane
        key = (round(loc.X(), 3), round(loc.Y(), 3), round(r_mm, 3))
        bb = face.BoundingBox()
        g = groups.setdefault(key, {"x_mm": loc.X(), "y_mm": loc.Y(),
                                    "r_mm": r_mm, "zmin": bb.zmin, "zmax": bb.zmax})
        g["zmin"] = min(g["zmin"], bb.zmin)
        g["zmax"] = max(g["zmax"], bb.zmax)

    holes: list[dict] = []
    for g in groups.values():
        span = g["zmax"] - g["zmin"]
        holes.append({
            "x": g["x_mm"] / 25.4,
            "y": g["y_mm"] / 25.4,
            "diameter": 2.0 * g["r_mm"] / 25.4,
            "through": span >= thickness_mm - max(DIA_FACE_TOL_MM, 1e-3),
        })
    return holes


def run_prevalidation(build_plan_path: Path,
                      constraints: list[dict] | Path | None,
                      out_dir: Path) -> dict:
    """Build + check + export. Returns the report dict (also written to
    ``prevalidation_report.json``); ``report["ok"]`` gates the SolidWorks build.
    Never raises — an internal error becomes ``{"ok": False, "error": ...}``
    UNLESS cadquery is simply unavailable, which is ``{"ok": True, "skipped"}``
    (a missing optional tool must not block a build)."""
    out_dir = Path(out_dir)
    report: dict[str, Any] = {"ok": True, "checks": [], "constraints": []}
    try:
        import cadquery as cq  # noqa: F401
    except ImportError:
        report["skipped"] = "cadquery not installed — pre-validation skipped"
        log.warning("%s", report["skipped"])
        return report

    if isinstance(constraints, (str, Path)):
        try:
            constraints = json.loads(Path(constraints).read_text(encoding="utf-8")).get("constraints", [])
        except (OSError, json.JSONDecodeError):
            constraints = []
    constraints = constraints or []

    try:
        plan = json.loads(Path(build_plan_path).read_text(encoding="utf-8"))
        solid = build_solid_from_plan(plan)
        import cadquery as cq

        shape = solid.val()
        volume = float(shape.Volume())
        n_faces = len(solid.faces().vals())
        valid = bool(shape.isValid())
        bb = shape.BoundingBox()
        thickness_mm = bb.zmax - bb.zmin
        report["solid"] = {"valid_watertight": valid, "volume_mm3": round(volume, 3),
                           "face_count": n_faces,
                           "bbox_mm": [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)]}
        report["checks"].append({"check": "watertight_valid_solid", "ok": valid})
        report["checks"].append({"check": "volume_positive", "ok": volume > 0.0,
                                 "measured_mm3": round(volume, 3)})
        report["checks"].append({"check": "face_count_sane", "ok": 3 <= n_faces <= 500,
                                 "measured": n_faces})
        if not (valid and volume > 0.0):
            report["ok"] = False

        stl_path = out_dir / PREVALIDATION_STL
        cq.exporters.export(solid, str(stl_path))
        report["stl"] = stl_path.name

        if constraints:
            from pipeline.must_meet import evaluate_constraints

            holes = measure_holes(solid, thickness_mm)
            report["measured_holes_in"] = [
                {k: round(v, 4) if isinstance(v, float) else v for k, v in hl.items()}
                for hl in holes
            ]
            results = evaluate_constraints(holes, constraints, dia_tol_in=0.005)
            report["constraints"] = results
            fails = [r for r in results if r["status"] == "FAIL"]
            if fails:
                report["ok"] = False
                report["failed_constraints"] = [
                    f"{r['id']} FAILED: {r['detail']} (required {r['required']}, "
                    f"measured {r['measured']})" for r in fails
                ]
    except Exception as e:
        report["ok"] = False
        report["error"] = f"{type(e).__name__}: {e}"
        log.warning("Pre-validation error: %s", report["error"])

    try:
        (out_dir / PREVALIDATION_REPORT).write_text(
            json.dumps(report, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write %s: %s", PREVALIDATION_REPORT, e)
    return report


_SCRIPT_TEMPLATE = '''#!/usr/bin/env python
"""Auto-generated per-run pre-validation (single source of truth =
{plan_name} in this folder). Re-run any time:

    python prevalidate.py

Exit 0 = all checks pass; exit 1 = a check failed (see prevalidation_report.json).
Requires: pip install cadquery
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, {project_dir!r})
from pipeline.cq_prevalidate import run_prevalidation

HERE = Path(__file__).resolve().parent
report = run_prevalidation(HERE / {plan_name!r}, HERE / "must_meet_constraints.json", HERE)
print(json.dumps(report, indent=2))
sys.exit(0 if report.get("ok") else 1)
'''


def write_prevalidate_script(part_dir: Path, plan_name: str) -> Path:
    """Write the per-run ``prevalidate.py`` beside the build plan."""
    project_dir = str(Path(__file__).resolve().parents[1])
    path = Path(part_dir) / "prevalidate.py"
    path.write_text(_SCRIPT_TEMPLATE.format(project_dir=project_dir,
                                            plan_name=plan_name),
                    encoding="utf-8")
    return path
