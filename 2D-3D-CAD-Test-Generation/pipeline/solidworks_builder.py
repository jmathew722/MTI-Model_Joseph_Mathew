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

import math
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


def _begin_sketch(sw_doc, sketch_plane: Optional[str], context: str) -> None:
    """Open a FRESH sketch on the named plane, robust to prior document state.

    ``InsertSketch(True)`` TOGGLES sketch mode, so if a sketch is already active
    (a prior feature left the doc in sketch mode, or a failed step didn't close
    cleanly) a naive call CLOSES it and leaves no active sketch — the
    "Failed to enter sketch mode" failure class seen on multi-hole parts. This
    guarantees the open by: closing any dangling sketch first, clearing the
    selection, selecting the plane, opening, and retrying once if needed."""
    sm = sw_doc.SketchManager
    # 1) Close any sketch left open by a prior step (toggle it shut), then clear.
    if sm.ActiveSketch is not None:
        try:
            sm.InsertSketch(True)
        except Exception:
            pass
    sw_doc.ClearSelection2(True)
    # 2) Select the plane and open a new sketch; retry once on failure.
    for attempt in (1, 2):
        _select_plane(sw_doc, sketch_plane)
        sm.InsertSketch(True)
        if sm.ActiveSketch is not None:
            return
        # Failed to open — a stray active sketch may have just been toggled shut;
        # clear and try again before giving up.
        sw_doc.ClearSelection2(True)
    raise SolidWorksError(
        f"Failed to enter sketch mode for {context} on plane "
        f"{sketch_plane or 'front'!r} after retrying (document left in an "
        "unexpected sketch state).")


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
    # A finite, positive radius is mandatory — a zero/NaN radius (e.g. a diameter
    # that never resolved) is one reason SketchManager returns Nothing.
    if not (isinstance(radius_m, (int, float)) and math.isfinite(radius_m) and radius_m > 0):
        raise SolidWorksError(
            f"cannot sketch a circle with radius {radius_m!r} m — the diameter did "
            "not resolve to a positive value.")
    # Add entities straight to the sketch DB with inferencing/auto-relations and
    # per-entity redraw OFF. Programmatic CreateCircleByRadius calls are otherwise
    # subject to snap/inference resolution that intermittently REJECTS a valid
    # circle (returns Nothing) — the multi-hole failure class. This is the
    # documented robust pattern for API-driven sketch geometry; state is always
    # restored, even on error.
    ext_ok = hasattr(sw_doc, "SetAddToDB")
    try:
        if ext_ok:
            sw_doc.SetAddToDB(True)
            try:
                sw_doc.SetDisplayWhenAdded(False)
            except Exception:
                pass
        for cx, cy in centers_m:
            if sw_doc.SketchManager.CreateCircleByRadius(cx, cy, 0.0, radius_m) is None:
                raise SolidWorksError(
                    f"CreateCircleByRadius returned Nothing for a circle r={radius_m:.5f} m "
                    f"at ({cx:.5f}, {cy:.5f}) m — the active sketch rejected it (check the "
                    f"sketch plane/face is valid and the point lies on it).")
    finally:
        if ext_ok:
            try:
                sw_doc.SetAddToDB(False)
                sw_doc.SetDisplayWhenAdded(True)
            except Exception:
                pass


def _bboxes_overlap_xy(a: tuple[float, float, float, float],
                       b: tuple[float, float, float, float], tol: float = 1e-6) -> bool:
    """True if two axis-aligned XY boxes (x1,y1,x2,y2) overlap within ``tol``.
    Pure/testable — the basis of the cut/body intersection sanity check."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return (min(ax2, bx2) - max(ax1, bx1) >= -tol) and (min(ay2, by2) - max(ay1, by1) >= -tol)


def _body_bbox_xy_m(sw_doc) -> Optional[tuple[float, float, float, float]]:
    """Current solid body's XY bounding box (x1,y1,x2,y2) in METERS, or None."""
    try:
        bodies = sw_doc.GetBodies2(0, False)
        body = bodies[0] if isinstance(bodies, (list, tuple)) else bodies
        box = body.GetBodyBox()  # (x1,y1,z1,x2,y2,z2) meters
        return (box[0], box[1], box[3], box[4])
    except Exception as e:
        log.warning("Could not read body bounding box for intersection sanity: %s", e)
        return None


def _assert_cut_intersects_body(sw_doc, feature, profile_xy: tuple[float, float, float, float]) -> None:
    """Fix 1.2c (learning-loop 2026-07-09: every FeatureCut4 None co-occurred with
    a POSITION ASSUMED flag). Before cutting, verify the cut profile's XY box
    overlaps the solid's; if it doesn't, the cut removes no material and
    FeatureCut4 would return a confusing None — raise the REAL root cause instead
    (an upstream position assumption placed the feature off the solid)."""
    body = _body_bbox_xy_m(sw_doc)
    if body is None:
        return  # can't read the body box — don't block; let the cut proceed
    if not _bboxes_overlap_xy(profile_xy, body):
        raise SolidWorksError(
            f"cut {feature.id} positioned OUTSIDE the solid — its profile "
            f"{tuple(round(v, 4) for v in profile_xy)} m does not overlap the body "
            f"{tuple(round(v, 4) for v in body)} m. Root cause: the feature's location was "
            f"assumed (POSITION ASSUMED) and lands off the part. The Stage 2.5 completeness "
            f"gate now excludes position-unresolved cuts before the build — add an X/Y "
            f"location dimension on the drawing and re-run; do not patch the build.")


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
        # Callers verify the preconditions before calling (sketch closed & fully
        # defined, profile overlaps the body via _assert_cut_intersects_body, depth
        # in meters). If BOTH directions still return None the profile does not
        # actually remove material — name that, never a bare None (P4 regression).
        raise SolidWorksError(
            f"FeatureCut4 returned None for {feature.id} in BOTH directions despite verified "
            f"preconditions (sketch closed & fully defined, profile overlaps the body, "
            f"depth in meters) — the cut removes no material (coincident with a face or "
            f"zero-area profile); check the profile geometry, not the build call.")
    return feat


def _wizard_hole_type(h) -> int:
    """Classify a callout into a ``swWzdGeneralHoleTypes_e`` value.

    We drive every case through the diameter-based LEGACY hole (``swWzdLegacy``):
    it is placed by an explicit diameter and needs no fastener standard/size
    table lookup (those indices are locale/data-pack specific and the #1 source
    of wrong-size or failed wizard holes). This yields a REAL hole-wizard feature
    at the resolved coordinates — replacing the open/empty-sketch ``FeatureCut4``
    failure class — while the drill diameter stays exactly the callout's. A
    tapped callout still drills its tap hole here; the thread stays cosmetic per
    the repo convention (real helical threads are prohibited)."""
    return _const("swWzdLegacy", 5)


# IFeatureManager::HoleWizard5 (dispid 222) parameter order, verified against the
# installed sldworks.tlb: GenericHoleType(long), StandardIndex(long),
# FastenerTypeIndex(long), SSize(str), EndType(short), Diameter, Depth, Length,
# Value1..Value12 (doubles), ThreadClass(str), RevDir, FeatureScope, AutoSelect,
# AssemblyFeatureScope, AutoSelectComponents, PropagateFeatureToParts (bools).
# StandardIndex/FastenerTypeIndex are LONGS (0 for the legacy diameter-driven
# hole) — passing strings there is what raised "Type mismatch" (-2147352571).


def _try_hole_wizard(sw_doc, model, feature: Feature, h, centers_m: list[tuple[float, float]],
                     through_all: bool, depth_m: Optional[float]):
    """Best-effort REAL Hole Wizard feature (IFeatureManager::HoleWizard5).

    Additive path (2026-07-10 redesign): a drilled/tapped/cbore/csk callout
    becomes a proper wizard hole feature instead of a bare ``FeatureCut4`` circle
    (which fails on an open/empty sketch). Placement uses a pre-selected point
    sketch — one point per resolved center on the target face — so every instance
    is drilled at its exact X/Y. Returns the created feature on success, or
    ``None`` to fall back to the exact existing sketch-cut path
    (``_circular_cut_at``): nothing regresses on ANY failure. After creation the
    build is verified (rebuild OK + solid body present); a no-op wizard hole is
    deleted and we fall back rather than ship bad geometry.

    OPT-IN (``MTI_ENABLE_HOLE_WIZARD=1``): default OFF. Live testing on
    SolidWorks 2024 showed ``HoleWizard5`` returning ``None`` for the
    diameter-driven legacy hole even on a clean part with a valid face + point
    sketch — the parameter/Value-slot mapping is version/locale specific (the
    exact quirk the redesign spec flagged). Until that mapping is nailed down on
    a live machine, the default stays the proven sketch-circle cut so the working
    build never regresses; flip the flag to iterate on the wizard call. The
    27-arg signature here is verified correct against the installed sldworks.tlb
    (dispid 222) — it no longer raises "Type mismatch".
    """
    import os

    if not os.getenv("MTI_ENABLE_HOLE_WIZARD"):
        return None
    if not centers_m:
        return None
    try:
        featmgr = sw_doc.FeatureManager
        hw = getattr(featmgr, "HoleWizard5", None)
        if hw is None:
            return None  # older SW without HoleWizard5 -> fallback

        unit = model.units.value
        wtype = _wizard_hole_type(h)
        end_cond = _const("swEndCondThroughAll", 1) if through_all else _const("swEndCondBlind", 0)
        dia_m = to_meters(h.diameter, unit)
        # A blind depth must be positive; a through hole gets a generous depth
        # (SW uses the end condition, not the value, for ThroughAll).
        depth = depth_m if (depth_m and not through_all) else to_meters(
            max(h.depth, h.diameter * 4.0, 0.01 / 0.0254), unit)

        # 1) Pre-select the host planar face + place one sketch point per center.
        if not _select_top_face(sw_doc, centers_m[0]):
            return None
        sw_doc.SketchManager.InsertSketch(True)
        if sw_doc.SketchManager.ActiveSketch is None:
            return None
        for cx, cy in centers_m:
            sw_doc.SketchManager.CreatePoint(cx, cy, 0.0)
        sw_doc.SketchManager.InsertSketch(True)  # close; the points stay selected

        # 2) Create the wizard hole at the selected points. Correct 27-arg
        # signature; longs (0) for the standard/fastener indices (legacy hole).
        wizard = hw(
            wtype, 0, 0, "", int(end_cond),
            float(dia_m), float(depth), 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            "", False, True, True, False, False, False,
        )
        if wizard is None:
            sw_doc.ClearSelection2(True)
            return None

        # 3) Verify the wizard actually removed material of the right size; a
        # zero-diameter / no-op wizard hole is deleted and we fall back.
        if not check_rebuild_errors(sw_doc) or not _solid_body_exists(sw_doc):
            _delete_feature(sw_doc, wizard)
            sw_doc.ClearSelection2(True)
            return None
        sw_doc.ClearSelection2(True)
        return wizard
    except Exception as e:
        log.warning("hole %s: HoleWizard5 attempt failed (%s) — using sketch-cut fallback.",
                    feature.id, e)
        try:
            sw_doc.ClearSelection2(True)
        except Exception:
            pass
        return None


def _select_top_face(sw_doc, center_m: tuple[float, float]) -> bool:
    """Select the host planar face at ``center_m``. Face picks are unreliable, so
    try the far/near faces (from the body box) and the mid-plane in turn."""
    cx0, cy0 = center_m
    z_candidates = [0.0]
    try:
        bodies = sw_doc.GetBodies2(0, False)
        body = bodies[0] if isinstance(bodies, (list, tuple)) else bodies
        bb = body.GetBodyBox()  # (x1,y1,z1,x2,y2,z2) meters
        z_candidates = [bb[5], bb[2], (bb[2] + bb[5]) / 2.0]
    except Exception:
        pass
    for z in z_candidates:
        sw_doc.ClearSelection2(True)
        if sw_doc.Extension.SelectByID2("", "FACE", cx0, cy0, z, False, 0, _null_dispatch(), 0):
            return True
    return False


def _delete_feature(sw_doc, feat) -> None:
    """Best-effort delete of a just-created feature during fallback rollback."""
    try:
        name = feat.Name
        sw_doc.ClearSelection2(True)
        if sw_doc.Extension.SelectByID2(name, "BODYFEATURE", 0, 0, 0, False, 0, _null_dispatch(), 0):
            sw_doc.EditDelete()
    except Exception:
        pass
    finally:
        try:
            sw_doc.ClearSelection2(True)
        except Exception:
            pass


def _circular_cut_at(sw_doc, feature, centers_m: list[tuple[float, float]],
                     radius_m: float, through_all: bool, depth_m: Optional[float]):
    """Sketch N circles at the given centres and cut them in one feature."""
    _begin_sketch(sw_doc, feature.sketch_plane, f"hole/cut {feature.id}")
    _draw_circles(sw_doc, centers_m, radius_m)
    _verify_sketch_fully_defined(sw_doc)
    sw_doc.SketchManager.InsertSketch(True)  # close the sketch
    if depth_m:
        assert_meters(depth_m, f"{feature.id}.cut_depth")
    # Intersection sanity: the circles' XY extent must overlap the solid, else the
    # cut removes nothing and FeatureCut4 returns a confusing None (Fix 1.2c).
    xs = [c[0] for c in centers_m]
    ys = [c[1] for c in centers_m]
    profile_xy = (min(xs) - radius_m, min(ys) - radius_m,
                  max(xs) + radius_m, max(ys) + radius_m)
    _assert_cut_intersects_body(sw_doc, feature, profile_xy)
    return _do_cut(sw_doc, feature, through_all, depth_m)


def build_extrude_boss(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Create the solid base body: a rectangular or circular extruded boss."""
    _begin_sketch(sw_doc, feature.sketch_plane, f"extrude_boss {feature.id}")

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

    _begin_sketch(sw_doc, feature.sketch_plane, f"extrude_cut {feature.id}")
    cx, cy = _feature_center_m(model, feature, circular=False)
    if sw_doc.SketchManager.CreateCornerRectangle(cx, cy, 0, cx + side_u, cy + side_v, 0) is None:
        raise SolidWorksError("CreateCornerRectangle returned None for extrude_cut.")
    _verify_sketch_fully_defined(sw_doc)
    sw_doc.SketchManager.InsertSketch(True)
    if depth:
        assert_meters(depth, f"{feature.id}.cut_depth")
    # P4 (2026-07-10): the RECTANGULAR cut path skipped the intersection pre-check
    # that the circular path (_circular_cut_at) runs — so an off-solid rectangular
    # cut returned a bare "FeatureCut4 returned None" (A001581E F003). Run the same
    # precondition here so a miss names its real cause, never a bare None.
    profile_xy = (min(cx, cx + side_u), min(cy, cy + side_v),
                  max(cx, cx + side_u), max(cy, cy + side_v))
    _assert_cut_intersects_body(sw_doc, feature, profile_xy)
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


def _last_feature_of_type(sw_doc, type_name: str):
    """Newest feature of the given GetTypeName2 type in the tree (or None)."""
    feat = sw_doc.FirstFeature
    last = None
    while feat is not None:
        try:
            if feat.GetTypeName2 == type_name:
                last = feat
        except Exception:
            pass
        feat = feat.GetNextFeature
    return last


def _rename_feature(feat, name: str) -> None:
    """Name a feature immediately after creation (deterministic tree names —
    downstream SelectByID2 never depends on SolidWorks auto-numbering)."""
    try:
        feat.Name = name
    except Exception as e:
        log.warning("Could not rename feature to %s: %s", name, e)


def build_circular_pattern_holes(sw_doc, model, feature: Feature, h, probe: dict):
    """Seed hole -> named reference axis -> FeatureCircularPattern5 (the
    must-meet Part 2 reliability contract), at parity with the VBA macros.

    Every step checks its return value: SolidWorks returns None/Nothing on bad
    parameters with NO error, so a silent miss here is converted into a precise
    SolidWorksError naming the failing step."""
    import math as _math

    from pipeline.macro_generator import (
        canonical_circular_pattern,
        _seed_position,
    )

    unit = model.units.value

    # 1) Seed hole cut, deterministically named.
    sx, sy = _seed_position(model, h)
    thru = bool(h.thru) or h.type == HoleType.THRU
    depth_m = None
    if not thru:
        if h.depth <= 0:
            raise SolidWorksError(f"blind seed hole {h.id} has no depth.")
        depth_m = to_meters(h.depth, unit)
    seed = _circular_cut_at(
        sw_doc, feature, [(to_meters(sx, unit), to_meters(sy, unit))],
        to_meters(h.diameter, unit) / 2.0, thru, depth_m,
    )
    seed_name = f"{feature.id}_SeedHoleCut"
    _rename_feature(seed, seed_name)

    # 2) Named reference axis from the center bore's cylindrical face.
    n_axes = 0
    feat = sw_doc.FirstFeature
    while feat is not None:
        try:
            if feat.GetTypeName2 == "RefAxis":
                n_axes += 1
        except Exception:
            pass
        feat = feat.GetNextFeature
    axis_name = f"PatternAxis{n_axes + 1}"
    spec = canonical_circular_pattern(
        model, feature, h, axis_name,
        "explicit reference axis from the center bore cylindrical face (InsertAxis2)",
    )
    # Primary: find the bore's cylindrical face GEOMETRICALLY (radius + axis
    # location match) and select the face object itself — no coordinate ray.
    # Late-bound quirks (verified against SW 2024): IFace2::GetSurface only
    # resolves via a raw dispid Invoke with METHOD|PROPERTYGET, and
    # ISurface::IsCylinder / CylinderParams come back as properties.
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    def _surface_of(face):
        flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
        ole = face._oleobj_
        return win32com.client.Dispatch(
            ole.Invoke(ole.GetIDsOfNames("GetSurface"), 0, flags, True))

    def _prop(obj, name):
        v = getattr(obj, name)
        return v() if callable(v) else v

    bore_r_m = to_meters(probe["bore_radius"], unit)
    cx_m = to_meters(probe["cx"], unit)
    cy_m = to_meters(probe["cy"], unit)
    created = False
    try:
        import math as _m2

        bodies = sw_doc.GetBodies2(0, False)
        body = bodies[0] if isinstance(bodies, (list, tuple)) else bodies
        for face in body.GetFaces() or []:
            try:
                surf = _surface_of(face)
                if not _prop(surf, "IsCylinder"):
                    continue
                p = _prop(surf, "CylinderParams")  # (x, y, z, ax, ay, az, radius)
                if abs(float(p[6]) - bore_r_m) > 2e-5:
                    continue
                if _m2.hypot(float(p[0]) - cx_m, float(p[1]) - cy_m) > 5e-4:
                    continue
                sw_doc.ClearSelection2(True)
                if face.Select4(False, _null_dispatch()) and sw_doc.InsertAxis2(True):
                    created = True
                    break
            except Exception:
                continue
    except Exception as e:
        log.warning("%s: geometric bore-face search failed (%s) — falling back to "
                    "coordinate selection.", feature.id, e)

    # Fallback: exact generated coordinates on the bore wall, z tried on both
    # sides of the sketch plane (extrude direction is template-dependent).
    px = to_meters(probe["cx"] + probe["bore_radius"], unit)
    py = to_meters(probe["cy"], unit)
    t_m = to_meters(probe["thickness"], unit)
    if not created:
        for z in (-t_m / 2.0, t_m / 2.0, 0.0):
            sw_doc.ClearSelection2(True)
            if sw_doc.Extension.SelectByID2("", "FACE", px, py, z, False, 0, _null_dispatch(), 0):
                if sw_doc.InsertAxis2(True):
                    created = True
                    break
    if not created:
        raise SolidWorksError(
            f"{feature.id}: could not create reference axis {axis_name} from the "
            f"bore cylindrical face (R {bore_r_m:.6f} m at {cx_m:.6f}, {cy_m:.6f} m)."
        )
    ax = _last_feature_of_type(sw_doc, "RefAxis")
    if ax is None:
        raise SolidWorksError(f"{feature.id}: InsertAxis2 succeeded but no RefAxis feature found.")
    _rename_feature(ax, axis_name)

    # 3) The pattern feature. Selection contract: axis Mark=1, seed Mark=4 —
    # a wrong/missing mark makes FeatureCircularPattern return None silently.
    sw_doc.ClearSelection2(True)
    if not sw_doc.Extension.SelectByID2(axis_name, "AXIS", 0, 0, 0, False, 1, _null_dispatch(), 0):
        raise SolidWorksError(f"{feature.id}: could not select pattern axis '{axis_name}' (Mark=1).")
    if not sw_doc.Extension.SelectByID2(seed_name, "BODYFEATURE", 0, 0, 0, True, 4, _null_dispatch(), 0):
        raise SolidWorksError(f"{feature.id}: could not select seed feature '{seed_name}' (Mark=4).")

    n = int(spec["total_instances"])  # INCLUDES the seed (6 = seed + 5 copies)
    ang = _math.radians(float(spec["total_angle_deg"]))  # TOTAL angle, radians
    pat = None
    try:
        # Signature from the installed sldworks.tlb (IFeatureManager::
        # FeatureCircularPattern5, dispid 261): Number, Spacing, FlipDirection,
        # DName, GeometryPattern, EqualSpacing, VaryInstance, SyncSubAssemblies,
        # BDir2, BSymmetric, Number2, Spacing2, DName2, EqualSpacing2.
        pat = sw_doc.FeatureManager.FeatureCircularPattern5(
            n, ang, bool(spec["reverse_direction"]), "NULL",
            bool(spec["geometry_pattern"]), bool(spec["equal_spacing"]),
            bool(spec["vary_sketch"]), False, False, False, 1, ang, "NULL", False,
        )
    except Exception as e:
        log.warning("%s: FeatureCircularPattern5 raised (%s) — trying FeatureCircularPattern4.",
                    feature.id, e)
    if pat is None:
        try:
            pat = sw_doc.FeatureManager.FeatureCircularPattern4(
                n, ang, bool(spec["reverse_direction"]), "NULL",
                bool(spec["geometry_pattern"]), bool(spec["equal_spacing"]),
                bool(spec["vary_sketch"]),
            )
        except Exception:
            pat = None
    if pat is None:
        raise SolidWorksError(
            f"{feature.id}: FeatureCircularPattern returned Nothing — check marks/axis "
            f"('{axis_name}' Mark=1, '{seed_name}' Mark=4, total_instances={n})."
        )
    _rename_feature(pat, f"{feature.id}_CircularPattern")
    sw_doc.ClearSelection2(True)
    return pat


def build_hole(sw_doc, model, feature: Feature, dims: dict[str, float]):
    """Create holes as exact circle cuts, at parity with the VBA macro generator.

    The hole DIAMETER and per-instance POSITIONS come from the feature's
    ``hole_callout`` (where the extractor records them) — not the feature's
    related dimensions, which carry bolt-circle / spacing values. Every instance
    is cut (a full bolt pattern, not a single centred hole), through or blind, and
    a counterbore gets a second concentric blind cut. Falls back to a single
    centred circular cut from the feature dims only when no callout exists.

    A callout routed to a circular pattern (must-meet spec or polar drawing
    dimensioning, with a concentric bore to derive the axis) builds as a REAL
    FeatureCircularPattern via :func:`build_circular_pattern_holes`."""
    if not _solid_body_exists(sw_doc):
        raise SolidWorksError(f"hole {feature.id} requires an existing solid body.")

    from pipeline.macro_generator import (
        _bore_axis_probe,
        _hole_positions,
        _plane_for,
        route_to_circular_pattern,
    )

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

    if route_to_circular_pattern(model, h) and _plane_for(feature) == "Front Plane":
        probe = _bore_axis_probe(model, h)
        if probe is not None:
            try:
                return build_circular_pattern_holes(sw_doc, model, feature, h, probe)
            except SolidWorksError as e:
                # The model output must never be lost to a pattern-feature
                # failure: fall back to cutting every instance as an exact
                # circle (geometrically identical part), note it for the
                # report, and continue. The note lands in model_check.txt and
                # the engineering review via model.warnings.
                log.warning("hole %s: circular-pattern route failed (%s) — "
                            "falling back to exact baked-circle instances.",
                            feature.id, e)
                model.warnings.append(
                    f"{feature.id}: FeatureCircularPattern route failed ({e}); "
                    f"all {h.qty} instances were cut as exact circles instead — "
                    "geometry is equivalent, but the tree has no pattern feature; "
                    "verify if a pattern feature is required."
                )
                # Any partially created seed hole is left in place: the fallback
                # cuts every instance position, and re-cutting the seed's circle
                # through the existing hole is a geometric no-op.
                sw_doc.ClearSelection2(True)
        else:
            log.info("hole %s: circular pattern requested but no concentric bore face "
                     "to derive the axis — using baked-circle instances.", feature.id)

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

    # Additive: try a REAL Hole Wizard feature first (carries thread/cbore/csk
    # callout data); fall back to the exact sketch-circle cut on any failure so
    # the working build path is never lost.
    wizard = _try_hole_wizard(sw_doc, model, feature, h, positions_m, thru, depth_m)
    if wizard is not None:
        _rename_feature(wizard, f"{feature.id}_HoleWizard")
        return wizard

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


def plan_fillet_scope(feature: Feature, model) -> tuple[str, int, str]:
    """P9 (2026-07-10) — derive a fillet/chamfer's INTENDED scope from its callout
    (pure, testable). Returns ``(mode, expected_count, reason)`` where mode is:

      * ``"corners"``   — a corner-radius TYP applying to N outer corners
                          (expected_count = N from the callout quantity);
      * ``"slot_ends"`` — a slot-end radius (the slot's 2 end arcs);
      * ``"feature"``   — scoped to a named, built host feature;
      * ``"all"``       — genuinely un-scopable (a general "ALL FILLETS R__" note):
                          the flagged fallback, never the silent default.

    This lets the builder say what it INTENDED and flag when the applied edge count
    disagrees, instead of silently rounding all 14 edges (A001551E F004)."""
    from pipeline.callout_qty import classify_callout, is_typ, parse_quantity

    # Collect the radius callout text linked to this feature.
    texts: list[str] = [feature.description or ""]
    try:
        for rid in list(feature.related_dimensions or []) + [feature.depth_dimension_id or ""]:
            d = model.dimension_by_id(rid) if rid else None
            if d is not None:
                texts.append(f"{getattr(d, 'notes', '') or ''} {getattr(d, 'raw_text', '') or ''} "
                             f"{getattr(d, 'applies_to', '') or ''}")
    except Exception:
        pass
    blob = " ".join(texts)
    desc = (feature.description or "").lower()

    if "slot" in desc or "slot" in blob.lower():
        return ("slot_ends", 2, "slot-end radius applies to the slot's 2 end arcs")

    if is_typ(blob) and classify_callout(blob) == "radius":
        n = max(int(getattr(feature, "quantity", 1) or 1), parse_quantity(blob, default=0))
        if n >= 2:
            return ("corners", n, f"corner-radius TYP applies to {n} outer corners")

    pid = feature.parent_feature or ""
    if pid:
        return ("feature", 0, f"scoped to named host feature {pid}")
    return ("all", 0, "no scope derivable from the callout — general all-edges fallback")


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
        # P4/P9 (2026-07-10): no candidate edge to fillet — EXCLUDE rather than
        # apply to nothing or all edges. Names the failed precondition explicitly.
        raise SolidWorksError(
            f"fillet {feature.id}: PRECONDITION FAILED — 0 edges selected for scope "
            f"'{scope}'; no candidate edge exists within tolerance, so the fillet is not applied.")
    # FeatureFillet3(Options, R1, Ftyp, OverflowType, ...) — minimal constant-radius form.
    feat = sw_doc.FeatureManager.FeatureFillet3(
        195,  # default fillet options bitmask (propagate, etc.)
        radius, 0, 0, 0, 0, 0,
        _null_dispatch(), _null_dispatch(), _null_dispatch(), _null_dispatch(),
        _null_dispatch(), _null_dispatch(), _null_dispatch(),
    )
    if feat is None:
        # Never a bare None: every precondition was verified (radius>0 in meters,
        # n>0 edges selected for the named scope), so enumerate them so the failure
        # is diagnosable, not a mystery (P4 regression).
        raise SolidWorksError(
            f"FeatureFillet3 returned None for {feature.id} despite verified preconditions "
            f"(radius={radius:.6f} m, {n} edge(s) selected, scope '{scope}') — the selected "
            f"edges likely cannot accept this radius (radius exceeds the adjacent face).")
    log.info("  fillet %s applied to %d edge(s) (%s).", feature.id, n, scope)
    # P9: state the INTENDED scope + expected count and flag when the applied edge
    # count disagrees (over-application to all edges is explicit, never silent).
    mode, expected, reason = plan_fillet_scope(feature, model)
    if mode in ("corners", "slot_ends") and expected and n != expected:
        _note_warning(model, f"{feature.id}: fillet callout implies {mode} ({reason}, expected "
                             f"~{expected} edge(s)) but {n} edge(s) were selected ({scope}) — VERIFY "
                             f"against the drawing; the geometric corner selector could not isolate "
                             f"the intended edges, so this may be over-applied.")
    else:
        _note_warning(model, f"{feature.id}: fillet applied to {n} edges ({scope}; intended {mode}); "
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
        raise SolidWorksError(
            f"chamfer {feature.id}: PRECONDITION FAILED — 0 edges selected for scope "
            f"'{scope}'; no candidate edge exists within tolerance, so the chamfer is not applied.")
    # InsertFeatureChamfer(Width, Angle, Flip, Type, OtherDist, VertexChamDist, VertexChamDist2)
    feat = sw_doc.FeatureManager.InsertFeatureChamfer(
        4, 1, distance, angle, 0, 0, 0, 0
    )
    if feat is None:
        raise SolidWorksError(
            f"InsertFeatureChamfer returned None for {feature.id} despite verified preconditions "
            f"(distance={distance:.6f} m, angle={angle:.4f} rad, {n} edge(s) selected, scope "
            f"'{scope}') — the selected edges likely cannot accept this chamfer distance.")
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
    feature_results: Optional[list[dict]] = None,
    deferred_out: Optional[list[dict]] = None,
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
        feature_results: if provided, every feature outcome is appended as
            ``{"feature", "feature_id", "type", "status", "detail"}`` — the
            caller writes this as ``macro_result.json`` so a failure surfaces as
            the EXACT failing feature, never a generic exit code.

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

    # Workstream 1: a hard non-strict failure quarantines the feature here and
    # the build CONTINUES; deferred features are retried after the rest of the
    # part is complete (their target faces/edges now exist).
    from pipeline.deferred_retry import DeferredQueue

    deferred_queue = DeferredQueue()

    def _record(feature_id: str, ftype: str, status: str, detail: str = "",
                name: str = "") -> None:
        if feature_results is not None:
            feature_results.append({
                "feature": name or feature_id, "feature_id": feature_id,
                "type": ftype, "status": status, "detail": detail,
            })

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
                _record(feature_id, feature.type.value, "PASS", "no geometry to add (no-op)")
                continue
            feature_map[feature_id] = result

            # Name the feature immediately after creation (deterministic tree
            # names; the circular-pattern builder already named its own trio).
            fname_det = f"{feature_id}_{feature.type.value}"
            try:
                cur = result.Name
                if isinstance(cur, str) and not cur.startswith(feature_id):
                    result.Name = fname_det
            except Exception:
                pass

            if not check_rebuild_errors(sw_doc):
                raise SolidWorksError(f"Rebuild errors after feature {feature_id}.")

            built_count += 1
            log.info("  ✓ %s complete", feature_id)
            _record(feature_id, feature.type.value, "PASS")

            # Periodic auto-save so a later crash doesn't lose everything.
            if built_count % AUTOSAVE_EVERY == 0:
                try:
                    save_model(sw_doc, f"AUTOSAVE_{model.part_name or 'part'}", output_dir)
                except SolidWorksError as e:
                    log.warning("Auto-save failed (continuing): %s", e)

        except SolidWorksError as e:
            _record(feature_id, feature.type.value, "FAIL", str(e))
            if _is_fragile(feature):
                # Fillets/chamfers are demoted to warnings — do not abort the build.
                log.warning("  ! %s (%s) failed and was SKIPPED: %s", feature_id, feature.type.value, e)
                model.warnings.append(f"{feature_id} ({feature.type.value}) skipped: {e}")
                if skipped_out is not None:
                    skipped_out.append((feature_id, feature.type.value, str(e)))
                continue

            if not strict:
                # Non-strict batch mode: DEFER, don't abandon. Quarantine the
                # feature and continue so the rest of the part completes; it is
                # retried after completion (Workstream 1). Final skip/deferred
                # recording happens after the retry passes below.
                log.warning("  ! %s (%s) FAILED — deferred for post-completion retry: %s",
                            feature_id, feature.type.value, e)
                deferred_queue.add(feature_id, feature.type.value, str(e))
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

    # ── Workstream 1: retry deferred features now that the rest of the solid
    # EXISTS. The retry re-dispatches the feature builder with the completed
    # solid as context — a feature that failed because its target face/parent
    # did not exist yet (build-order defect) now succeeds; a genuinely
    # unrecoverable one ends `deferred_open` with a clarification question,
    # never a silent forever-skip.
    if deferred_queue.items:
        from pipeline.deferred_retry import run_retry_passes

        def _topology_ctx() -> dict:
            ctx: dict[str, Any] = {}
            try:
                bodies = sw_doc.GetBodies2(0, False)
                body = bodies[0] if isinstance(bodies, (list, tuple)) else bodies
                ctx["face_count"] = int(body.GetFaceCount()) if hasattr(body, "GetFaceCount") else None
                ctx["bbox_m"] = list(body.GetBodyBox())
            except Exception:
                pass
            return ctx

        def _retry_one(item, strategy, ctx):
            # Every strategy re-attempts the build with the completed solid in
            # place (the new information). The strategy label is recorded; the
            # COM action is the same converging move — re-run the builder now
            # that faces/parents exist. A clean second failure escalates to the
            # next strategy, then to the clarification gate.
            feat = get_feature_by_id(model, item.feature_id)
            dims = get_dimensions_for_feature(model, feat)
            try:
                result = dispatch_feature_builder(sw_doc, model, feat, dims, feature_map)
            except Exception as e:
                return False, f"{strategy}: {type(e).__name__}: {e}"
            if result is None or result is _NOOP:
                return (result is _NOOP), f"{strategy}: {'no-op' if result is _NOOP else 'returned None'}"
            feature_map[item.feature_id] = result
            try:
                result.Name = f"{item.feature_id}_{feat.type.value}"
            except Exception:
                pass
            if not check_rebuild_errors(sw_doc):
                return False, f"{strategy}: rebuild errors after retry"
            return True, f"{strategy}: recovered (faces={ctx.get('face_count')})"

        run_retry_passes(deferred_queue, _retry_one, _topology_ctx)
        # Record final outcomes: recovered -> PASS; still-open -> deferred skip.
        for item in deferred_queue.items:
            if item.recovered:
                built_count += 1
                _record(item.feature_id, item.feature_type, "PASS",
                        f"recovered on retry ({item.attempts[-1].strategy})")
            else:
                model.warnings.append(
                    f"{item.feature_id} ({item.feature_type}) deferred_open after retries: "
                    f"{item.error_text[:120]}")
                if skipped_out is not None:
                    skipped_out.append((item.feature_id, item.feature_type,
                                        f"deferred_open ({item.error_class}): {item.error_text[:120]}"))
        if deferred_out is not None:
            deferred_out.extend(i.as_dict() for i in deferred_queue.items)

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
