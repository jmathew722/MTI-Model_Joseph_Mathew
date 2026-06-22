"""Build-readiness validation & verification report (Phase 1 gate).

:mod:`pipeline.schema` guarantees the *shape* of the data (types, enums, positive
dimensions, confidence range). This module enforces the cross-field *business
rules* that determine whether the data can actually drive a SolidWorks build:

  * completeness (features exist, build order non-empty)
  * geometry sanity (no zero/negative dims)
  * build-order / dependency integrity
  * base-feature-first rule
  * unit consistency
  * sketch definability heuristic
  * ambiguous dimensions requiring human resolution        (v2)
  * dimensional closure of extracted dimension chains       (v2)
  * pattern envelope feasibility (seed + spacing fits part) (v2)
  * REF (reference) dimensions not used as driving dims     (v2)

Outputs both a machine ``ValidationReport`` and the human ``VERIFICATION
REPORT`` text (spec format) with an overall READY TO BUILD / BLOCKED status.

If the status is BLOCKED, the pipeline must NOT generate macros or touch
SolidWorks.

Public entry points: :func:`run_verification`, :func:`format_verification_report`,
:func:`validate_drawing_data` (raising wrapper kept for the COM build path).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from pydantic import ValidationError

from pipeline.schema import DrawingData, FeatureType, PatternKind
from utils.logger import get_logger

log = get_logger()

# Feature types that create a solid base body the rest of the part builds on.
BASE_FEATURE_TYPES = {FeatureType.EXTRUDE_BOSS, FeatureType.REVOLVE}

# Feature types that are sketch-based and therefore need dimensions to define a profile.
SKETCH_FEATURE_TYPES = {
    FeatureType.EXTRUDE_BOSS,
    FeatureType.EXTRUDE_CUT,
    FeatureType.REVOLVE,
}

# Minimum number of dimensions needed to define a basic closed profile.
MIN_PROFILE_DIMENSIONS = 1


class DrawingValidationError(Exception):
    """Raised when extracted data fails build-readiness validation."""

    def __init__(self, report: "ValidationReport"):
        self.report = report
        super().__init__(str(report))


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # v2 bookkeeping for the human-readable verification report:
    ambiguous_dimension_ids: list[str] = field(default_factory=list)
    failed_closure_chains: list[str] = field(default_factory=list)
    feasibility_issues: list[str] = field(default_factory=list)
    unit_consistency_ok: bool = True
    view_consistency_ok: bool = True
    readiness: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def __str__(self) -> str:
        lines = []
        if self.errors:
            lines.append("Validation FAILED:")
            lines.extend(f"  [ERROR] {e}" for e in self.errors)
        else:
            lines.append("Validation PASSED.")
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"  [WARN]  {w}" for w in self.warnings)
        return "\n".join(lines)


def _coerce(data: Union[DrawingData, dict[str, Any]]) -> DrawingData:
    """Accept either a DrawingData or a raw dict; validate shape first."""
    if isinstance(data, DrawingData):
        return data
    return DrawingData.model_validate(data)


# --------------------------------------------------------------------------- #
# Individual check groups
# --------------------------------------------------------------------------- #
def _check_completeness(model: DrawingData, report: ValidationReport) -> None:
    if not model.features:
        report.error("No features found — nothing to build.")
    if not model.build_order:
        report.error("build_order is empty — no build sequence defined.")


def _check_geometry_sanity(model: DrawingData, report: ValidationReport) -> None:
    # Schema already enforces > 0, but re-check defensively for raw dicts.
    for d in model.dimensions:
        if d.value <= 0:
            report.error(f"Dimension {d.id} has non-positive value {d.value}.")


def _check_build_order(model: DrawingData, report: ValidationReport) -> None:
    feature_ids = {f.id for f in model.features}
    for fid in model.build_order:
        if fid not in feature_ids:
            report.error(f"build_order references unknown feature id {fid!r}.")
    for fid in feature_ids:
        if fid not in model.build_order:
            report.warn(f"Feature {fid} is not in build_order and will not be built.")

    if model.build_order:
        first_id = model.build_order[0]
        first = model.feature_by_id(first_id)
        if first is None:
            report.error(f"First build_order id {first_id!r} is not a known feature.")
        elif first.type not in BASE_FEATURE_TYPES:
            report.error(
                f"First feature {first_id} is {first.type.value!r}; SolidWorks needs a "
                f"solid base ({', '.join(t.value for t in BASE_FEATURE_TYPES)}) first."
            )


def _check_dependencies(model: DrawingData, report: ValidationReport) -> None:
    dim_ids = {d.id for d in model.dimensions}
    feature_ids = {f.id for f in model.features}
    for f in model.features:
        for ref in f.related_dimensions:
            if ref not in dim_ids:
                report.error(f"Feature {f.id} references unknown dimension {ref!r}.")
        if f.depth_dimension_id and f.depth_dimension_id not in dim_ids:
            report.error(
                f"Feature {f.id} depth_dimension_id {f.depth_dimension_id!r} does not exist."
            )
        if f.parent_feature and f.parent_feature not in feature_ids:
            report.warn(f"Feature {f.id} parent_feature {f.parent_feature!r} does not exist.")
    for h in model.hole_callouts:
        if h.feature_ref and h.feature_ref not in feature_ids:
            report.warn(f"Hole callout {h.id} feature_ref {h.feature_ref!r} does not exist.")


def _check_unit_consistency(model: DrawingData, report: ValidationReport) -> None:
    mismatched = sorted({d.unit.value for d in model.dimensions if d.unit != model.units})
    if mismatched:
        report.unit_consistency_ok = False
        report.error(
            f"Mixed units: drawing declares {model.units.value!r} but dimensions use "
            f"{mismatched}. Normalize units before building."
        )


def _check_sketch_definability(model: DrawingData, report: ValidationReport) -> None:
    for f in model.features:
        if f.type not in SKETCH_FEATURE_TYPES:
            continue
        # A revolve carries its geometry as an explicit half-profile (>=2 points),
        # not as related dimensions — that is sufficient to define its sketch.
        if f.type == FeatureType.REVOLVE and len(f.revolve_profile) >= 2:
            continue
        n = len(f.related_dimensions)
        if n < MIN_PROFILE_DIMENSIONS:
            report.error(
                f"Sketch feature {f.id} ({f.type.value}) has {n} related dimension(s); "
                f"need at least {MIN_PROFILE_DIMENSIONS} to define a profile."
            )


def _check_mirror_features(model: DrawingData, report: ValidationReport) -> None:
    """A mirror feature must name the feature it mirrors (parent_feature) and a
    mirror plane; otherwise it cannot be built (warning — non-strict skips it)."""
    feature_ids = {f.id for f in model.features}
    for f in model.features:
        if f.type != FeatureType.MIRROR:
            continue
        if not f.parent_feature or f.parent_feature not in feature_ids:
            report.warn(
                f"Mirror feature {f.id} has no valid parent_feature (the feature to mirror); "
                f"it cannot be auto-built and will be left for manual modeling."
            )
        if not (f.mirror_plane or f.sketch_plane):
            report.warn(f"Mirror feature {f.id} has no mirror plane; defaulting to the Front Plane.")


def _check_ambiguity(model: DrawingData, report: ValidationReport) -> None:
    """v2: any dimension flagged as requiring resolution blocks the build."""
    for d in model.dimensions:
        if d.resolution_required or d.value_unclear:
            report.ambiguous_dimension_ids.append(d.id)
        if d.resolution_required:
            candidates = ", ".join(f"{v:g}" for v in d.possible_values) or f"{d.value:g} (best guess)"
            report.error(
                f"Dimension {d.id} requires human resolution "
                f"({d.ambiguity_reason or 'ambiguous'}). Candidate values: {candidates}."
            )
        elif d.value_unclear:
            report.warn(
                f"Dimension {d.id} value is unclear ({d.ambiguity_reason or 'no reason given'}); "
                f"using best guess {d.value:g}."
            )


def _closure_slack(model: DrawingData, dim_ids: list[str], total_value: float) -> float:
    """Allowed closure mismatch: the involved dims' own tolerances, with a floor."""
    tol_sum = 0.0
    for did in dim_ids:
        d = model.dimension_by_id(did)
        if d is not None:
            tol_sum += abs(d.tolerance_plus) + abs(d.tolerance_minus)
    # Floor: 0.1% of the total — covers chains with no explicit tolerances.
    return max(tol_sum, 1e-3 * abs(total_value))


def _check_dimensional_closure(model: DrawingData, report: ValidationReport) -> None:
    """v2: every extracted dimension chain must arithmetically close."""
    for chain in model.relationships.dimension_chains:
        total = model.dimension_by_id(chain.total_dimension_id)
        if total is None:
            report.warn(
                f"Dimension chain references unknown total {chain.total_dimension_id!r}; skipping."
            )
            continue
        components = []
        missing = []
        for cid in chain.component_dimension_ids:
            c = model.dimension_by_id(cid)
            (components if c is not None else missing).append(c if c is not None else cid)
        if missing:
            report.warn(
                f"Dimension chain for {total.id} references unknown components {missing}; skipping."
            )
            continue
        comp_sum = sum(c.value for c in components)
        slack = _closure_slack(
            model, [total.id, *chain.component_dimension_ids], total.value
        )
        if abs(comp_sum - total.value) > slack:
            label = f"{total.id} = {' + '.join(c.id for c in components)}"
            report.failed_closure_chains.append(label)
            report.error(
                f"Dimensional closure FAILED: {label} → "
                f"{comp_sum:g} != {total.value:g} (allowed slack {slack:g} {model.units.value})."
            )


def _check_pattern_envelopes(model: DrawingData, report: ValidationReport) -> None:
    """v2: seed + (qty-1) x spacing must fit inside the part envelope."""
    envelope_values = [d.value for d in model.dimensions if d.is_envelope]
    max_envelope = max(envelope_values, default=0.0)

    def _check(span: float, qty: int, spacing: float, label: str) -> None:
        if qty < 2 or spacing <= 0:
            return
        if max_envelope <= 0:
            report.warn(
                f"{label}: pattern span {span:g} could not be checked — no envelope "
                "dimension (length/width/height) extracted."
            )
            return
        if span > max_envelope:
            report.feasibility_issues.append(label)
            report.error(
                f"Pattern infeasible: {label} spans {span:g} {model.units.value} "
                f"({qty} x {spacing:g} spacing) but the largest envelope dimension is "
                f"{max_envelope:g} {model.units.value}."
            )

    for h in model.hole_callouts:
        if h.pattern != PatternKind.NONE:
            span = (h.qty - 1) * h.pattern_spacing
            _check(span, h.qty, h.pattern_spacing, f"hole callout {h.id}")
    for s in model.relationships.equal_spacing:
        span = (s.qty - 1) * s.spacing_value
        _check(span, s.qty, s.spacing_value, f"equal-spacing note for {s.feature_ref}")


def _check_reference_dimensions(model: DrawingData, report: ValidationReport) -> None:
    """v2: REF dimensions are non-controlling — they must not drive features."""
    ref_ids = {d.id for d in model.dimensions if d.is_reference}
    ref_ids |= set(model.relationships.reference_dimension_ids)
    for f in model.features:
        if f.depth_dimension_id in ref_ids:
            report.warn(
                f"Feature {f.id} is driven by REF dimension {f.depth_dimension_id} — "
                "reference dimensions are non-controlling; verify a driving dimension exists."
            )


def _check_instance_positions(model: DrawingData, report: ValidationReport) -> None:
    """Advisory: explicit per-instance hole positions should match qty and sit
    inside the part envelope (drawing frame: corner at origin)."""
    length = max((d.value for d in model.dimensions
                  if d.is_envelope and d.canonical_applies_to == "length"), default=0.0)
    width = max((d.value for d in model.dimensions
                 if d.is_envelope and d.canonical_applies_to == "width"), default=0.0)
    for h in model.hole_callouts:
        if not h.instance_positions:
            continue
        if len(h.instance_positions) != h.qty:
            report.warn(
                f"Hole callout {h.id} has {len(h.instance_positions)} explicit position(s) "
                f"but qty={h.qty}; using the listed positions."
            )
        if length > 0 and width > 0:
            for x, y in ((p[0], p[1]) for p in h.instance_positions if len(p) == 2):
                if not (0.0 <= x <= length and 0.0 <= y <= width):
                    report.warn(
                        f"Hole callout {h.id} instance ({x:g}, {y:g}) lies outside the "
                        f"{length:g} x {width:g} envelope — verify the coordinate frame."
                    )
                    break


def _check_unmodeled_fillets(model: DrawingData, report: ValidationReport) -> None:
    """Advisory: a fillet dimension was extracted but no fillet feature consumes
    it — a likely missed fillet. Warning only; never blocks the build."""
    if any(f.type == FeatureType.FILLET for f in model.features):
        return
    suspects = [
        d.id for d in model.dimensions
        if d.canonical_applies_to == "fillet_radius"
        or "fillet" in (d.applies_to or "").lower()
        or "fillet" in (d.notes or "").lower()
    ]
    if suspects:
        report.warn(
            f"Fillet dimension(s) {suspects} were extracted but no fillet feature uses "
            f"them — a fillet may have been missed; verify against the drawing."
        )


def _check_view_consistency(model: DrawingData, report: ValidationReport) -> None:
    """v2 (advisory): views' dimension lists should reference real dimensions."""
    dim_ids = {d.id for d in model.dimensions}
    view_names = {v.view_type.lower() for v in model.views}
    for v in model.views:
        unknown = [d for d in v.dimensions_shown if d not in dim_ids]
        if unknown:
            report.view_consistency_ok = False
            report.warn(f"View {v.view_type!r} lists unknown dimension ids {unknown}.")
    if view_names:
        for d in model.dimensions:
            if d.view and d.view.lower() not in view_names:
                report.view_consistency_ok = False
                report.warn(
                    f"Dimension {d.id} cites view {d.view!r}, which is not among the "
                    f"identified views."
                )
                break  # one example is enough; don't flood the report


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def compute_readiness(model: DrawingData, report: "ValidationReport") -> dict[str, float]:
    """Phase-4 drawing-completeness score (0..1 sub-scores + overall).

    Advisory and transparent — the authoritative gate is still ``report.ok``
    (READY/BLOCKED). These scores quantify *how close* a drawing is so batches
    can be triaged and an optional ``MACRO_READINESS_THRESHOLD`` can hard-gate.
    """
    dims = model.dimensions
    clear = sum(1 for d in dims if not d.value_unclear and not d.resolution_required)
    dimension_completeness = (clear / len(dims)) if dims else 0.0

    # Geometry completeness: penalize each structural-error category present.
    has_base = bool(model.build_order) and (
        (model.feature_by_id(model.build_order[0]) or _NO_FEATURE).type in BASE_FEATURE_TYPES
    )
    geometry_completeness = 1.0
    if not model.features:
        geometry_completeness -= 0.5
    if not model.build_order:
        geometry_completeness -= 0.3
    if not has_base:
        geometry_completeness -= 0.2
    if not report.unit_consistency_ok:
        geometry_completeness -= 0.2
    geometry_completeness = max(0.0, geometry_completeness)

    consistency = 1.0
    if report.failed_closure_chains:
        consistency -= 0.5
    if report.feasibility_issues:
        consistency -= 0.5
    consistency = max(0.0, consistency)

    feature_confidence = float(model.confidence)
    overall = (
        0.30 * dimension_completeness
        + 0.25 * geometry_completeness
        + 0.20 * consistency
        + 0.25 * feature_confidence
    )
    return {
        "geometry_completeness": round(geometry_completeness, 3),
        "dimension_completeness": round(dimension_completeness, 3),
        "consistency": round(consistency, 3),
        "feature_confidence": round(feature_confidence, 3),
        "macro_readiness": round(overall, 3),
    }


class _NoFeature:
    type = None


_NO_FEATURE = _NoFeature()


def run_verification(
    data: Union[DrawingData, dict[str, Any]],
) -> tuple[Optional[DrawingData], ValidationReport]:
    """Run the full Phase-1 verification pass.

    Returns ``(model, report)``; ``model`` is None only if shape validation
    failed. Never raises — inspect ``report.ok`` for the READY/BLOCKED status.
    """
    report = ValidationReport()

    try:
        model = _coerce(data)
    except ValidationError as e:
        report.error(f"Schema/shape validation failed:\n{e}")
        log.error("%s", report)
        return None, report

    # Carry the model's own extraction warnings into the report (visibility).
    for w in model.warnings:
        report.warn(f"extraction: {w}")

    _check_completeness(model, report)
    _check_geometry_sanity(model, report)
    _check_build_order(model, report)
    _check_dependencies(model, report)
    _check_unit_consistency(model, report)
    _check_sketch_definability(model, report)
    _check_ambiguity(model, report)
    _check_dimensional_closure(model, report)
    _check_pattern_envelopes(model, report)
    _check_reference_dimensions(model, report)
    _check_instance_positions(model, report)
    _check_unmodeled_fillets(model, report)
    _check_mirror_features(model, report)
    _check_view_consistency(model, report)

    # Phase-4 readiness scoring (advisory; optional hard-gate via env var).
    report.readiness = compute_readiness(model, report)
    threshold_raw = os.getenv("MACRO_READINESS_THRESHOLD")
    if threshold_raw:
        try:
            threshold = float(threshold_raw)
        except ValueError:
            report.warn(f"Ignoring non-numeric MACRO_READINESS_THRESHOLD={threshold_raw!r}.")
        else:
            score = report.readiness["macro_readiness"]
            if score < threshold:
                report.error(
                    f"Macro readiness {score:.0%} is below the configured threshold "
                    f"{threshold:.0%} (MACRO_READINESS_THRESHOLD) — build blocked."
                )

    log.info("%s", report)
    return model, report


def format_verification_report(model: Optional[DrawingData], report: ValidationReport) -> str:
    """Render the human-readable VERIFICATION REPORT (spec format)."""
    if model is None:
        body = [
            "VERIFICATION REPORT",
            "===================",
            "Schema validation: FAIL — extracted data did not match the schema.",
            *(f"  {e}" for e in report.errors),
            "",
            "OVERALL STATUS: BLOCKED — RESOLVE ISSUES FIRST",
        ]
        return "\n".join(body)

    derived = model.relationships.derived_dimension_ids
    ambiguous = sorted(set(report.ambiguous_dimension_ids))
    closure_status = "PASS" if not report.failed_closure_chains else "FAIL"
    unit_status = "PASS" if report.unit_consistency_ok else "FAIL"
    view_status = "PASS" if report.view_consistency_ok else "FAIL"
    feasibility_status = "PASS" if not report.feasibility_issues else "FAIL"
    overall = "READY TO BUILD" if report.ok else "BLOCKED — RESOLVE ISSUES FIRST"

    lines = [
        "VERIFICATION REPORT",
        "===================",
        f"Part: {model.display_name}    Units: {model.units.value}    "
        f"Standard: {model.drawing_standard or 'unknown'}    Confidence: {model.confidence:.2f}",
        f"Total dimensions extracted: {len(model.dimensions)}",
        f"Hole callouts extracted: {len(model.hole_callouts)}",
        f"Features identified: {len(model.features)}  (build order: {len(model.build_order)})",
        f"Dimensions flagged ambiguous: {len(ambiguous)}"
        + (f"  ->  {', '.join(ambiguous)}" if ambiguous else ""),
        f"Dimensional closure: {closure_status}"
        + (f"  ->  failed: {'; '.join(report.failed_closure_chains)}" if report.failed_closure_chains else
           f"  ({len(model.relationships.dimension_chains)} chain(s) checked)"),
        f"Unit consistency: {unit_status}",
        f"View consistency: {view_status}",
        f"Feature feasibility: {feasibility_status}"
        + (f"  ->  {'; '.join(report.feasibility_issues)}" if report.feasibility_issues else ""),
        f"Derived dimensions computed: {len(derived)}"
        + (f"  ->  {', '.join(derived)}" if derived else ""),
    ]
    r = report.readiness
    if r:
        lines += [
            "",
            "DRAWING COMPLETENESS SCORE (Phase 4):",
            f"  Geometry completeness:  {r['geometry_completeness']:.0%}",
            f"  Dimension completeness: {r['dimension_completeness']:.0%}",
            f"  Cross-view consistency: {r['consistency']:.0%}",
            f"  Feature confidence:     {r['feature_confidence']:.0%}",
            f"  Macro readiness:        {r['macro_readiness']:.0%}",
        ]
    if report.errors:
        lines += ["", "ERRORS:"]
        lines += [f"  [ERROR] {e}" for e in report.errors]
    if report.warnings:
        lines += ["", "WARNINGS:"]
        lines += [f"  [WARN]  {w}" for w in report.warnings]
    lines += ["", f"OVERALL STATUS: {overall}"]
    return "\n".join(lines)


def validate_drawing_data(
    data: Union[DrawingData, dict[str, Any]],
    raise_on_error: bool = True,
) -> DrawingData:
    """Validate extracted data for build readiness (raising wrapper).

    Kept for the COM build path and existing callers/tests. Runs the same
    checks as :func:`run_verification`.

    Raises:
        DrawingValidationError: if any check fails (and ``raise_on_error``).
    """
    model, report = run_verification(data)
    if model is None:
        raise DrawingValidationError(report)
    if not report.ok and raise_on_error:
        raise DrawingValidationError(report)

    # Stash the report's warnings onto the model so downstream code can see them.
    model.warnings = list(dict.fromkeys(model.warnings + report.warnings))
    return model
