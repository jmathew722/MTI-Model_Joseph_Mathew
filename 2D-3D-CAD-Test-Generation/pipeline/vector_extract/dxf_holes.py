"""Exact hole geometry from DXF (and DWG via the ODA File Converter bridge).

DXF model space is real CAD geometry: CIRCLE/ARC entities carry exact centers
and radii in drawing units, so positions extracted here are authoritative —
vision never overrides them (see pipeline/hole_resolution.py precedence).

DWG inputs are converted DWG→DXF with ezdxf's ``odafc`` add-on, which shells
out to the free ODA File Converter. When ODA is not installed the caller gets
a DocGeometry with ``is_raster=True`` and an explicit note — a clear, flagged
fallback to the raster/vision path, never a silent degrade.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from .geometry import (
    SOURCE_DXF,
    DocGeometry,
    OutlineBox,
    VCircle,
    VDim,
    VText,
    group_full_circles,
)

log = logging.getLogger(__name__)

# DXF $INSUNITS header code → millimeters per drawing unit.
# 0 = unitless (unknown). Only the codes seen in mechanical drawings are mapped.
_INSUNITS_TO_MM: dict[int, float | None] = {
    0: None,
    1: 25.4,      # inches
    2: 304.8,     # feet
    4: 1.0,       # millimeters
    5: 10.0,      # centimeters
    6: 1000.0,    # meters
    8: 0.0254,    # microinches
    9: 0.0254 * 1000 / 1000,  # mils (0.0254 mm)
    10: 914.4,    # yards
}
_INSUNITS_TO_MM[9] = 0.0254  # mils, spelled out


def _dwg_to_dxf(path: Path, notes: list[str]) -> Path | None:
    """Convert a DWG to a temp DXF via the no-ODA engine chain
    (ezdwg → SolidWorks translator → ODA if installed), or None + notes."""
    import tempfile

    from .dwg_convert import dwg_to_dxf

    out_dir = Path(tempfile.mkdtemp(prefix="mti_dwg2dxf_"))
    out_path = out_dir / (path.stem + ".dxf")
    engine = dwg_to_dxf(path, out_path, notes)
    if engine is not None:
        notes.append(f"DWG converted to DXF via {engine}.")
        return out_path
    notes.append("DWG could not be converted by any available engine "
                 "(ezdwg / SolidWorks / ODA) — see the notes above.")
    return None


def extract_dxf_geometry(path: str | Path) -> DocGeometry:
    """Extract circles, dimensions, texts, and outline candidates from a DXF/DWG.

    Never raises for content problems — a DocGeometry with ``is_raster=True``
    and an explanatory note is returned instead, so the pipeline can fall back
    (flagged) to the raster/vision path.
    """
    path = Path(path)
    notes: list[str] = []
    geom = DocGeometry(source_kind="dxf", notes=notes)

    if path.suffix.lower() == ".dwg":
        dxf_path = _dwg_to_dxf(path, notes)
        if dxf_path is None:
            geom.is_raster = True
            notes.append("FALLBACK: DWG vector extraction unavailable — positions will "
                         "come from the raster/vision path and must be flagged.")
            return geom
        path = dxf_path

    try:
        import ezdxf

        doc = ezdxf.readfile(str(path))
    except Exception as e:
        geom.is_raster = True
        notes.append(f"DXF could not be read ({type(e).__name__}: {e}); falling back to raster/vision.")
        return geom

    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    geom.native_units_to_mm = _INSUNITS_TO_MM.get(insunits)
    if geom.native_units_to_mm is None:
        notes.append(f"DXF $INSUNITS={insunits} does not declare a usable unit; "
                     "scale will be anchored from dimension callouts instead.")

    msp = doc.modelspace()
    arcs: list[tuple[float, float, float, float, float]] = []

    # --- direct entities ---------------------------------------------------
    for e in msp.query("CIRCLE"):
        c = e.dxf.center
        geom.circles.append(VCircle(float(c.x), float(c.y), float(e.dxf.radius),
                                    SOURCE_DXF, meta=f"CIRCLE#{e.dxf.handle}"))
    for e in msp.query("ARC"):
        c = e.dxf.center
        arcs.append((float(c.x), float(c.y), float(e.dxf.radius),
                     float(e.dxf.start_angle), float(e.dxf.end_angle)))

    # --- block references (hole patterns are often INSERTs) ----------------
    for ins in msp.query("INSERT"):
        try:
            for ve in ins.virtual_entities():  # transformed copies
                t = ve.dxftype()
                if t == "CIRCLE":
                    c = ve.dxf.center
                    geom.circles.append(VCircle(float(c.x), float(c.y), float(ve.dxf.radius),
                                                SOURCE_DXF, meta=f"INSERT:{ins.dxf.name}"))
                elif t == "ARC":
                    c = ve.dxf.center
                    arcs.append((float(c.x), float(c.y), float(ve.dxf.radius),
                                 float(ve.dxf.start_angle), float(ve.dxf.end_angle)))
        except Exception as e:
            notes.append(f"INSERT '{ins.dxf.name}' could not be resolved: {e}")

    for cx, cy, r in group_full_circles(arcs):
        geom.circles.append(VCircle(cx, cy, r, SOURCE_DXF, meta="ARC-group"))

    # --- dimensions ---------------------------------------------------------
    _DIMTYPE_KIND = {0: "linear", 1: "linear", 2: "angular", 3: "diameter",
                     4: "radial", 5: "angular", 6: "ordinate"}
    for d in msp.query("DIMENSION"):
        try:
            meas = d.get_measurement()
            if not isinstance(meas, (int, float)):
                continue  # angular dims return vectors/angles we don't use
            base = _DIMTYPE_KIND.get(int(d.dimtype) & 7, "linear")
            p = d.dxf.get("text_midpoint", None) or d.dxf.get("defpoint", None)
            x, y = (float(p.x), float(p.y)) if p is not None else (0.0, 0.0)
            geom.dims.append(VDim(float(meas), x, y, kind=base))
        except Exception:
            continue

    # --- positioned texts (callouts live here in DXF) -----------------------
    for t in msp.query("TEXT"):
        try:
            p = t.dxf.insert
            geom.texts.append(VText(t.dxf.text, float(p.x), float(p.y)))
        except Exception:
            continue
    for t in msp.query("MTEXT"):
        try:
            p = t.dxf.insert
            geom.texts.append(VText(t.plain_text(), float(p.x), float(p.y)))
        except Exception:
            continue

    # --- outline candidates: closed polylines / rectangles ------------------
    for pl in msp.query("LWPOLYLINE"):
        try:
            if not pl.closed:
                continue
            pts = [(float(x), float(y)) for x, y, *_ in pl.get_points()]
            if len(pts) < 3:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            geom.outlines.append(OutlineBox(min(xs), min(ys), max(xs), max(ys),
                                            meta=f"LWPOLYLINE#{pl.dxf.handle}"))
        except Exception:
            continue
    # Loose fallback: bbox over all lines+circles (used only if nothing better).
    xs: list[float] = []
    ys: list[float] = []
    for ln in msp.query("LINE"):
        s, e = ln.dxf.start, ln.dxf.end
        xs += [float(s.x), float(e.x)]
        ys += [float(s.y), float(e.y)]
    for c in geom.circles:
        xs += [c.cx - c.r, c.cx + c.r]
        ys += [c.cy - c.r, c.cy + c.r]
    if xs and ys:
        geom.outlines.append(OutlineBox(min(xs), min(ys), max(xs), max(ys), meta="loose-bbox"))

    if not geom.circles:
        notes.append("No CIRCLE/ARC hole candidates found in DXF model space.")
    return geom
