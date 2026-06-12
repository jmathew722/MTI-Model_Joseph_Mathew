"""Post-build model validation.

After SolidWorks builds the part, confirm it actually represents the drawing:
a non-zero solid body exists, and the model's overall bounding box matches the
drawing's overall length/width/height dimensions within tolerance.

WINDOWS ONLY at call time (operates on a live SolidWorks document), but imports
cleanly anywhere. Public entry point: :func:`validate_model`.
"""
from __future__ import annotations

from typing import Any, Union

from pipeline.schema import DrawingData
from utils.logger import get_logger
from utils.unit_converter import to_meters

log = get_logger()

# Allowed mismatch between a model bounding-box edge and a drawing dimension.
# A drawing's overall envelope may not exactly equal any single dimension, so we
# use a generous relative tolerance plus an absolute floor.
REL_TOLERANCE = 0.05      # 5%
ABS_TOLERANCE_M = 0.0005  # 0.5 mm floor


def _coerce(data: Union[DrawingData, dict[str, Any]]) -> DrawingData:
    return data if isinstance(data, DrawingData) else DrawingData.model_validate(data)


def _dims_in_meters(model: DrawingData, applies_to: str) -> list[float]:
    """All dimension values (in meters) whose applies_to matches."""
    out = []
    for d in model.dimensions:
        if (d.applies_to or "").lower().strip() == applies_to:
            out.append(to_meters(d.value, d.unit.value))
    return out


def _matches_any(actual_m: float, expected_values_m: list[float]) -> bool:
    for exp in expected_values_m:
        tol = max(ABS_TOLERANCE_M, REL_TOLERANCE * exp)
        if abs(actual_m - exp) <= tol:
            return True
    return False


def validate_model(sw_doc, drawing_data: Union[DrawingData, dict[str, Any]]) -> dict[str, Any]:
    """Verify the built model matches the drawing's dimensions.

    Args:
        sw_doc: The live SolidWorks document returned by ``build_model``.
        drawing_data: The validated drawing data the model was built from.

    Returns:
        A report dict with ``passed`` / ``failed`` / ``warnings`` lists, plus
        ``volume_mm3``, ``surface_area_mm2``, and the measured bounding box.
    """
    model = _coerce(drawing_data)
    report: dict[str, Any] = {"passed": [], "failed": [], "warnings": []}

    # --- Mass properties: confirms a solid body exists ---
    try:
        mass = sw_doc.Extension.CreateMassProperty()
    except Exception as e:
        report["failed"].append(f"Could not create mass property object: {e}")
        return report
    if mass is None:
        report["failed"].append("CreateMassProperty returned None — no body to measure.")
        return report
    mass.UseSystemUnits = False  # report in the document's units; Volume still in SI here

    try:
        volume_m3 = float(mass.Volume)
    except Exception as e:
        report["failed"].append(f"Could not read volume: {e}")
        return report

    if volume_m3 <= 0:
        report["failed"].append("CRITICAL: Part has zero volume — no solid body was created.")
        return report
    report["passed"].append("Solid body exists (volume > 0).")
    report["volume_mm3"] = volume_m3 * 1e9
    try:
        report["surface_area_mm2"] = float(mass.SurfaceArea) * 1e6
    except Exception:
        report["warnings"].append("Could not read surface area.")

    # --- Bounding box vs overall drawing dimensions ---
    # IModelDoc2 has no GetModelBoundingBox; read the box from the solid body
    # itself (IBody2::GetBodyBox). [xmin,ymin,zmin,xmax,ymax,zmax] in meters.
    try:
        bodies = sw_doc.GetBodies2(0, True)  # swBodyType_e.swSolidBody = 0
        bbox = bodies[0].GetBodyBox() if bodies else None
        if bbox is None:
            report["warnings"].append("No solid body found to read a bounding box from.")
    except Exception as e:
        report["warnings"].append(f"Could not read bounding box: {e}")
        bbox = None

    if bbox and len(bbox) >= 6:
        extents_m = sorted(
            [bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]], reverse=True
        )
        report["bounding_box_mm"] = [round(e * 1000, 4) for e in extents_m]

        # Compare each declared overall dimension to the closest bbox extent.
        for label in ("length", "width", "height"):
            expected = _dims_in_meters(model, label)
            if not expected:
                continue
            matched = any(_matches_any(e, expected) for e in extents_m)
            exp_mm = [round(v * 1000, 3) for v in expected]
            if matched:
                report["passed"].append(
                    f"{label}: a model extent matches drawing value(s) {exp_mm} mm."
                )
            else:
                report["failed"].append(
                    f"{label}: drawing value(s) {exp_mm} mm not found among model extents "
                    f"{report['bounding_box_mm']} mm (tolerance {REL_TOLERANCE:.0%})."
                )

    log.info(
        "Model validation: %d passed, %d failed, %d warnings.",
        len(report["passed"]),
        len(report["failed"]),
        len(report["warnings"]),
    )
    for f in report["failed"]:
        log.error("  [FAIL] %s", f)
    for w in report["warnings"]:
        log.warning("  [WARN] %s", w)

    report["ok"] = not report["failed"]
    return report
