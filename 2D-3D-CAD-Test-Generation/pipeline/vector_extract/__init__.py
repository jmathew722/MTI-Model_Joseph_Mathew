"""Multi-source exact hole extraction.

Entry point: :func:`augment_hole_positions` — dispatches the drawing source to
the right extractor (DXF/DWG → ezdxf, PDF → PyMuPDF vectors, image → OpenCV
Hough), then runs the consensus layer (:mod:`pipeline.hole_resolution`) which
writes exact positions back into the extracted model's hole callouts.

Design contract: this stage can only IMPROVE the model. Any failure —
missing dependency, unreadable file, unanchorable scale — leaves the model
exactly as vision produced it, plus an explanatory flag. It never raises into
the pipeline and never blocks a build.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..schema import DrawingData

log = logging.getLogger(__name__)

_VECTOR_SUFFIXES = {".pdf", ".dxf", ".dwg"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def augment_hole_positions(model: DrawingData, source_path: str | Path,
                           page: int = 1):
    """Vector-augment ``model``'s hole positions from the original drawing file.

    Returns a :class:`pipeline.hole_resolution.HoleResolutionReport`, or None
    when the source type is unsupported. Mutates ``model`` in place (positions,
    position_source/confidence, warnings). Exception-safe by design.
    """
    from ..hole_resolution import resolve_holes

    path = Path(source_path)
    suffix = path.suffix.lower()
    try:
        if suffix in (".dxf", ".dwg"):
            from .dxf_holes import extract_dxf_geometry
            geom = extract_dxf_geometry(path)
        elif suffix == ".pdf":
            from .pdf_holes import extract_pdf_geometry
            geom = extract_pdf_geometry(path, page_number=page)
            if geom.is_raster:
                # Scanned PDF: rasterize the page and run the Hough fallback.
                raster = _rasterize_pdf_page(path, page)
                if raster is not None:
                    from .raster_holes import extract_raster_geometry
                    rgeom = extract_raster_geometry(raster)
                    rgeom.notes = geom.notes + rgeom.notes
                    geom = rgeom
        elif suffix in _IMAGE_SUFFIXES:
            from .raster_holes import extract_raster_geometry
            geom = extract_raster_geometry(path)
        else:
            return None
        if not model.hole_callouts:
            log.info("Vector augment: model has no hole callouts; nothing to resolve.")
            return None
        report = resolve_holes(model, geom)
        n_exact = sum(1 for h in report.holes if h.outcome == "vector_exact")
        log.info("Vector augment (%s): %d/%d hole callout(s) placed exactly "
                 "(scale=%.6g from %d anchor(s)).",
                 geom.source_kind, n_exact, len(report.holes),
                 report.scale, report.scale_anchors)
        return report
    except Exception as e:  # never break the pipeline from this stage
        log.warning("Vector hole augmentation failed (%s): %s — continuing with "
                    "vision-derived positions.", type(e).__name__, e)
        model.warnings.append(
            f"hole-position: vector extraction crashed ({type(e).__name__}: {e}); "
            "positions remain vision-derived — verify hole placement.")
        return None


def _rasterize_pdf_page(path: Path, page: int) -> Path | None:
    """Rasterize one PDF page to a temp PNG for the Hough fallback."""
    try:
        import tempfile

        import fitz

        doc = fitz.open(str(path))
        try:
            pg = doc[max(0, page - 1)]
            pix = pg.get_pixmap(dpi=300)
            out = Path(tempfile.mkdtemp(prefix="mti_pdfraster_")) / "page.png"
            pix.save(str(out))
            return out
        finally:
            doc.close()
    except Exception as e:
        log.warning("Could not rasterize scanned PDF for Hough fallback: %s", e)
        return None
