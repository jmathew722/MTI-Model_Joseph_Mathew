"""Macro echo check — generation-time round-trip verification (2026-07-12, Task 1).

The single highest-value Stage-7 addition. After the macros are generated, parse
every emitted geometry literal back out of the VBA and prove it equals the
build-plan value for the SAME feature that emitted it, after unit conversion.
Plan → VBA → parse → compare, per step, at generation time.

This converts every Stage-7 translation bug from a build-time mystery on the
SolidWorks machine into an instant, named generation failure. It catches, by
construction:

* **cross-contamination** — a coordinate/dimension literal in feature F's macro
  that matches a DIFFERENT feature's build-plan value and not F's own (the
  158-C "corner-array coordinates in the wrong macro" class);
* **orphan literals** — a geometry literal that maps to no plan value anywhere;
* **missing values** — a build-plan position/size that never made it into the
  macro that should carry it.

Parsing is ANCHORED to the known call signatures the templates emit (circle,
corner-rectangle, slot line, slot-fillet corner + radius), never a scan of
arbitrary VBA — so structural constants (``0#``, ``2#``, ``0.01``) are never
mistaken for data.

Public entry point: :func:`check_macro_echo`, raising :class:`MacroEchoError`
on any discrepancy. Wired into ``generate_macro_package`` right after the static
audit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Drawing-unit tolerance: literals are formatted with %.6g, so a real value
# round-trips to ~6 significant figures. 1e-3 drawing units (0.001") is far
# tighter than any geometry error while absorbing formatting.
TOL_DRAWING = 1e-3
# Meters tolerance: slot literals are written as %.6f meters directly.
TOL_METERS = 5e-6

# Feature step types whose macros carry coordinate/size literals we can map
# 1:1 to the build plan. Scaffolding (setup/verify/export/run_all), interactive
# fillet/chamfer, reference axes, and the angle-driven circular-pattern step are
# out of scope (they carry no directly-mappable coordinate literals).
_IN_SCOPE = {"extrude_boss", "extrude_cut", "hole", "thread",
             "slot_rect_cut", "slot_corner_fillet"}

# --- signature-anchored parsers (extract ONLY the meaningful operands) --------
_NUM = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
_CIRCLE_RE = re.compile(
    rf"CreateCircleByRadius\s+{_NUM}\s*\*\s*UNIT_FACTOR,\s*{_NUM}\s*\*\s*UNIT_FACTOR,"
    rf"\s*0#,\s*\(\s*{_NUM}\s*/\s*2#\s*\)\s*\*\s*UNIT_FACTOR")
_RECT_RE = re.compile(
    rf"CreateCornerRectangle\s+{_NUM}\s*\*\s*UNIT_FACTOR,\s*{_NUM}\s*\*\s*UNIT_FACTOR,"
    rf"\s*0#,\s*_\s*\(\s*{_NUM}\s*\+\s*{_NUM}\s*\)\s*\*\s*UNIT_FACTOR,"
    rf"\s*\(\s*{_NUM}\s*\+\s*{_NUM}\s*\)\s*\*\s*UNIT_FACTOR", re.DOTALL)
# Slot rectangle: 4 CreateLine calls in METERS (literals written directly).
_LINE_RE = re.compile(
    rf"CreateLine\s+{_NUM},\s*{_NUM},\s*0#,\s*{_NUM},\s*{_NUM},\s*0#")
# Slot fillet: target corners as Array(x, y) in METERS + rMeters = R * UNIT_FACTOR.
_ARRAY_RE = re.compile(rf"Array\(\s*{_NUM},\s*{_NUM}\s*\)")
_RMETERS_RE = re.compile(rf"rMeters\s*=\s*{_NUM}\s*\*\s*UNIT_FACTOR")


class MacroEchoError(Exception):
    """A generated macro literal does not round-trip to the build plan."""


@dataclass
class EchoIssue:
    kind: str          # cross_contamination | orphan_literal | missing_value
    feature_id: str
    field: str
    expected: Any
    found: Any
    detail: str = ""

    def __str__(self) -> str:
        return (f"[{self.kind}] {self.feature_id} {self.field}: "
                f"expected {self.expected}, found {self.found}"
                + (f" — {self.detail}" if self.detail else ""))


@dataclass
class EchoReport:
    issues: list[EchoIssue] = field(default_factory=list)
    checked_files: int = 0
    checked_literals: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _pair_in(pairs: list[tuple[float, float]], x: float, y: float, tol: float) -> bool:
    return any(_close(px, x, tol) and _close(py, y, tol) for px, py in pairs)


def _val_in(vals: list[float], v: float, tol: float) -> bool:
    return any(_close(u, v, tol) for u in vals)


def _step_positions(step) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in (getattr(step, "positions_xy", None) or []):
        if len(p) == 2:
            out.append((float(p[0]), float(p[1])))
    return out


def _step_positions_m(step) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in (getattr(step, "positions_xy_meters", None) or []):
        if len(p) == 2:
            out.append((float(p[0]), float(p[1])))
    return out


def _step_dims(step) -> list[float]:
    return [float(v) for v in (getattr(step, "dimensions", None) or {}).values()]


def check_macro_echo(pkg, macros_dir: Optional[Path] = None) -> EchoReport:
    """Round-trip every in-scope generated macro against the build plan.

    ``pkg`` is the :class:`~pipeline.macro_generator.MacroPackage`. Returns an
    :class:`EchoReport`; callers raise :class:`MacroEchoError` on ``not ok``.
    """
    macros_dir = Path(macros_dir or pkg.macros_dir)
    report = EchoReport()

    steps = [s for s in pkg.steps if str(getattr(s, "macro_file", "")).endswith(".vba")]
    by_file: dict[str, Any] = {s.macro_file: s for s in steps}

    # Global pools for cross-contamination attribution (every feature's values).
    pos_pool: dict[str, list[tuple[float, float]]] = {}
    pos_pool_m: dict[str, list[tuple[float, float]]] = {}
    dim_pool: dict[str, list[float]] = {}
    for s in steps:
        pos_pool.setdefault(s.feature_id, []).extend(_step_positions(s))
        pos_pool_m.setdefault(s.feature_id, []).extend(_step_positions_m(s))
        dim_pool.setdefault(s.feature_id, []).extend(_step_dims(s))

    def _blame_pair(fid: str, x: float, y: float, tol: float, pool: dict) -> Optional[str]:
        for other, pairs in pool.items():
            if other == fid:
                continue
            if _pair_in(pairs, x, y, tol):
                return other
        return None

    def _blame_val(fid: str, v: float, tol: float) -> Optional[str]:
        for other, vals in dim_pool.items():
            if other == fid:
                continue
            if _val_in(vals, v, tol):
                return other
        return None

    for fname, step in by_file.items():
        if step.feature_type not in _IN_SCOPE:
            continue
        path = macros_dir / fname
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        report.checked_files += 1
        fid = step.feature_id
        own_pos = _step_positions(step)
        own_pos_m = _step_positions_m(step)
        own_dims = _step_dims(step)
        seen_pos: list[tuple[float, float]] = []
        seen_pos_m: list[tuple[float, float]] = []

        # --- circles: center (drawing) must be one of this step's positions,
        #     diameter one of its dimensions ------------------------------------
        for m in _CIRCLE_RE.finditer(text):
            cx, cy, dia = float(m.group(1)), float(m.group(2)), float(m.group(3))
            report.checked_literals += 1
            seen_pos.append((cx, cy))
            if not _pair_in(own_pos, cx, cy, TOL_DRAWING):
                blame = _blame_pair(fid, cx, cy, TOL_DRAWING, pos_pool)
                report.issues.append(EchoIssue(
                    "cross_contamination" if blame else "orphan_literal", fid,
                    "circle_center", own_pos, (cx, cy),
                    f"matches feature {blame}'s position, not {fid}'s" if blame
                    else "center matches no planned position"))
            if own_dims and not _val_in(own_dims, dia, TOL_DRAWING):
                blame = _blame_val(fid, dia, TOL_DRAWING)
                report.issues.append(EchoIssue(
                    "cross_contamination" if blame else "orphan_literal", fid,
                    "diameter", own_dims, dia,
                    f"matches feature {blame}'s dimension" if blame
                    else "diameter matches no planned dimension"))

        # --- rectangle: lower-left corner (drawing) + length/width -------------
        for m in _RECT_RE.finditer(text):
            cx, cy = float(m.group(1)), float(m.group(2))
            length, width = float(m.group(4)), float(m.group(6))
            report.checked_literals += 1
            seen_pos.append((cx, cy))
            if own_pos and not _pair_in(own_pos, cx, cy, TOL_DRAWING):
                blame = _blame_pair(fid, cx, cy, TOL_DRAWING, pos_pool)
                report.issues.append(EchoIssue(
                    "cross_contamination" if blame else "orphan_literal", fid,
                    "rect_corner", own_pos, (cx, cy),
                    f"matches feature {blame}'s position" if blame
                    else "corner matches no planned position"))
            for label, val in (("length", length), ("width", width)):
                if own_dims and not _val_in(own_dims, val, TOL_DRAWING):
                    blame = _blame_val(fid, val, TOL_DRAWING)
                    report.issues.append(EchoIssue(
                        "cross_contamination" if blame else "orphan_literal", fid,
                        label, own_dims, val,
                        f"matches feature {blame}'s dimension" if blame
                        else f"{label} matches no planned dimension"))

        # --- slot rectangle lines (meters): every endpoint must be a corner ----
        if step.feature_type == "slot_rect_cut" and own_pos_m:
            for m in _LINE_RE.finditer(text):
                pts = [(float(m.group(1)), float(m.group(2))),
                       (float(m.group(3)), float(m.group(4)))]
                report.checked_literals += 1
                for (x, y) in pts:
                    seen_pos_m.append((x, y))
                    if not _pair_in(own_pos_m, x, y, TOL_METERS):
                        blame = _blame_pair(fid, x, y, TOL_METERS, pos_pool_m)
                        report.issues.append(EchoIssue(
                            "cross_contamination" if blame else "orphan_literal", fid,
                            "slot_line_point", own_pos_m, (x, y),
                            f"matches feature {blame}'s corner" if blame
                            else "line endpoint matches no slot corner"))

        # --- slot fillet corners (meters, Array()) + radius --------------------
        if step.feature_type == "slot_corner_fillet":
            for m in _ARRAY_RE.finditer(text):
                x, y = float(m.group(1)), float(m.group(2))
                report.checked_literals += 1
                seen_pos_m.append((x, y))
                if own_pos_m and not _pair_in(own_pos_m, x, y, TOL_METERS):
                    blame = _blame_pair(fid, x, y, TOL_METERS, pos_pool_m)
                    report.issues.append(EchoIssue(
                        "cross_contamination" if blame else "orphan_literal", fid,
                        "fillet_corner", own_pos_m, (x, y),
                        f"matches feature {blame}'s corner" if blame
                        else "fillet corner matches no planned corner"))
            rm = _RMETERS_RE.search(text)
            if rm is not None and own_dims:
                r = float(rm.group(1))
                if not _val_in(own_dims, r, TOL_DRAWING):
                    report.issues.append(EchoIssue(
                        "orphan_literal", fid, "fillet_radius", own_dims, r,
                        "radius matches no planned dimension"))

        # --- missing: every planned position must appear as a literal ---------
        # Gate by coordinate frame: circle/rect features emit drawing-unit
        # literals; slot features emit meters literals. Only enforce the frame
        # the macro actually uses (else the other frame's positions read as
        # spuriously missing).
        if step.feature_type in ("extrude_boss", "extrude_cut", "hole", "thread"):
            for (x, y) in own_pos:
                if not _pair_in(seen_pos, x, y, TOL_DRAWING):
                    report.issues.append(EchoIssue(
                        "missing_value", fid, "position", (x, y), None,
                        "planned position never emitted as a geometry literal"))
        elif step.feature_type in ("slot_rect_cut", "slot_corner_fillet"):
            for (x, y) in own_pos_m:
                if not _pair_in(seen_pos_m, x, y, TOL_METERS):
                    report.issues.append(EchoIssue(
                        "missing_value", fid, "position_m", (x, y), None,
                        "planned corner never emitted as a geometry literal"))

    return report


def assert_macro_echo(pkg, macros_dir: Optional[Path] = None) -> EchoReport:
    """Run :func:`check_macro_echo` and raise :class:`MacroEchoError` on any
    discrepancy (feature id, field, expected, found in the message)."""
    report = check_macro_echo(pkg, macros_dir)
    if not report.ok:
        detail = "; ".join(str(i) for i in report.issues[:20])
        raise MacroEchoError(
            f"Macro echo check failed ({len(report.issues)} issue(s) across "
            f"{report.checked_files} macro(s)): {detail}")
    return report
