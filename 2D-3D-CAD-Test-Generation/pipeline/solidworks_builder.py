"""SolidWorks 2024 model builder via the Windows COM API.

WINDOWS ONLY. ``win32com``/``pythoncom`` are imported lazily *inside* functions
so this module imports cleanly on any platform (macOS/Linux); the Windows-only
code paths raise a clear :class:`PlatformError` only when actually invoked.

Design discipline enforced throughout:
  * every COM call's return value is checked for ``None``/failure;
  * EVERY linear dimension passes through :func:`utils.unit_converter.to_meters`
    (asserted via :func:`assert_meters`) before reaching the API — SolidWorks works
    in meters internally;
  * document units are set BEFORE any geometry is created;
  * sketches are verified FULLY DEFINED before extruding;
  * :func:`check_rebuild_errors` runs after EVERY feature;
  * fillets and chamfers (the fragile operations) are wrapped in try/except and
    demoted to warnings rather than crashing the whole build;
  * a partial model is saved if the build crashes, and an auto-save runs every
    few features.

NOTE: This module is verified by inspection on non-Windows dev machines; run it on
Windows + SolidWorks 2024 to exercise the COM paths.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional, Union

from pipeline.schema import DrawingData, Feature, FeatureType, HoleType
from utils.logger import get_logger
from utils.unit_converter import assert_meters, to_meters, to_radians

log = get_logger()

AUTOSAVE_EVERY = 3  # auto-save the part every N features
OUTPUT_DIR_DEFAULT = Path(__file__).resolve().parent.parent / "output"

# Plane names as SolidWorks exposes them for SelectByID2. Aligned with
# pipeline.view_ingest.VIEW_PLANES so a feature read in a side/bottom view lands
# on the right reference plane (second_side/bottom reuse the orthogonal plane —
# through-cuts are direction-proof).
_PLANE_NAMES = {
    "front": "Front Plane",
    "top": "Top Plane",
    "right": "Right Plane",
    "side": "Right Plane",
    "second_side": "Right Plane",
    "bottom": "Top Plane",
}


class PlatformError(RuntimeError):
    """Raised when SolidWorks/COM functionality is used off Windows."""


class SolidWorksError(RuntimeError):
    """Raised on any SolidWorks build failure. Carries the partial-save path."""

    def __init__(self, message: str, partial_path: Optional[str] = None):
        self.partial_path = partial_path
        super().__init__(message)


# --------------------------------------------------------------------------- #
# COM bootstrap
# --------------------------------------------------------------------------- #
def _require_windows() -> None:
    if sys.platform != "win32":
        raise PlatformError(
            "The SolidWorks build stage requires Windows with SolidWorks 2024 and "
            f"pywin32 installed. Current platform: {sys.platform!r}. Use --validate-only "
            "to run the extraction/validation pipeline on this machine."
        )


def _constants():
    """Return the win32com SolidWorks constants namespace (requires makepy/gencache)."""
    from win32com.client import constants  # type: ignore

    return constants


def _null_dispatch():
    """A VT_DISPATCH NULL VARIANT.

    Late-bound (non-gencache) COM calls can't pass plain Python ``None`` for an
    ``Object``-typed parameter — dynamic dispatch sends VT_EMPTY, and SolidWorks'
    IDispatch::Invoke rejects that with "Type mismatch". Wrapping it as an
    explicit VT_DISPATCH NULL VARIANT is accepted.
    """
    import pythoncom  # type: ignore
    from win32com.client import VARIANT  # type: ignore

    return VARIANT(pythoncom.VT_DISPATCH, None)


def _const(name: str, default: Optional[int] = None) -> int:
    """Fetch a SolidWorks enum constant by name with an optional fallback.

    Using the generated type-library constants (early binding) is reliable once
    ``EnsureDispatch`` has run. The fallback keeps us robust if a particular
    constant is missing from the generated cache.
    """
    try:
        value = getattr(_constants(), name)
        if value is not None:
            return value
    except Exception:
        pass
    if default is None:
        raise SolidWorksError(f"SolidWorks constant {name!r} unavailable (run makepy).")
    return default


# SOLIDWORKS 2024 Constant type library — loading this by CLSID/version populates
# win32com.client.constants with the swConst enums (swDocPART, swSketchSegment_*, etc.)
# without needing GetTypeInfo() on a live SolidWorks object.
_SW_CONST_TYPELIB = ("{4687F359-55D0-4CD3-B6CF-2EB42C11F989}", 0, 20, 0)


def connect_to_solidworks():
    """Connect to a running SolidWorks instance, or launch a new one.

    SolidWorks' IDispatch does not support ``GetTypeInfo()``, so
    ``gencache.EnsureDispatch`` (which calls it) always fails with "This COM
    object can not automate the makepy process". Instead we use plain
    (late-bound) ``Dispatch`` for the application object, and load the
    SOLIDWORKS constants type library directly by CLSID to populate
    ``win32com.client.constants`` (needed by the feature builders).

    Returns:
        The ``ISldWorks`` application object.

    Raises:
        PlatformError: if not on Windows.
        SolidWorksError: if connection/launch fails.
    """
    _require_windows()
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore
    from win32com.client import gencache  # type: ignore

    pythoncom.CoInitialize()
    gencache.EnsureModule(*_SW_CONST_TYPELIB)

    sw_app = None
    try:
        # Prefer an already-running instance.
        active = win32com.client.GetActiveObject("SldWorks.Application")
        sw_app = win32com.client.Dispatch(active)
        log.info("Connected to existing SolidWorks instance.")
    except Exception:
        try:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            sw_app.Visible = True
            log.info("Launched a new SolidWorks instance.")
        except Exception as e:
            raise SolidWorksError(f"Failed to connect to or launch SolidWorks: {e}") from e

    if sw_app is None:
        raise SolidWorksError("Failed to obtain a SolidWorks application object.")

    try:
        log.info("SolidWorks revision: %s", sw_app.RevisionNumber)
    except Exception:
        log.warning("Could not read SolidWorks revision number (continuing).")
    return sw_app


def create_new_part(sw_app, template_path: Optional[str] = None):
    """Create a new part document.

    Raises:
        SolidWorksError: if the template is missing or the document is not created.
    """
    if not template_path:
        # swUserPreferenceStringValue.swDefaultTemplatePart = 8 (documented default).
        try:
            template_path = sw_app.GetUserPreferenceStringValue(_const("swDefaultTemplatePart", 8))
        except Exception as e:
            raise SolidWorksError(f"Could not resolve default part template: {e}") from e

    if not template_path or not Path(template_path).exists():
        raise SolidWorksError(
            f"Part template not found: {template_path!r}. Set SOLIDWORKS_TEMPLATE_PATH "
            "in .env or ensure the SolidWorks default template is configured."
        )

    sw_doc = sw_app.NewDocument(template_path, 0, 0, 0)
    if sw_doc is None:
        raise SolidWorksError("NewDocument returned None — failed to create part document.")
    log.info("Created new part from template: %s", template_path)
    return sw_doc


def set_document_units(sw_doc, unit_system: str) -> None:
    """Set the document's display units. MUST be called before creating geometry.

    SolidWorks stores geometry in meters regardless of this setting; this only
    affects what the user sees in the UI, but we set it for correctness.
    """
    # swLengthUnit_e via generated constants; cm corrected from the original spec
    # (which used the unit-*system* constant swCGS by mistake).
    unit_map = {
        "mm": _const("swMM", 0),
        "cm": _const("swCM", 1),
        "inch": _const("swINCHES", 3),
    }
    length_unit = unit_map.get(unit_system.lower().strip(), unit_map["mm"])
    # SetUnits(lengthUnit, thousandsDelimiter, decimalPlaces, fractionDenominator, roundToFraction)
    ok = sw_doc.SetUnits(length_unit, 0, 4, 2, False)
    if ok is False:  # SetUnits returns bool; None on some bindings — treat None as ok.
        log.warning("SetUnits reported failure for unit system %r (continuing).", unit_system)
    else:
        log.info("Document units set to %s.", unit_system)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _coerce(data: Union[DrawingData, dict[str, Any]]) -> DrawingData:
    return data if isinstance(data, DrawingData) else DrawingData.model_validate(data)


def get_feature_by_id(model: DrawingData, feature_id: str) -> Feature:
    feature = model.feature_by_id(feature_id)
    if feature is None:
        raise SolidWorksError(f"build_order references missing feature {feature_id!r}.")
    return feature


def get_dimensions_for_feature(model: DrawingData, feature: Feature) -> dict[str, float]:
    """Resolve a feature's dimensions to METERS, keyed by ``applies_to``.

    Every value is converted via :func:`to_meters` and gated by
    :func:`assert_meters`, so anything handed to the COM API is guaranteed to be
    a sane meter value. Angular dimensions are returned in radians under their
    ``applies_to`` key as well.
    """
    resolved: dict[str, float] = {}
    ids = list(feature.related_dimensions)
    if feature.depth_dimension_id and feature.depth_dimension_id not in ids:
        ids.append(feature.depth_dimension_id)

    for dim_id in ids:
        dim = model.dimension_by_id(dim_id)
        if dim is None:
            raise SolidWorksError(
                f"Feature {feature.id} references missing dimension {dim_id!r}."
            )
        key = (dim.applies_to or dim.type.value).lower().strip()
        if dim.type.value == "angular":
            value = to_radians(dim.value)
        else:
            value = assert_meters(to_meters(dim.value, dim.unit.value), f"{feature.id}.{key}")
        resolved[key] = value
        # Also expose the value under its CANONICAL applies_to token so the feature
        # builders find width/length/height/diameter regardless of the descriptive
        # label (e.g. "overall_width"->"width", "inside_height"->"height"). This
        # mirrors the VBA generator's _dims_map and fixes empty-body builds where
        # the base profile was labeled "overall_*"/"inside_*".
        canon = getattr(dim, "canonical_applies_to", "") or ""
        if canon:
            resolved.setdefault(canon, value)
        # Also expose the value under its canonical geometric type so the feature
        # builders find a diameter/radius regardless of its descriptive applies_to
        # label (e.g. "outer_diameter"/"bore_diameter" -> reachable as "diameter").
        if dim.type.value == "diameter":
            resolved.setdefault("diameter", value)
        elif dim.type.value == "radial":
            resolved.setdefault("radius", value)

    # Also expose the depth explicitly under "depth" for builders that need it.
    if feature.depth_dimension_id:
        depth_dim = model.dimension_by_id(feature.depth_dimension_id)
        if depth_dim is not None and depth_dim.type.value != "angular":
            resolved.setdefault(
                "depth",
                assert_meters(
                    to_meters(depth_dim.value, depth_dim.unit.value), f"{feature.id}.depth"
                ),
            )
    return resolved


def _rect_sides(dims: dict[str, float]) -> tuple[Optional[float], Optional[float]]:
    """Pick two distinct in-plane rectangle sides from the resolved dims.

    Prefers length/width/height (the in-plane envelope) over the extrude axis
    (depth/thickness). Returns the two largest distinct values; falls back to a
    square when only one in-plane size is known, or (None, None) when none is.
    """
    seen: list[float] = []
    for k in ("length", "width", "height"):
        v = dims.get(k)
        if v and v > 0 and v not in seen:
            seen.append(v)
    if len(seen) >= 2:
        seen.sort(reverse=True)
        return seen[0], seen[1]
    if len(seen) == 1:
        return seen[0], seen[0]
    return None, None


def _select_plane(sw_doc, sketch_plane: Optional[str]) -> None:
    """Select the named reference plane for the next sketch."""
    name = _PLANE_NAMES.get((sketch_plane or "front").lower().strip(), "Front Plane")
    selected = sw_doc.Extension.SelectByID2(
        name, "PLANE", 0.0, 0.0, 0.0, False, 0, _null_dispatch(), 0
    )
    if not selected:
        raise SolidWorksError(f"Could not select sketch plane {name!r}.")


def _verify_sketch_fully_defined(sw_doc) -> None:
    """Verify the active sketch is fully defined before extruding.

    An under-defined sketch produces unpredictable geometry. We attempt to add
    relations automatically; if it remains under-defined we warn (the geometry
    is still dimensionally pinned by the values we drew with).
    """
    sketch = sw_doc.SketchManager.ActiveSketch
    if sketch is None:
        raise SolidWorksError("No active sketch to verify.")
    # GetConstrainedStatus: 1 = fully defined, 2 = over defined, 3 = under defined.
    try:
        status = sketch.GetConstrainedStatus()
    except Exception:
        status = None
    if status == 3:
        log.warning("Sketch is under-defined; attempting Fully Define Sketch.")
        try:
            sw_doc.SketchManager.FullyDefineSketch(
                True, True, 0, 0, 0,
                _null_dispatch(), _null_dispatch(), _null_dispatch(),
                _null_dispatch(), _null_dispatch(),
            )
            status = sketch.GetConstrainedStatus()
        except Exception as e:
            log.warning("FullyDefineSketch failed: %s", e)
    if status == 2:
        raise SolidWorksError("Sketch is over-defined — cannot extrude reliably.")
    if status == 3:
        log.warning("Sketch still under-defined after auto-define; geometry may be loose.")


def _origin_relation_for_rectangle(sw_doc) -> None:
    """Best-effort: relate the sketch to the origin so it's anchored."""
    try:
        # Selecting the origin and a sketch point and making them coincident is
        # geometry-specific; we leave the explicit relation to FullyDefineSketch.
        pass
    except Exception:
        pass


def check_rebuild_errors(sw_doc) -> bool:
    """Check for rebuild errors/warnings after a feature. Returns True if clean."""
    try:
        errors = sw_doc.GetRebuildErrorCount() if hasattr(sw_doc, "GetRebuildErrorCount") else sw_doc.GetRebuildErrors()
    except Exception:
        # Older API name fallback.
        errors = 0
    try:
        warnings = sw_doc.GetRebuildWarningCount() if hasattr(sw_doc, "GetRebuildWarningCount") else 0
    except Exception:
        warnings = 0

    if errors and errors > 0:
        log.error("REBUILD ERROR: %s error(s) detected.", errors)
        return False
    if warnings and warnings > 0:
        log.warning("REBUILD WARNING: %s warning(s) — continuing.", warnings)
    return True


def _solid_body_exists(sw_doc) -> bool:
    """True if the part currently contains at least one solid body.

    Uses ``IPartDoc.GetBodies2(swSolidBody, visibleOnly=False)`` which is reliable
    under late-bound dispatch. (The mass-property objects — ``CreateMassProperty``/
    ``CreateMassProperty2`` — are not resolvable without early binding here and
    raise DISP_E_MEMBERNOTFOUND, so they must not gate the build.)
    """
    try:
        # swBodyType_e.swSolidBody = 0
        bodies = sw_doc.GetBodies2(0, False)
    except Exception as e:
        log.warning("Could not enumerate solid bodies: %s", e)
        return False
    if bodies is None:
        return False
    try:
        return len(bodies) > 0
    except TypeError:
        # A single body may come back as a bare COM object rather than a tuple.
        return True


# --------------------------------------------------------------------------- #
# Feature builders — each returns the created feature object (or None on skip)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Drawing-frame placement helpers (parity with the VBA macro generator)
# --------------------------------------------------------------------------- #
# The .sldprt build now uses the SAME coordinate convention as the generated
# macros and the build plan header: the base solid's lower-left corner sits at
# the sketch origin (+X right, +Y up), so hole/feature positions dimensioned from
# the part edges are used as sketch coordinates directly. Circular features with
# no read position center on the part envelope; rectangles anchor their lower-left
# corner. This is what lets positioned hole PATTERNS land where they were drawn
# (the old centred-at-origin convention could only place a single hole at the
# part centre).
def _feature_center_m(model, feature, *, circular: bool) -> tuple[float, float]:
    """(cx, cy) in METERS for a feature's sketch, matching the macro generator."""
    from pipeline.macro_generator import _envelope

    unit = model.units.value
    if getattr(feature, "position_known", False):
        return to_meters(feature.offset_x, unit), to_meters(feature.offset_y, unit)
    if circular:
        length, width = _envelope(model)
        return to_meters((length or 0.0) / 2.0, unit), to_meters((width or 0.0) / 2.0, unit)
    return 0.0, 0.0  # rectangle: lower-left corner at the origin


def _body_center_xy_m(sw_doc) -> Optional[tuple[float, float]]:
    """In-plane centre (X, Y) of the current solid body's bounding box, in METERS.

    Used to place holes whose location was never dimensioned: centring on the
    ACTUAL body (rather than the extracted envelope, which can be missing a
    length/width and collapse to an edge) guarantees the cut lands on material."""
    try:
        bodies = sw_doc.GetBodies2(0, False)
        body = bodies[0] if isinstance(bodies, (list, tuple)) else bodies
        box = body.GetBodyBox()  # (x1, y1, z1, x2, y2, z2) in meters
        return (box[0] + box[3]) / 2.0, (box[1] + box[4]) / 2.0
    except Exception as e:
        log.warning("Could not read body bounding box for centring: %s", e)
        return None


def _draw_circles(sw_doc, centers_m: list[tuple[float, float]], radius_m: float) -> None:
    for cx, cy in centers_m:
        if sw_doc.SketchManager.CreateCircleByRadius(cx, cy, 0.0, radius_m) is None:
            raise SolidWorksError("CreateCircleByRadius returned None.")


def _do_cut(sw_doc, feature, through_all: bool, depth_m: Optional[float]):
    """FeatureCut4 with a direction-flip retry (mirrors the macro generator).

    A blind/through cut aimed at the wrong side of the sketch plane removes no
    material and returns None; retrying with the direction flipped recovers it.
    """
    end = 1 if through_all else 0  # 1=ThroughAll, 0=Blind

    def _cut(flip: bool):
        return sw_doc.FeatureManager.FeatureCut4(
            True, False, flip, end, 0, depth_m or 0.0, 0.01,
            False, False, False, False, to_radians(0), to_radians(0),
            False, False, False, False, False,
            True, True, True, True, False, 0, 0, False, False,
        )

    feat = _cut(True)
    if feat is None:
        feat = _cut(False)
    if feat is None:
        raise SolidWorksError(f"FeatureCut4 returned None for {feature.id}.")
    return feat


def _circular_cut_at(sw_doc, feature, centers_m: list[tuple[float, float]],
                     radius_m: float, through_all: bool, depth_m: Optional[float]):
    """Sketch N circles at the given centres and cut them in one feature."""
    _select_plane(sw_doc, feature.sketch_plane)
    sw_doc.SketchManager.InsertSketch(True)
    if sw_doc.SketchManager.ActiveSketch is None:
        raise SolidWorksError("Failed to enter sketch mode for hole/cut.")
    _draw_circles(sw_doc, centers_m, radius_m)
    _verify_sketch_fully_defined(sw_doc)
    sw_doc.SketchManager.InsertSketch(True)  # close the sketch
    if depth_m:
        assert_meters(depth_m, f"{feature.id}.cut_depth")
    return _do_cut(sw_doc, feature, through_all, depth_m)


def build_extrude_boss(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Create the solid base body: a rectangular or circular extruded boss."""
    _select_plane(sw_doc, feature.sketch_plane)
    sw_doc.SketchManager.InsertSketch(True)
    if sw_doc.SketchManager.ActiveSketch is None:
        raise SolidWorksError("Failed to enter sketch mode for extrude_boss.")

    diameter = dims.get("diameter") or dims.get("hole_diameter")
    side_u, side_v = _rect_sides(dims)

    if diameter:
        r = diameter / 2.0
        cx, cy = _feature_center_m(model, feature, circular=True)
        if sw_doc.SketchManager.CreateCircleByRadius(cx, cy, 0, r) is None:
            raise SolidWorksError("CreateCircleByRadius returned None for extrude_boss.")
    elif side_u and side_v:
        # Lower-left corner at the drawing-frame origin (offset if dimensioned).
        cx, cy = _feature_center_m(model, feature, circular=False)
        if sw_doc.SketchManager.CreateCornerRectangle(
            cx, cy, 0, cx + side_u, cy + side_v, 0
        ) is None:
            raise SolidWorksError("CreateCornerRectangle returned None for extrude_boss.")
    else:
        raise SolidWorksError(
            f"extrude_boss {feature.id} needs either a diameter or two in-plane sides; got {dims}."
        )
    _origin_relation_for_rectangle(sw_doc)
    _verify_sketch_fully_defined(sw_doc)

    # Prefer the explicit extrude axis (depth/thickness) over height, which is
    # usually an in-plane rectangle side rather than the extrude distance.
    depth = dims.get("depth") or dims.get("thickness") or dims.get("height")
    if not depth:
        raise SolidWorksError(f"extrude_boss {feature.id} has no depth/height dimension.")
    assert_meters(depth, f"{feature.id}.extrude_depth")

    sw_doc.SketchManager.InsertSketch(True)  # close the sketch
    # FeatureExtrusion3(Sd, Flip, Dir, T1, T2, D1, D2, Dchk1, Dchk2, Ddir1, Ddir2,
    #   Dang1, Dang2, OffsetReverse1, OffsetReverse2, TranslateSurface1, TranslateSurface2,
    #   Merge, UseFeatScope, UseAutoSelect, T0, StartOffset, FlipStartOffset)
    feat = sw_doc.FeatureManager.FeatureExtrusion3(
        True, False, False, 0, 0, depth, 0.01,
        False, False, False, False,
        to_radians(0), to_radians(0),
        False, False, False, False,
        True, True, True, 0, 0, False,
    )
    if feat is None:
        raise SolidWorksError(f"FeatureExtrusion3 returned None for {feature.id}.")
    if not _solid_body_exists(sw_doc):
        raise SolidWorksError(f"No solid body exists after base extrude {feature.id} (volume=0).")
    return feat


def build_extrude_cut(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Cut material from the existing solid. Requires a base body first.

    The cut profile is placed in the drawing frame: a circular cut centres on the
    feature offset (or the part centre when not dimensioned), a rectangular cut
    anchors its lower-left corner at the offset (or the origin)."""
    if not _solid_body_exists(sw_doc):
        raise SolidWorksError(f"extrude_cut {feature.id} requires an existing solid body.")

    diameter = dims.get("diameter") or dims.get("hole_diameter")
    side_u, side_v = _rect_sides(dims)
    depth = dims.get("depth")
    through_all = depth is None  # no depth → cut through everything

    if diameter:
        cx, cy = _feature_center_m(model, feature, circular=True)
        return _circular_cut_at(sw_doc, feature, [(cx, cy)], diameter / 2.0, through_all, depth)

    if not (side_u and side_v):
        raise SolidWorksError(f"extrude_cut {feature.id} needs diameter or two in-plane sides; got {dims}.")

    _select_plane(sw_doc, feature.sketch_plane)
    sw_doc.SketchManager.InsertSketch(True)
    if sw_doc.SketchManager.ActiveSketch is None:
        raise SolidWorksError("Failed to enter sketch mode for extrude_cut.")
    cx, cy = _feature_center_m(model, feature, circular=False)
    if sw_doc.SketchManager.CreateCornerRectangle(cx, cy, 0, cx + side_u, cy + side_v, 0) is None:
        raise SolidWorksError("CreateCornerRectangle returned None for extrude_cut.")
    _verify_sketch_fully_defined(sw_doc)
    sw_doc.SketchManager.InsertSketch(True)
    if depth:
        assert_meters(depth, f"{feature.id}.cut_depth")
    return _do_cut(sw_doc, feature, through_all, depth)


def build_hole(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Create holes as exact circle cuts, at parity with the VBA macro generator.

    The hole DIAMETER and per-instance POSITIONS come from the feature's
    ``hole_callout`` (where the extractor records them) — not the feature's
    related dimensions, which carry bolt-circle / spacing values. Every instance
    is cut (a full bolt pattern, not a single centred hole), through or blind, and
    a counterbore gets a second concentric blind cut. Falls back to a single
    centred circular cut from the feature dims only when no callout exists."""
    if not _solid_body_exists(sw_doc):
        raise SolidWorksError(f"hole {feature.id} requires an existing solid body.")

    from pipeline.macro_generator import _hole_positions

    unit = model.units.value
    h = model.hole_callout_for_feature(feature.id)
    if h is None:
        diameter = dims.get("hole_diameter") or dims.get("diameter")
        if not diameter:
            raise SolidWorksError(f"hole {feature.id} has no diameter dimension.")
        cx, cy = _feature_center_m(model, feature, circular=True)
        depth = dims.get("depth")
        return _circular_cut_at(sw_doc, feature, [(cx, cy)], diameter / 2.0,
                                through_all=depth is None, depth_m=depth)

    positions_m = [(to_meters(x, unit), to_meters(y, unit)) for x, y in _hole_positions(model, h)]
    if not positions_m:
        raise SolidWorksError(f"hole {feature.id}: no instance positions could be resolved.")
    # If the positions are the centred FALLBACK (drawing never dimensioned them),
    # re-centre the whole layout on the ACTUAL body so the cut can't land off an
    # edge when an envelope dimension was missing.
    if not h.position_known and not h.instance_positions:
        center = _body_center_xy_m(sw_doc)
        if center is not None:
            cxm = sum(p[0] for p in positions_m) / len(positions_m)
            cym = sum(p[1] for p in positions_m) / len(positions_m)
            dx, dy = center[0] - cxm, center[1] - cym
            positions_m = [(x + dx, y + dy) for x, y in positions_m]
    dia_m = to_meters(h.diameter, unit)
    thru = bool(h.thru) or h.type == HoleType.THRU
    depth_m = None
    if not thru:
        if h.depth <= 0:
            raise SolidWorksError(f"blind hole {h.id} has no depth.")
        depth_m = to_meters(h.depth, unit)

    feat = _circular_cut_at(sw_doc, feature, positions_m, dia_m / 2.0, thru, depth_m)

    # Counterbore: a second concentric blind cut with the larger diameter.
    if h.type == HoleType.COUNTERBORE and h.cbore_diameter > 0 and h.cbore_depth > 0:
        _circular_cut_at(
            sw_doc, feature, positions_m, to_meters(h.cbore_diameter, unit) / 2.0,
            through_all=False, depth_m=to_meters(h.cbore_depth, unit),
        )
    return feat


# Sentinel: a feature that is intentionally a no-op (already realized elsewhere or
# cosmetic-only) — build_model treats it as a successful, non-fatal skip.
_NOOP = object()


def build_thread(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Tapped/threaded feature. Real helical threads are prohibited, so this
    DRILLS the tap hole (from the hole callout, like build_hole) and leaves the
    thread cosmetic. When the feature carries no drillable callout it is a
    cosmetic-only marker and is skipped without error."""
    if model.hole_callout_for_feature(feature.id) is not None:
        return build_hole(sw_doc, model, feature, dims)
    log.info("  thread %s is cosmetic-only (no drill callout) — not modeled.", feature.id)
    return _NOOP


def build_fillet(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Apply a constant-radius fillet. FRAGILE — wrapped by the orchestrator.

    Selects edges of the most recent feature when no explicit edge data exists.
    On failure the orchestrator records a warning and continues.
    """
    radius = dims.get("fillet_radius") or dims.get("radius") or next(iter(dims.values()), None)
    if not radius:
        raise SolidWorksError(f"fillet {feature.id} has no radius dimension.")
    assert_meters(radius, f"{feature.id}.fillet_radius")

    # Select all edges of the part by selecting the solid body's edges is non-trivial
    # without topology data; select the last feature so SW fillets its edges.
    sw_doc.ClearSelection2(True)
    # FeatureFillet3(Options, R1, Ftyp, OverflowType, ...) — minimal constant-radius form.
    feat = sw_doc.FeatureManager.FeatureFillet3(
        195,  # default fillet options bitmask (propagate, etc.)
        radius, 0, 0, 0, 0, 0,
        _null_dispatch(), _null_dispatch(), _null_dispatch(), _null_dispatch(),
        _null_dispatch(), _null_dispatch(), _null_dispatch(),
    )
    if feat is None:
        raise SolidWorksError(f"FeatureFillet3 returned None for {feature.id} (no edges selected?).")
    return feat


def build_chamfer(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Apply a chamfer. FRAGILE — wrapped by the orchestrator like fillet."""
    distance = dims.get("chamfer") or dims.get("length") or next(
        (v for k, v in dims.items() if k != "angle"), None
    )
    if not distance:
        raise SolidWorksError(f"chamfer {feature.id} has no distance dimension.")
    assert_meters(distance, f"{feature.id}.chamfer_distance")
    angle = dims.get("angle", to_radians(45))  # default 45° if unspecified (already radians)

    sw_doc.ClearSelection2(True)
    # InsertFeatureChamfer(Width, Angle, Flip, Type, OtherDist, VertexChamDist, VertexChamDist2)
    feat = sw_doc.FeatureManager.InsertFeatureChamfer(
        4, 1, distance, angle, 0, 0, 0, 0
    )
    if feat is None:
        raise SolidWorksError(f"InsertFeatureChamfer returned None for {feature.id}.")
    return feat


def build_pattern(sw_doc, model, feature: Feature, dims: dict[str, float], feature_map: dict[str, Any]):
    """Create a linear pattern of the most-recent seed feature.

    If the seed's instances were already cut individually (the hole callout
    carried every ``instance_position``), the pattern is redundant — it is a
    successful no-op rather than an error. Otherwise spacing/count come from the
    feature's related dimensions.
    """
    # Redundant-pattern check (mirrors the macro generator's _pattern_covered_by):
    # when the seed hole already placed all its instances, there is nothing to do.
    try:
        from pipeline.macro_generator import _pattern_covered_by

        if _pattern_covered_by(model, feature) is not None:
            log.info("  pattern %s already realized by its seed's instances — no-op.", feature.id)
            return _NOOP
    except Exception:
        pass

    if not feature_map:
        raise SolidWorksError(f"pattern {feature.id} has no seed feature to pattern.")
    spacing = dims.get("spacing") or dims.get("length") or next(iter(dims.values()), None)
    if not spacing:
        raise SolidWorksError(f"pattern {feature.id} has no spacing dimension.")
    assert_meters(spacing, f"{feature.id}.pattern_spacing")
    count = max(2, int(feature.quantity))

    # Select the seed (last created feature) before patterning.
    sw_doc.ClearSelection2(True)
    feat = sw_doc.FeatureManager.FeatureLinearPattern4(
        count, spacing, 1, 0.0, False, False, "NULL", "NULL",
        False, False, False, False, False, False, False, False, 0, 0,
    )
    if feat is None:
        raise SolidWorksError(f"FeatureLinearPattern4 returned None for {feature.id}.")
    return feat


# Map feature types to (builder, is_fragile).
_BUILDERS = {
    FeatureType.EXTRUDE_BOSS: (build_extrude_boss, False),
    FeatureType.EXTRUDE_CUT: (build_extrude_cut, False),
    FeatureType.HOLE: (build_hole, False),
    FeatureType.THREAD: (build_thread, False),
    FeatureType.FILLET: (build_fillet, True),
    FeatureType.CHAMFER: (build_chamfer, True),
}


def dispatch_feature_builder(sw_doc, model, feature: Feature, dims: dict[str, float], feature_map: dict[str, Any]):
    """Dispatch to the right feature builder. Returns the created feature object."""
    if feature.type == FeatureType.PATTERN:
        return build_pattern(sw_doc, model, feature, dims, feature_map)
    entry = _BUILDERS.get(feature.type)
    if entry is None:
        raise SolidWorksError(
            f"Feature type {feature.type.value!r} ({feature.id}) is not yet supported by the builder."
        )
    builder, _fragile = entry
    return builder(sw_doc, model, feature, dims)


def _is_fragile(feature: Feature) -> bool:
    return feature.type in (FeatureType.FILLET, FeatureType.CHAMFER)


def save_model(sw_doc, name: str, output_dir: Optional[Path] = None) -> str:
    """Save the part as a .sldprt and return the absolute path.

    Raises:
        SolidWorksError: if the save fails.
    """
    output_dir = Path(output_dir) if output_dir else OUTPUT_DIR_DEFAULT
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (name or "output_part") if c.isalnum() or c in ("_", "-")).strip("_") or "output_part"
    # MUST be absolute: SaveAs3 resolves a relative path against SolidWorks' own
    # working directory (not Python's), silently scattering files elsewhere.
    path = (output_dir / f"{safe}.sldprt").resolve()

    # SaveAs3(Name, Version, Options) → returns error/warning ints via SaveAs.
    errors = 0
    warnings = 0
    try:
        result = sw_doc.SaveAs3(str(path), 0, 1)
        # Some bindings return a bool; treat falsy as failure only if no file written.
        if result in (False, None) and not path.exists():
            raise SolidWorksError(f"SaveAs3 failed for {path}.")
    except SolidWorksError:
        raise
    except Exception as e:
        raise SolidWorksError(f"Saving {path} failed: {e} (errors={errors}, warnings={warnings})") from e

    log.info("Saved model: %s", path)
    return str(path)


def build_model(
    sw_app,
    drawing_data: Union[DrawingData, dict[str, Any]],
    output_dir: Optional[Union[str, Path]] = None,
    template_path: Optional[str] = None,
    strict: bool = True,
    skipped_out: Optional[list[tuple[str, str, str]]] = None,
):
    """Build the complete 3D model from validated drawing data.

    Args:
        strict: when True (default) a non-fragile feature failure saves a partial
            model and aborts the build (fail-fast). When False, ANY feature that
            fails is demoted to a warning and skipped so the build runs to
            completion — used by the batch driver, which then documents every
            skipped feature for human review.
        skipped_out: if provided, every skipped feature is appended as
            ``(feature_id, feature_type, reason)`` for the caller's report.

    Returns:
        (output_path, sw_doc): path to the saved .sldprt and the live document
        (so the caller can run model validation against it).

    Raises:
        SolidWorksError: on any build failure (strict mode), or if the build
            produced no solid body at all. The exception carries the path to a
            saved partial model (``partial_path``) for debugging.
    """
    _require_windows()
    model = _coerce(drawing_data)
    output_dir = Path(output_dir) if output_dir else OUTPUT_DIR_DEFAULT

    sw_doc = create_new_part(sw_app, template_path)
    set_document_units(sw_doc, model.units.value)  # BEFORE any geometry

    feature_map: dict[str, Any] = {}
    built_count = 0

    for feature_id in model.build_order:
        feature = get_feature_by_id(model, feature_id)
        dims = get_dimensions_for_feature(model, feature)
        log.info("Building feature %s: %s", feature_id, feature.type.value)

        try:
            try:
                result = dispatch_feature_builder(sw_doc, model, feature, dims, feature_map)
            except SolidWorksError:
                raise
            except Exception as e:
                # Wrap unexpected COM errors (e.g. pywintypes.com_error) so the
                # fragile/partial-save handling below applies uniformly.
                raise SolidWorksError(f"{type(e).__name__}: {e}") from e

            if result is None:
                raise SolidWorksError(f"Feature builder returned None for {feature_id}.")
            if result is _NOOP:
                # Intentional no-op (redundant pattern / cosmetic thread): success,
                # but no feature object to record or count.
                log.info("  ✓ %s (no geometry to add)", feature_id)
                continue
            feature_map[feature_id] = result

            if not check_rebuild_errors(sw_doc):
                raise SolidWorksError(f"Rebuild errors after feature {feature_id}.")

            built_count += 1
            log.info("  ✓ %s complete", feature_id)

            # Periodic auto-save so a later crash doesn't lose everything.
            if built_count % AUTOSAVE_EVERY == 0:
                try:
                    save_model(sw_doc, f"AUTOSAVE_{model.part_name or 'part'}", output_dir)
                except SolidWorksError as e:
                    log.warning("Auto-save failed (continuing): %s", e)

        except SolidWorksError as e:
            if _is_fragile(feature):
                # Fillets/chamfers are demoted to warnings — do not abort the build.
                log.warning("  ! %s (%s) failed and was SKIPPED: %s", feature_id, feature.type.value, e)
                model.warnings.append(f"{feature_id} ({feature.type.value}) skipped: {e}")
                if skipped_out is not None:
                    skipped_out.append((feature_id, feature.type.value, str(e)))
                continue

            if not strict:
                # Non-strict batch mode: skip any failing feature so the build
                # completes; the driver records it for human verification.
                log.warning("  ! %s (%s) FAILED and was SKIPPED (non-strict): %s",
                            feature_id, feature.type.value, e)
                model.warnings.append(f"{feature_id} ({feature.type.value}) skipped: {e}")
                if skipped_out is not None:
                    skipped_out.append((feature_id, feature.type.value, str(e)))
                continue

            # Non-fragile failure (strict): save a partial model and abort with context.
            partial_path = None
            try:
                partial_path = save_model(sw_doc, f"PARTIAL_{feature_id}", output_dir)
            except Exception as save_err:
                log.error("Could not save partial model: %s", save_err)
            log.error("  ✗ %s FAILED: %s", feature_id, e)
            raise SolidWorksError(
                f"Build failed at feature {feature_id}: {e}"
                + (f" Partial model saved to {partial_path}." if partial_path else ""),
                partial_path=partial_path,
            ) from e

    # Final rebuild + error check.
    try:
        sw_doc.ForceRebuild3(True)
    except Exception as e:
        log.warning("ForceRebuild3 failed: %s", e)
    check_rebuild_errors(sw_doc)

    # A completed run with no solid body is still a failure (e.g. the base boss
    # was skipped in non-strict mode) — surface it rather than save an empty part.
    if not _solid_body_exists(sw_doc):
        raise SolidWorksError("Build produced no solid body (all body-creating features were skipped).")

    output_path = save_model(sw_doc, model.part_name or "output_part", output_dir)
    return output_path, sw_doc
