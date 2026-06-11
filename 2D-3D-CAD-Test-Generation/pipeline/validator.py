"""Build-readiness validation.

:mod:`pipeline.schema` guarantees the *shape* of the data (types, enums, positive
dimensions, confidence range). This module enforces the cross-field *business
rules* that determine whether the data can actually drive a SolidWorks build, and
produces a clear, human-readable report of exactly what failed and why.

If validation fails, the pipeline must NOT proceed to SolidWorks.

Public entry point: :func:`validate_drawing_data`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from pydantic import ValidationError

from pipeline.schema import DrawingData, FeatureType
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


def validate_drawing_data(
    data: Union[DrawingData, dict[str, Any]],
    raise_on_error: bool = True,
) -> DrawingData:
    """Validate extracted data for build readiness.

    Runs, in order: shape validation (via Pydantic), then build-graph checks
    (base feature, dependencies, unit consistency, sketch definability).

    Args:
        data: A :class:`DrawingData` or a raw dict.
        raise_on_error: if True (default), raise :class:`DrawingValidationError`
            when any check fails. If False, return the model and let the caller
            inspect ``model.warnings`` / re-run with a report.

    Returns:
        The validated :class:`DrawingData`.

    Raises:
        DrawingValidationError: if shape or build-readiness checks fail (and
            ``raise_on_error`` is True).
    """
    report = ValidationReport()

    # --- Shape validation (types/enums/positivity) ---
    try:
        model = _coerce(data)
    except ValidationError as e:
        report.error(f"Schema/shape validation failed:\n{e}")
        log.error("%s", report)
        raise DrawingValidationError(report) from e

    # Carry the model's own extraction warnings into the report (visibility).
    for w in model.warnings:
        report.warn(f"extraction: {w}")

    feature_ids = {f.id for f in model.features}
    dim_ids = {d.id for d in model.dimensions}

    # --- 1. Completeness: at least one feature and a non-empty build order ---
    if not model.features:
        report.error("No features found — nothing to build.")
    if not model.build_order:
        report.error("build_order is empty — no build sequence defined.")

    # --- 2. Geometry sanity: no zero/negative dimension values ---
    #     (schema already enforces > 0, but re-check defensively for raw dicts.)
    for d in model.dimensions:
        if d.value <= 0:
            report.error(f"Dimension {d.id} has non-positive value {d.value}.")

    # --- 3. build_order references only real features ---
    for fid in model.build_order:
        if fid not in feature_ids:
            report.error(f"build_order references unknown feature id {fid!r}.")
    # Features omitted from the build order are a warning, not a hard error.
    for fid in feature_ids:
        if fid not in model.build_order:
            report.warn(f"Feature {fid} is not in build_order and will not be built.")

    # --- 4. Base feature check: first built feature must be a solid base ---
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

    # --- 5. Feature dependency: referenced dimensions exist ---
    for f in model.features:
        for ref in f.related_dimensions:
            if ref not in dim_ids:
                report.error(f"Feature {f.id} references unknown dimension {ref!r}.")
        if f.depth_dimension_id is not None and f.depth_dimension_id not in dim_ids:
            report.error(
                f"Feature {f.id} depth_dimension_id {f.depth_dimension_id!r} does not exist."
            )

    # --- 6. Unit consistency: all dimensions share the drawing's unit system ---
    mismatched = sorted({d.unit.value for d in model.dimensions if d.unit != model.units})
    if mismatched:
        report.error(
            f"Mixed units: drawing declares {model.units.value!r} but dimensions use "
            f"{mismatched}. Normalize units before building."
        )

    # --- 7. Sketch closure heuristic: sketch-based features need enough dimensions ---
    for f in model.features:
        if f.type in SKETCH_FEATURE_TYPES:
            n = len(f.related_dimensions)
            if n < MIN_PROFILE_DIMENSIONS:
                report.error(
                    f"Sketch feature {f.id} ({f.type.value}) has {n} related dimension(s); "
                    f"need at least {MIN_PROFILE_DIMENSIONS} to define a profile."
                )

    log.info("%s", report)

    if not report.ok and raise_on_error:
        raise DrawingValidationError(report)

    # Stash the report's warnings onto the model so downstream code can see them.
    model.warnings = list(dict.fromkeys(model.warnings + report.warnings))
    return model
