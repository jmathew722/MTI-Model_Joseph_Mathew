"""Image preparation for Claude Vision.

Accepts a drawing as an image or PDF and returns a base64-encoded PNG sized and
cleaned for the vision model. Handles the messy realities of real drawings:
multi-page PDFs, dark-background (inverted) scans, low-contrast photocopies, and
rotated pages.

Public entry point: :func:`prepare_image`.
"""
from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageOps

from utils.logger import get_logger

log = get_logger()

# High-resolution vision supports up to 2576px on the long edge; fine dimension
# text on drawings benefits from extra resolution, but image tokens scale with
# pixel count. Override with MAX_IMAGE_LONG_EDGE to A/B lower resolutions against
# extraction accuracy + token usage (e.g. 1568, the effective-resolution ceiling).
try:
    MAX_LONG_EDGE = int(os.getenv("MAX_IMAGE_LONG_EDGE", "2576"))
except ValueError:
    MAX_LONG_EDGE = 2576
MIN_DIMENSION = 100  # reject anything smaller than 100x100 px
PDF_DPI = 300

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
PDF_SUFFIXES = {".pdf"}


@dataclass
class PreparedImage:
    """Result of preparing one page/image."""

    base64: str
    media_type: str  # always "image/png" here
    width: int
    height: int
    page: int  # 1-based page index (1 for single images)
    warnings: list[str]


class ImagePrepError(Exception):
    """Raised when an input cannot be turned into a usable image."""


# --------------------------------------------------------------------------- #
# PDF rasterization
# --------------------------------------------------------------------------- #
def _pdf_to_image(path: Path, page: int) -> Image.Image:
    """Render one PDF page (1-based) to a PIL image at PDF_DPI.

    Tries pdf2image (poppler) first, falls back to PyMuPDF (fitz). Raises
    ImagePrepError if both are unavailable or the page can't be rendered.
    """
    errors: list[str] = []

    # --- Attempt 1: pdf2image (requires poppler) ---
    try:
        from pdf2image import convert_from_path  # type: ignore

        pages = convert_from_path(
            str(path), dpi=PDF_DPI, first_page=page, last_page=page
        )
        if pages:
            log.info("Rendered PDF page %d via pdf2image", page)
            return pages[0]
        errors.append("pdf2image returned no pages")
    except Exception as e:  # poppler missing, page out of range, etc.
        errors.append(f"pdf2image failed: {e}")

    # --- Attempt 2: PyMuPDF / fitz ---
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(path))
        try:
            if page < 1 or page > doc.page_count:
                raise ImagePrepError(
                    f"PDF page {page} out of range (document has {doc.page_count} pages)"
                )
            zoom = PDF_DPI / 72.0  # PDFs are 72 DPI natively
            matrix = fitz.Matrix(zoom, zoom)
            pix = doc.load_page(page - 1).get_pixmap(matrix=matrix)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            log.info("Rendered PDF page %d via PyMuPDF (fallback)", page)
            return img
        finally:
            doc.close()
    except ImagePrepError:
        raise
    except Exception as e:
        errors.append(f"PyMuPDF failed: {e}")

    raise ImagePrepError(
        "Could not convert PDF to image. Install poppler (for pdf2image) or "
        "PyMuPDF. Details: " + " | ".join(errors)
    )


# --------------------------------------------------------------------------- #
# Image cleanup steps
# --------------------------------------------------------------------------- #
def _to_rgb(img: Image.Image) -> Image.Image:
    """Normalize any mode (RGBA, grayscale, palette) to RGB on a white background."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        # Flatten transparency onto white so it reads like paper.
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return img.convert("RGB")


def _maybe_invert_dark_background(img: Image.Image, warnings: list[str]) -> Image.Image:
    """If the page is mostly dark (white-on-black drawing), invert it.

    Engineering drawings are line-work on paper: a low mean brightness means a
    dark background, which the model reads better inverted.
    """
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    if gray.mean() < 110:  # mostly dark
        warnings.append("Detected dark background — inverted to dark-on-light.")
        log.info("Inverting dark-background image (mean=%.1f)", gray.mean())
        return ImageOps.invert(img)
    return img


def _maybe_enhance_contrast(img: Image.Image, warnings: list[str]) -> Image.Image:
    """Autocontrast low-contrast scans/photocopies.

    Uses the luminance histogram spread as the trigger; only stretches contrast
    when the dynamic range is narrow, to avoid harming already-clean images.
    """
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    spread = gray.max() - gray.min()
    if spread < 200:  # full range is 0..255; a narrow spread = washed out
        warnings.append("Low contrast detected — applied autocontrast.")
        log.info("Applying autocontrast (luminance spread=%.0f)", spread)
        return ImageOps.autocontrast(img, cutoff=1)
    return img


def _warn_if_blank(img: Image.Image, warnings: list[str]) -> None:
    """Warn (do not fail) if the page looks blank — almost no line work."""
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    if gray.mean() > 240:
        msg = (
            f"Image appears nearly blank (mean pixel value {gray.mean():.1f} > 240) "
            "— it may contain no visible line work."
        )
        warnings.append(msg)
        log.warning(msg)


def _resize_long_edge(img: Image.Image) -> Image.Image:
    """Downscale so the longest edge is <= MAX_LONG_EDGE, preserving aspect ratio.

    Never upscales — enlarging a small image adds no detail and wastes tokens.
    """
    w, h = img.size
    longest = max(w, h)
    if longest <= MAX_LONG_EDGE:
        return img
    scale = MAX_LONG_EDGE / longest
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    log.info("Resizing %sx%s -> %sx%s", w, h, *new_size)
    return img.resize(new_size, Image.LANCZOS)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def prepare_image(
    drawing_path: str | Path,
    page: int = 1,
    return_details: bool = False,
) -> str | PreparedImage:
    """Prepare a drawing for the Claude Vision API.

    Args:
        drawing_path: Path to a .jpg/.jpeg/.png/.tif/.tiff/.bmp or .pdf file.
        page: 1-based page to use for PDFs (default first page).
        return_details: if True, return a :class:`PreparedImage` with metadata
            and warnings; if False (default), return just the base64 string.

    Returns:
        The base64-encoded PNG string, or a :class:`PreparedImage`.

    Raises:
        ImagePrepError: for missing files, unsupported types, undersized images,
            or unconvertible PDFs.
    """
    path = Path(drawing_path)
    if not path.exists():
        raise ImagePrepError(f"Drawing file not found: {path}")
    if not path.is_file():
        raise ImagePrepError(f"Not a file: {path}")

    suffix = path.suffix.lower()
    warnings: list[str] = []

    # --- Load to a PIL image ---
    if suffix in PDF_SUFFIXES:
        if page < 1:
            raise ImagePrepError(f"page must be >= 1, got {page}")
        img = _pdf_to_image(path, page)
    elif suffix in IMAGE_SUFFIXES:
        try:
            img = Image.open(path)
            img.load()
        except Exception as e:
            raise ImagePrepError(f"Could not open image {path}: {e}") from e
        # Honor EXIF orientation so rotated photos of drawings come out upright.
        img = ImageOps.exif_transpose(img)
        page = 1
    else:
        raise ImagePrepError(
            f"Unsupported file type {suffix!r}. Supported: "
            f"{sorted(IMAGE_SUFFIXES | PDF_SUFFIXES)}"
        )

    # --- Validate minimum size before doing any work ---
    w, h = img.size
    if w < MIN_DIMENSION or h < MIN_DIMENSION:
        raise ImagePrepError(
            f"Image too small ({w}x{h}px); minimum is {MIN_DIMENSION}x{MIN_DIMENSION}px."
        )

    # --- Clean up ---
    img = _to_rgb(img)
    img = _maybe_invert_dark_background(img, warnings)
    img = _maybe_enhance_contrast(img, warnings)
    img = _resize_long_edge(img)
    _warn_if_blank(img, warnings)

    # --- Encode ---
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    b64 = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")

    if return_details:
        return PreparedImage(
            base64=b64,
            media_type="image/png",
            width=img.size[0],
            height=img.size[1],
            page=page,
            warnings=warnings,
        )
    return b64
