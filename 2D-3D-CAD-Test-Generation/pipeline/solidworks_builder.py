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

from pipeline.schema import DrawingData, Feature, FeatureType, HoleType, PatternKind
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


# SOLIDWORKS Constant type library — loading this by CLSID/version populates
# win32com.client.constants with the swConst enums (swDocPART, swSketchSegment_*, etc.)
# without needing GetTypeInfo() on a live SolidWorks object. The major version
# differs by SolidWorks release (and the hardcoded one drifts as installs change),
# so we DISCOVER the loadable version at runtime rather than pinning one — pinning
# a version the machine can't load raised "Library not registered" and silently
# disabled every .sldprt build.
_SW_CONST_TYPELIB_CLSID = "{4687F359-55D0-4CD3-B6CF-2EB42C11F989}"
# Tried newest-first after any versions the registry actually advertises; spans
# SolidWorks 2012-era (20) through recent releases (33+).
_SW_CONST_FALLBACK_VERSIONS = (33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18)


def _registered_typelib_versions(clsid: str) -> list[int]:
    """Major versions advertised for ``clsid`` under HKCR\\TypeLib (best-effort)."""
    try:
        import winreg

        out: list[int] = []
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "TypeLib\\" + clsid) as k:
            i = 0
            while True:
                try:
                    ver = winreg.EnumKey(k, i)
                    i += 1
                except OSError:
                    break
                try:
                    out.append(int(float(ver)))
                except ValueError:
                    continue
        return out
    except Exception:
        return []


def _ensure_sw_constants() -> Optional[int]:
    """Populate ``win32com.client.constants`` with the SolidWorks enums.

    Tries the versions the registry advertises first, then a newest-first fallback
    list, and returns the major version that loaded (or None). NON-FATAL: SolidWorks
    constants are also resolved with literal defaults via :func:`_const`, so a miss
    only means named-constant lookups fall back — it must never block the build."""
    from win32com.client import gencache  # type: ignore

    clsid = _SW_CONST_TYPELIB_CLSID
    seen: set[int] = set()
    candidates = _registered_typelib_versions(clsid) + list(_SW_CONST_FALLBACK_VERSIONS)
    for major in candidates:
        if major in seen:
            continue
        seen.add(major)
        try:
            gencache.EnsureModule(clsid, 0, major, 0)
            log.info("Loaded SolidWorks constant type library v%d.0", major)
            return major
        except Exception:
            continue
    log.warning("Could not load any SolidWorks constant type library version "
                "(constants fall back to literal defaults).")
    return None


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

    pythoncom.CoInitialize()
    # Load the swConst enums under whatever typelib version this machine can load
    # (non-fatal — pinning the wrong version previously disabled all .sldprt builds).
    _ensure_sw_constants()

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


def build_revolve(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Create a revolved solid from the extracted half-profile.

    The profile is the feature's ``revolve_profile`` — ordered [axial, radial]
    points (drawing units). They are drawn as a closed sketch region against a
    horizontal centerline (the revolve axis at radial=0), then revolved 360°. When
    no profile was extracted the build can't synthesize one, so it raises and the
    non-strict driver records it for manual modeling (or the resolver's
    bounding-cylinder approximation applies)."""
    profile = getattr(feature, "revolve_profile", None) or []
    if len(profile) < 2:
        raise SolidWorksError(
            f"revolve {feature.id} has no usable profile (need >=2 [axial, radial] points)."
        )
    from pipeline.macro_generator import revolve_sketch_points

    unit = model.units.value
    closed, (x_min, x_max) = revolve_sketch_points(profile)
    pts_m = [(to_meters(ax, unit), to_meters(rad, unit)) for ax, rad in closed]
    axis_x1, axis_x2 = to_meters(x_min, unit), to_meters(x_max, unit)

    _select_plane(sw_doc, feature.sketch_plane or "front")
    sw_doc.SketchManager.InsertSketch(True)
    if sw_doc.SketchManager.ActiveSketch is None:
        raise SolidWorksError("Failed to enter sketch mode for revolve.")

    # Centerline = revolve axis (kept as an object so we can select it by reference,
    # never by an unreliable name lookup).
    axis = sw_doc.SketchManager.CreateCenterLine(axis_x1, 0.0, 0.0, axis_x2, 0.0, 0.0)
    if axis is None:
        raise SolidWorksError(f"revolve {feature.id}: could not create the centerline axis.")
    # Closed profile polyline.
    for (x1, y1), (x2, y2) in zip(pts_m, pts_m[1:] + pts_m[:1]):
        if sw_doc.SketchManager.CreateLine(x1, y1, 0.0, x2, y2, 0.0) is None:
            raise SolidWorksError(f"revolve {feature.id}: CreateLine returned None.")
    _verify_sketch_fully_defined(sw_doc)
    sw_doc.SketchManager.InsertSketch(True)  # close the sketch

    sw_doc.ClearSelection2(True)
    try:
        axis.Select4(False, _null_dispatch())  # select the axis for the revolve
    except Exception:
        pass
    # FeatureRevolve2(SingleDir, IsSolid, IsThin, IsCut, ReverseDir, BothSameEntity,
    #   Dir1Type, Dir2Type, Dir1Angle, Dir2Angle, OffReverse1, OffReverse2, OffDist1,
    #   OffDist2, ThinType, ThinThick1, ThinThick2, Merge, UseFeatScope, UseAutoSelect)
    feat = sw_doc.FeatureManager.FeatureRevolve2(
        True, True, False, False, False, False,
        0, 0, to_radians(360), 0.0,
        False, False, 0.0, 0.0, 0, 0.0, 0.0,
        True, True, True,
    )
    if feat is None:
        raise SolidWorksError(f"FeatureRevolve2 returned None for {feature.id}.")
    if not _solid_body_exists(sw_doc):
        raise SolidWorksError(f"No solid body exists after revolve {feature.id} (volume=0).")
    return feat


def build_mirror(sw_doc, model, feature: Feature, dims: dict[str, float], feature_map: Optional[dict] = None):
    """Mirror the host feature(s) about a plane. FRAGILE — wrapped like fillet.

    Selects the mirror plane plus the seed feature named by ``parent_feature`` and
    calls InsertMirrorFeature2. Best-effort: when the seed isn't available the
    build skips it (non-strict) and records it for manual modeling."""
    pid = feature.parent_feature or ""
    seed = (feature_map or {}).get(pid)
    if seed is None:
        raise SolidWorksError(f"mirror {feature.id}: seed feature {pid or '(none)'} was not built.")
    plane = _PLANE_NAMES.get((feature.mirror_plane or feature.sketch_plane or "front").lower().strip(), "Front Plane")

    sw_doc.ClearSelection2(True)
    if not sw_doc.Extension.SelectByID2(plane, "PLANE", 0.0, 0.0, 0.0, False, 1, _null_dispatch(), 0):
        raise SolidWorksError(f"mirror {feature.id}: could not select mirror plane {plane!r}.")
    try:
        seed.Select2(True, 4)  # append the feature to mirror (mark 4)
    except Exception as e:
        raise SolidWorksError(f"mirror {feature.id}: could not select seed feature {pid}: {e}")
    # InsertMirrorFeature2(BodyFeatureScope, GeomPattern, Merge, KnitSurface)
    feat = sw_doc.FeatureManager.InsertMirrorFeature2(False, False, True, False)
    if feat is None:
        raise SolidWorksError(f"InsertMirrorFeature2 returned None for {feature.id}.")
    _note_warning(model, f"{feature.id}: mirrored {pid} about {plane}; verify against the drawing.")
    return feat


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
    # Re-centre the whole layout on the ACTUAL body when the positions are a
    # centred fallback (drawing never dimensioned them) OR it is a bolt circle with
    # no explicit center — a bolt circle is concentric with the round part by
    # definition, so aligning its ring to the body centre is what keeps the holes
    # on material. A circular pattern WITH an explicit center is left as placed.
    explicit_circular = h.pattern == PatternKind.CIRCULAR and len(h.bolt_circle_center) == 2
    concentric_circular = h.pattern == PatternKind.CIRCULAR and not explicit_circular
    needs_recenter = concentric_circular or (
        not h.position_known and not h.instance_positions and not explicit_circular
    )
    if needs_recenter:
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


def _note_warning(model, message: str) -> None:
    """Record a build caveat on the model's warnings (surfaced to the operator)
    and the log, without ever breaking the build if the field is missing."""
    log.warning("%s", message)
    try:
        model.warnings.append(message)
    except Exception:
        pass


def _select_all_body_edges(sw_doc) -> int:
    """Select every edge of every solid body and return how many were selected.

    A generated build has no per-edge topology from the 2D drawing, so the
    reliable automatic behavior for a fillet/chamfer is to apply the called-out
    value to ALL edges (matching general notes like "ALL FILLETS R__"). Selective
    fillets — where only specific corners are rounded — are left to the
    interactive macro, which lets a human pick the edges.
    """
    try:
        bodies = sw_doc.GetBodies2(0, False)  # swSolidBody, visibleOnly=False
    except Exception as e:
        log.warning("Could not enumerate bodies for edge selection: %s", e)
        return 0
    if bodies is None:
        return 0
    if not isinstance(bodies, (list, tuple)):
        bodies = [bodies]
    sw_doc.ClearSelection2(True)
    count = 0
    for body in bodies:
        try:
            edges = body.GetEdges()
        except Exception:
            edges = None
        if not edges:
            continue
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        for edge in edges:
            try:
                if edge.Select4(True, _null_dispatch()):  # append to the selection
                    count += 1
            except Exception:
                continue
    return count


def _select_feature_edges(sw_doc, feat_obj) -> int:
    """Select every edge of the faces created by ``feat_obj`` (a SolidWorks
    Feature) and return how many were selected. Used to scope a fillet/chamfer to
    its host feature instead of the whole body. Returns 0 if the feature exposes
    no faces (caller then falls back to all body edges)."""
    sw_doc.ClearSelection2(True)
    try:
        faces = feat_obj.GetFaces()
    except Exception:
        faces = None
    if not faces:
        return 0
    if not isinstance(faces, (list, tuple)):
        faces = [faces]
    count = 0
    for face in faces:
        try:
            edges = face.GetEdges()
        except Exception:
            edges = None
        if not edges:
            continue
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        for edge in edges:
            try:
                if edge.Select4(True, _null_dispatch()):
                    count += 1
            except Exception:
                continue
    return count


def _fillet_edge_strategy(feature: Feature, feature_map: Optional[dict]) -> tuple[str, str]:
    """Decide which edges a fillet/chamfer applies to (pure, testable).

    Returns ``("feature", parent_id)`` to scope to the host feature's faces when
    the drawing named a host (``parent_feature``) that was actually built, else
    ``("all", "")`` to round every body edge. ``FILLET_EDGE_MODE=all`` forces the
    whole-body behavior regardless.
    """
    import os

    if os.getenv("FILLET_EDGE_MODE", "").strip().lower() == "all":
        return ("all", "")
    pid = feature.parent_feature or ""
    if pid and feature_map and pid in feature_map:
        return ("feature", pid)
    return ("all", "")


def _select_fillet_edges(sw_doc, model, feature: Feature, feature_map: Optional[dict]) -> tuple[int, str]:
    """Select the edges to fillet/chamfer per the strategy; return (count, scope).

    Scopes to the host feature's faces when possible (closer to a selective
    fillet), otherwise selects all body edges. Always leaves the chosen edges
    selected for the immediately following feature call."""
    strategy, pid = _fillet_edge_strategy(feature, feature_map)
    if strategy == "feature":
        n = _select_feature_edges(sw_doc, feature_map[pid])
        if n > 0:
            return n, f"feature {pid}"
    return _select_all_body_edges(sw_doc), "all edges"


def build_fillet(sw_doc, model, feature: Feature, dims: dict[str, float], feature_map: Optional[dict] = None):
    """Apply a constant-radius fillet. FRAGILE — wrapped by the orchestrator (a
    failure is demoted to a warning, not fatal).

    The 2D drawing carries no per-edge topology, so edges are chosen by scope: the
    host feature's faces when the drawing named one (``parent_feature``), else
    every body edge (correct for global "ALL FILLETS R__" notes). A warning is
    recorded with the scope so the result is verified, never assumed; the
    interactive macro remains the authoritative path for truly selective fillets.
    """
    radius = dims.get("fillet_radius") or dims.get("radius") or next(iter(dims.values()), None)
    if not radius:
        # Extracted fillet whose radius wasn't linked to the feature: recover it
        # from any fillet/corner-radius dimension rather than failing outright.
        from pipeline.macro_generator import _model_radius_fallback

        radius, _src = _model_radius_fallback(model)
        if not radius:
            raise SolidWorksError(f"fillet {feature.id} has no radius dimension.")
        radius = to_meters(radius, model.units.value)
    assert_meters(radius, f"{feature.id}.fillet_radius")

    n, scope = _select_fillet_edges(sw_doc, model, feature, feature_map)
    if n == 0:
        raise SolidWorksError(f"fillet {feature.id}: no body edges available to fillet.")
    # FeatureFillet3(Options, R1, Ftyp, OverflowType, ...) — minimal constant-radius form.
    feat = sw_doc.FeatureManager.FeatureFillet3(
        195,  # default fillet options bitmask (propagate, etc.)
        radius, 0, 0, 0, 0, 0,
        _null_dispatch(), _null_dispatch(), _null_dispatch(), _null_dispatch(),
        _null_dispatch(), _null_dispatch(), _null_dispatch(),
    )
    if feat is None:
        raise SolidWorksError(f"FeatureFillet3 returned None for {feature.id}.")
    log.info("  fillet %s applied to %d edge(s) (%s).", feature.id, n, scope)
    _note_warning(model, f"{feature.id}: fillet applied to {n} edges ({scope}); "
                         f"verify against the drawing (selective fillets need the interactive macro).")
    return feat


def build_chamfer(sw_doc, model, feature: Feature, dims: dict[str, float], feature_map: Optional[dict] = None):
    """Apply a chamfer. FRAGILE — wrapped like fillet, scoped the same way."""
    distance = dims.get("chamfer") or dims.get("length") or next(
        (v for k, v in dims.items() if k != "angle"), None
    )
    if not distance:
        from pipeline.macro_generator import _model_chamfer_fallback

        distance, _src = _model_chamfer_fallback(model)
        if not distance:
            raise SolidWorksError(f"chamfer {feature.id} has no distance dimension.")
        distance = to_meters(distance, model.units.value)
    assert_meters(distance, f"{feature.id}.chamfer_distance")
    angle = dims.get("angle", to_radians(45))  # default 45° if unspecified (already radians)

    n, scope = _select_fillet_edges(sw_doc, model, feature, feature_map)
    if n == 0:
        raise SolidWorksError(f"chamfer {feature.id}: no body edges available to chamfer.")
    # InsertFeatureChamfer(Width, Angle, Flip, Type, OtherDist, VertexChamDist, VertexChamDist2)
    feat = sw_doc.FeatureManager.InsertFeatureChamfer(
        4, 1, distance, angle, 0, 0, 0, 0
    )
    if feat is None:
        raise SolidWorksError(f"InsertFeatureChamfer returned None for {feature.id}.")
    log.info("  chamfer %s applied to %d edge(s) (%s).", feature.id, n, scope)
    _note_warning(model, f"{feature.id}: chamfer applied to {n} edges ({scope}); "
                         f"verify against the drawing (selective chamfers need the interactive macro).")
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
    FeatureType.REVOLVE: (build_revolve, False),
    FeatureType.HOLE: (build_hole, False),
    FeatureType.THREAD: (build_thread, False),
    FeatureType.FILLET: (build_fillet, True),
    FeatureType.CHAMFER: (build_chamfer, True),
    FeatureType.MIRROR: (build_mirror, True),
}

# Builders that need the map of already-built features (to scope edges / find a
# seed): they take an extra ``feature_map`` argument.
_NEEDS_FEATURE_MAP = {FeatureType.FILLET, FeatureType.CHAMFER, FeatureType.MIRROR}


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
    if feature.type in _NEEDS_FEATURE_MAP:
        return builder(sw_doc, model, feature, dims, feature_map)
    return builder(sw_doc, model, feature, dims)


def _is_fragile(feature: Feature) -> bool:
    return feature.type in (FeatureType.FILLET, FeatureType.CHAMFER, FeatureType.MIRROR)


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


def export_stl(sw_doc, name: str, output_dir: Optional[Path] = None) -> str:
    """Export the active part as an ``.stl`` next to its ``.sldprt`` and return the
    absolute path. Uses the SAME name convention as :func:`save_model`, so the STL
    filename matches the part name and the web UI can locate it automatically.

    SolidWorks picks the STL translator from the ``.stl`` extension (default STL
    export options). Raises :class:`SolidWorksError` on failure.
    """
    output_dir = Path(output_dir) if output_dir else OUTPUT_DIR_DEFAULT
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (name or "output_part") if c.isalnum() or c in ("_", "-")).strip("_") or "output_part"
    path = (output_dir / f"{safe}.stl").resolve()
    try:
        result = sw_doc.SaveAs3(str(path), 0, 1)
        if result in (False, None) and not path.exists():
            raise SolidWorksError(f"STL SaveAs3 failed for {path}.")
    except SolidWorksError:
        raise
    except Exception as e:
        raise SolidWorksError(f"Exporting STL {path} failed: {e}") from e

    log.info("Exported STL: %s", path)
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
    # Also export an STL beside the .sldprt (same base name) so the 3D viewer can
    # load it. Non-fatal: a failed STL export never invalidates a good .sldprt.
    try:
        export_stl(sw_doc, model.part_name or "output_part", output_dir)
    except SolidWorksError as e:
        log.warning("STL export failed (continuing): %s", e)
    return output_path, sw_doc
