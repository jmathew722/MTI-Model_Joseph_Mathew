"""Post-build must-meet verification (Part 4): measure the REAL SolidWorks STL.

After the COM build exports the part's STL, the mesh is measured with trimesh
and every MM constraint is re-graded against the built geometry — the same
checks the CadQuery pre-validation ran, now against what SolidWorks actually
produced:

  * hole count (circular through-holes, grouped per axis);
  * bore / cut diameters (circles fitted to the hole boundaries of planar
    cross-sections);
  * through-all verification (the hole appears in sections near BOTH faces);
  * spacing between cut centers (e.g. the 1.25 cut at 2.94 from the bore).

Writes ``constraint_verification.json``: every MM-xxx constraint -> PASS/FAIL
with measured vs required values. A run is only SUCCESS when every MM
constraint passes; each failure is appended to the lessons-learned JSONL with
the constraint, the responsible generated-VBA snippet, and the fix guidance —
so repeated pattern failures train the codegen.

Public entry point: :func:`verify_constraints_stl`.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

CONSTRAINT_VERIFICATION = "constraint_verification.json"

# Scale candidates: SolidWorks STL exports default to meters or millimeters
# depending on export options; CadQuery's prevalidation STL is mm.
_SCALES_TO_IN = {"mm": 1.0 / 25.4, "m": 1.0 / 0.0254, "inch": 1.0}
# Mesh measurements are tessellated — tolerances are looser than the CAD-side
# prevalidation (chord deviation shrinks measured radii slightly).
DIA_TOL_IN = 0.02
POS_TOL_IN = 0.05
_MIN_CIRCLE_PTS = 8
_CIRCULARITY = 0.15  # std(radii)/mean(radii) below this = a circle


def _pick_scale(extents_raw, expected_thickness_in: Optional[float]) -> float:
    """Unit scale (raw -> inches), chosen by matching the expected thickness
    when given, else by a plausible part-size heuristic."""
    tmin_raw = min(extents_raw)
    if expected_thickness_in and expected_thickness_in > 0:
        best, best_err = 1.0 / 25.4, float("inf")
        for s in _SCALES_TO_IN.values():
            err = abs(tmin_raw * s - expected_thickness_in) / expected_thickness_in
            if err < best_err:
                best, best_err = s, err
        return best
    for s in _SCALES_TO_IN.values():
        if 0.05 <= max(extents_raw) * s <= 120.0:
            return s
    return 1.0 / 25.4


def _loops_at(mesh, axis: int, coord: float) -> list:
    """Closed polyline loops of the cross-section at ``coord`` along ``axis``."""
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    origin = [0.0, 0.0, 0.0]
    origin[axis] = coord
    section = mesh.section(plane_origin=origin, plane_normal=normal)
    if section is None:
        return []
    try:
        return [d for d in section.discrete if len(d) >= _MIN_CIRCLE_PTS]
    except Exception:
        return []


def _fit_circles(loops: list, axis: int) -> list[dict]:
    """Fit a circle to each closed loop; returns in-plane circles with signed
    area (largest |area| loop = the outer boundary, not a hole)."""
    u, v = [(1, 2), (0, 2), (0, 1)][axis]
    out = []
    for pts in loops:
        xs = [p[u] for p in pts]
        ys = [p[v] for p in pts]
        n = len(pts)
        # Shoelace area (loop is closed: last point repeats the first).
        area = 0.0
        for i in range(n - 1):
            area += xs[i] * ys[i + 1] - xs[i + 1] * ys[i]
        area = abs(area) / 2.0
        cx, cy = sum(xs) / n, sum(ys) / n
        radii = [math.hypot(x - cx, y - cy) for x, y in zip(xs, ys)]
        mean_r = sum(radii) / n
        if mean_r <= 0:
            continue
        std_r = (sum((r - mean_r) ** 2 for r in radii) / n) ** 0.5
        out.append({"x": cx, "y": cy, "r": mean_r, "area": area,
                    "circular": (std_r / mean_r) <= _CIRCULARITY})
    return out


def measure_holes_from_stl(stl_path: Path,
                           expected_thickness_in: Optional[float] = None
                           ) -> tuple[list[dict], float]:
    """Measure circular through-holes from an STL. Returns
    ``([{x, y, diameter, through}], thickness_in)`` — all inches, in the
    section plane's 2D frame (consistent across holes, which is all the
    constraint checks compare)."""
    import trimesh

    mesh = trimesh.load(str(stl_path), force="mesh")
    ext = mesh.extents
    scale = _pick_scale(ext, expected_thickness_in)
    axis = int(min(range(3), key=lambda i: ext[i]))  # thickness axis
    lo = float(mesh.bounds[0][axis])
    hi = float(mesh.bounds[1][axis])
    thickness_in = (hi - lo) * scale

    def circles_at(frac: float) -> list[dict]:
        loops = _loops_at(mesh, axis, lo + (hi - lo) * frac)
        circles = _fit_circles(loops, axis)
        if not circles:
            return []
        outer = max(circles, key=lambda c: c["area"])
        return [c for c in circles if c is not outer and c["circular"]]

    mid = circles_at(0.5)
    near = circles_at(0.06)
    far = circles_at(0.94)

    def present(c: dict, group: list[dict]) -> bool:
        for o in group:
            if (math.hypot(c["x"] - o["x"], c["y"] - o["y"]) * scale <= POS_TOL_IN
                    and abs(c["r"] - o["r"]) / c["r"] <= 0.15):
                return True
        return False

    holes = []
    for c in mid:
        holes.append({
            "x": c["x"] * scale,
            "y": c["y"] * scale,
            "diameter": 2.0 * c["r"] * scale,
            "through": present(c, near) and present(c, far),
        })
    return holes, thickness_in


def _responsible_vba_snippet(constraint: dict, build_plan_path: Optional[Path],
                             part_dir: Path) -> str:
    """Best-effort: the generated VBA lines responsible for this constraint
    (for the lessons-learned record)."""
    try:
        if build_plan_path is None or not Path(build_plan_path).is_file():
            return ""
        plan = json.loads(Path(build_plan_path).read_text(encoding="utf-8"))
        want = ("circular_pattern",) if constraint.get("type") == "circular_pattern" \
            else ("hole", "extrude_cut")
        for step in plan.get("steps", []):
            if step.get("type") in want and step.get("macro_file", "").endswith(".vba"):
                macro = Path(part_dir) / "macros" / step["macro_file"]
                if macro.is_file():
                    lines = macro.read_text(encoding="utf-8", errors="replace").splitlines()
                    hits = [ln.strip() for ln in lines
                            if ("CreateCircularPatternSafe" in ln or "FeatureCut4" in ln
                                or "CreateCircleByRadius" in ln)]
                    if hits:
                        return f"{step['macro_file']}: " + " | ".join(hits[:3])
        return ""
    except Exception:
        return ""


def verify_constraints_stl(
    stl_path: Path,
    constraints: list[dict],
    part_dir: Path,
    *,
    part: str = "",
    expected_thickness_in: Optional[float] = None,
    build_plan_path: Optional[Path] = None,
    lessons_path: Optional[Path] = None,
) -> dict:
    """Grade every MM constraint against the built STL and write
    ``constraint_verification.json``. Never raises; an internal error becomes
    ``{"ok": False, "error": ...}`` so the caller can gate READY on it."""
    report: dict[str, Any] = {"ok": True, "stl": Path(stl_path).name, "constraints": []}
    part_dir = Path(part_dir)
    if not constraints:
        report["note"] = "no must-meet constraints for this run"
        _write(report, part_dir)
        return report
    try:
        from pipeline.must_meet import append_lesson, evaluate_constraints

        holes, thickness_in = measure_holes_from_stl(Path(stl_path), expected_thickness_in)
        report["measured_thickness_in"] = round(thickness_in, 4)
        report["measured_holes_in"] = [
            {k: (round(v, 4) if isinstance(v, float) else v) for k, v in hl.items()}
            for hl in holes
        ]
        results = evaluate_constraints(holes, constraints,
                                       dia_tol_in=DIA_TOL_IN, pos_tol_in=POS_TOL_IN)
        report["constraints"] = results
        fails = [r for r in results if r["status"] == "FAIL"]
        if fails:
            report["ok"] = False
            report["failed_constraints"] = [
                f"{r['id']} FAILED: {r['detail']} (required {r['required']}, "
                f"measured {r['measured']})" for r in fails
            ]
            if lessons_path is not None:
                by_id = {c.get("id"): c for c in constraints}
                for r in fails:
                    append_lesson(lessons_path, {
                        "kind": "post_build_constraint_failure",
                        "part": part,
                        "constraint_id": r["id"],
                        "constraint": by_id.get(r["id"], {}),
                        "required": r["required"],
                        "measured": r["measured"],
                        "detail": r["detail"],
                        "vba_snippet": _responsible_vba_snippet(
                            by_id.get(r["id"], {}), build_plan_path, part_dir),
                        "fix_applied": ("surfaced as a precise MM failure in the UI "
                                        "checklist; human confirms the drawing/spec "
                                        "disagreement before re-run"),
                    })
    except Exception as e:
        report["ok"] = False
        report["error"] = f"{type(e).__name__}: {e}"
        log.warning("Post-build constraint verification error: %s", report["error"])
    _write(report, part_dir)
    return report


def _write(report: dict, part_dir: Path) -> None:
    try:
        (Path(part_dir) / CONSTRAINT_VERIFICATION).write_text(
            json.dumps(report, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write %s: %s", CONSTRAINT_VERIFICATION, e)
