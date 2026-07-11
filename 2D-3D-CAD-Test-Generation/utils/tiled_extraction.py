"""Workstream 2 — tiled high-resolution extraction ("zoom pass").

Full-sheet rasterization at a fixed long-edge cap dilutes thin line work into
near-invisibility on large-format (C/D/E-size) drawings — a 0.3 mm line on a
D-size sheet shrunk to ~2576 px is sub-pixel, so extraction warns "image appears
nearly blank." This module is the ESCALATION: detect the condition, re-render
the (vector) PDF adaptively at higher DPI, extract in overlapping zoomed tiles
anchored to a cheap global map, then stitch the tile results back into one
coherent extraction. Clean small drawings keep the fast single-shot path.

Design (research-grounded): naive window slicing destroys spatial context at
tile boundaries, so tiles carry 20-25% OVERLAP plus a global-pass map (view
boundaries, title block, datum candidates, feature-cluster regions) — a
dimension whose arrow crosses a boundary is not lost or double-read. Tiles emit
SHEET coordinates (tile offset applied), not tile-local ones. Only tiles the
global map says contain content are sent (cost control). Conflicting readings
across tiles are kept as candidate values for the Stage 2.5 resolver (which
already handles them). Tile-pass cost is logged separately as
``extraction_tiled``.

The VLM calls are injected (``extract_fn`` / ``global_fn``) so the machinery is
unit-tested without any paid API call; production wires the real vision call.

Public: :func:`should_tile`, :func:`adaptive_render`, :func:`median_line_width_px`,
:func:`ink_density`, :func:`make_tiles`, :func:`stitch`, :func:`datum_anchor`,
:func:`tiled_extract`.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ── Trigger thresholds ────────────────────────────────────────────────────────
BLANK_MEAN = 240.0          # gray mean above this = "nearly blank" (matches image_prep)
MIN_INK_DENSITY = 0.005     # < 0.5% dark pixels after threshold = too sparse
MIN_CONFIDENCE = 0.6
MAX_UNCLEAR_FRACTION = 0.25
# A C-size sheet is 17x22 in; area >= this (sq in) at the standard raster cap
# pushes line width below ~2 px -> tile.
LARGE_SHEET_SQIN = 17.0 * 22.0
TARGET_LINE_PX = 2.5
ADAPTIVE_DPIS = (300, 600, 900)
TILE_PX = 1500
TILE_OVERLAP = 0.22         # 22% overlap between neighbours
EXTRACTION_TILED_STAGE = "extraction_tiled"


# --------------------------------------------------------------------------- #
# Image measurements
# --------------------------------------------------------------------------- #
def _to_gray_array(image):
    import numpy as np
    from PIL import Image

    if not isinstance(image, Image.Image):
        image = Image.open(image)
    return np.asarray(image.convert("L"), dtype=np.float32)


def ink_density(image) -> float:
    """Fraction of dark (ink) pixels after an adaptive (Otsu-ish) threshold."""
    import numpy as np

    g = _to_gray_array(image)
    if g.size == 0:
        return 0.0
    # Simple robust threshold: midpoint between the 5th and 95th percentiles,
    # biased toward ink (dark). Pixels darker than it are "ink".
    lo, hi = np.percentile(g, 5), np.percentile(g, 95)
    thresh = lo + 0.5 * (hi - lo)
    return float((g < thresh).mean())


def median_line_width_px(image) -> float:
    """Estimate median stroke width (px) of the dark line work via a distance
    transform: 2x the median distance-to-background over ink pixels. Larger DPI
    -> wider strokes -> higher value; used to pick an adaptive DPI."""
    import numpy as np
    from scipy import ndimage

    g = _to_gray_array(image)
    if g.size == 0:
        return 0.0
    lo, hi = np.percentile(g, 5), np.percentile(g, 95)
    ink = g < (lo + 0.5 * (hi - lo))
    if not ink.any():
        return 0.0
    dt = ndimage.distance_transform_edt(ink)
    vals = dt[ink]
    # The stroke half-width is the ridge (max) of the distance transform along
    # a stroke; the median over all ink pixels underestimates, so use a high
    # percentile as the half-width estimate.
    half = float(np.percentile(vals, 80))
    return 2.0 * half


# --------------------------------------------------------------------------- #
# Trigger
# --------------------------------------------------------------------------- #
@dataclass
class TileTrigger:
    fire: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.fire


def should_tile(*, image=None, extraction: Optional[dict] = None,
                page_area_sqin: Optional[float] = None,
                raster_long_edge: int = 2576) -> TileTrigger:
    """Decide whether the tiled zoom path should fire. ANY trigger fires it."""
    reasons: list[str] = []
    if image is not None:
        try:
            g = _to_gray_array(image)
            if g.size and float(g.mean()) > BLANK_MEAN:
                reasons.append(f"nearly blank (mean {g.mean():.1f} > {BLANK_MEAN})")
            dens = ink_density(image)
            if dens < MIN_INK_DENSITY:
                reasons.append(f"ink density {dens*100:.2f}% < {MIN_INK_DENSITY*100:.1f}%")
        except Exception as e:
            log.warning("should_tile image check failed: %s", e)
    if extraction is not None:
        conf = extraction.get("confidence")
        if isinstance(conf, (int, float)) and conf < MIN_CONFIDENCE:
            reasons.append(f"extraction confidence {conf:.2f} < {MIN_CONFIDENCE}")
        dims = extraction.get("dimensions") or []
        if dims:
            unclear = sum(1 for d in dims if d.get("value_unclear"))
            frac = unclear / len(dims)
            if frac > MAX_UNCLEAR_FRACTION:
                reasons.append(f"{frac*100:.0f}% of dimensions flagged unclear")
    if page_area_sqin and page_area_sqin >= LARGE_SHEET_SQIN and raster_long_edge <= 2576:
        reasons.append(f"large sheet ({page_area_sqin:.0f} sq in) at raster cap {raster_long_edge}px")
    return TileTrigger(bool(reasons), reasons)


# --------------------------------------------------------------------------- #
# Adaptive re-rendering (vector PDF -> lossless zoom)
# --------------------------------------------------------------------------- #
def render_pdf_page(pdf_path, page: int, dpi: int):
    """Render one PDF page to a PIL image at the given DPI (via PyMuPDF)."""
    import fitz
    from PIL import Image

    doc = fitz.open(str(pdf_path))
    try:
        pg = doc[page - 1]
        pix = pg.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def page_area_sqin(pdf_path, page: int) -> float:
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        r = doc[page - 1].rect  # points (1/72 in)
        return (r.width / 72.0) * (r.height / 72.0)
    finally:
        doc.close()


def adaptive_render(pdf_path, page: int = 1, target_px: float = TARGET_LINE_PX,
                    dpis: tuple[int, ...] = ADAPTIVE_DPIS):
    """Render the (vector) PDF page at escalating DPI until median line width
    >= target_px. Returns ``(image, dpi, measured_line_px)``. Re-rendering a
    vector source is lossless zoom — never upscales a raster."""
    best = None
    for dpi in dpis:
        img = render_pdf_page(pdf_path, page, dpi)
        lw = median_line_width_px(img)
        best = (img, dpi, lw)
        log.info("adaptive_render: dpi=%d median_line=%.2f px", dpi, lw)
        if lw >= target_px:
            break
    return best


# --------------------------------------------------------------------------- #
# Tiling (overlapping windows in sheet coordinates)
# --------------------------------------------------------------------------- #
@dataclass
class Tile:
    row: int
    col: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def offset(self) -> tuple[int, int]:
        return (self.x0, self.y0)

    def as_dict(self) -> dict[str, Any]:
        return {"row": self.row, "col": self.col, "x0": self.x0, "y0": self.y0,
                "x1": self.x1, "y1": self.y1}


def make_tiles(width: int, height: int, tile: int = TILE_PX,
               overlap: float = TILE_OVERLAP) -> list[Tile]:
    """Overlapping tile grid over a (width, height) image. Step = tile * (1 -
    overlap); the last tile in each axis is clamped to the edge so full coverage
    is guaranteed. Coordinates are SHEET (image) pixels."""
    step = max(1, int(round(tile * (1.0 - overlap))))
    xs = _starts(width, tile, step)
    ys = _starts(height, tile, step)
    tiles = []
    for r, y0 in enumerate(ys):
        for c, x0 in enumerate(xs):
            tiles.append(Tile(r, c, x0, y0, min(x0 + tile, width), min(y0 + tile, height)))
    return tiles


def _starts(length: int, tile: int, step: int) -> list[int]:
    if length <= tile:
        return [0]
    starts = list(range(0, max(1, length - tile) + 1, step))
    if starts[-1] != length - tile:
        starts.append(length - tile)  # clamp last tile to the edge
    return starts


def crop_tile(image, t: Tile):
    return image.crop((t.x0, t.y0, t.x1, t.y1))


def tile_has_content(image, t: Tile, min_density: float = MIN_INK_DENSITY) -> bool:
    """Whether a tile is worth sending — skip blank margins for cost control."""
    return ink_density(crop_tile(image, t)) >= min_density


# --------------------------------------------------------------------------- #
# Stitch + dedupe (merge tile extractions into one)
# --------------------------------------------------------------------------- #
def _anchor(dim: dict) -> Optional[tuple[float, float]]:
    a = dim.get("anchor") or dim.get("anchor_xy")
    if isinstance(a, (list, tuple)) and len(a) == 2:
        return (float(a[0]), float(a[1]))
    if "anchor_x" in dim and "anchor_y" in dim:
        return (float(dim["anchor_x"]), float(dim["anchor_y"]))
    return None


def stitch(tile_dims: list[dict], tol_px: float = 25.0) -> list[dict]:
    """Merge dimensions read across tiles (each already in SHEET coordinates).

    Same dimension = anchors agree within ``tol_px`` AND values match -> one
    entry (records contributing tiles). Anchors agree but values differ -> keep
    BOTH as candidate readings (``value_unclear=True`` + ``possible_values``) for
    the Stage 2.5 resolver's conflicting-readings path. Dims without an anchor
    are kept as-is (deduped by value+applies_to)."""
    merged: list[dict] = []
    anchored = [d for d in tile_dims if _anchor(d) is not None]
    unanchored = [d for d in tile_dims if _anchor(d) is None]

    used = [False] * len(anchored)
    for i, d in enumerate(anchored):
        if used[i]:
            continue
        ax, ay = _anchor(d)
        group = [d]
        used[i] = True
        for j in range(i + 1, len(anchored)):
            if used[j]:
                continue
            bx, by = _anchor(anchored[j])
            if math.hypot(ax - bx, ay - by) <= tol_px:
                group.append(anchored[j])
                used[j] = True
        merged.append(_merge_group(group))

    # Unanchored: dedupe by (applies_to, rounded value).
    seen = set()
    for d in unanchored:
        key = ((d.get("applies_to") or "").lower(), round(float(d.get("value", 0)), 4))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(d))
    return merged


def _merge_group(group: list[dict]) -> dict:
    base = dict(group[0])
    tiles = sorted({t for d in group for t in _tiles_of(d)})
    base["source_tiles"] = tiles
    values = {round(float(d.get("value", 0)), 4) for d in group if "value" in d}
    if len(values) > 1:
        # Conflicting readings across tiles — hand BOTH to the resolver.
        base["value_unclear"] = True
        base["possible_values"] = sorted(values)
        base["ambiguity_reason"] = "tile readings disagree at the same anchor"
    return base


def _tiles_of(d: dict) -> list:
    t = d.get("source_tiles") or ([d["tile"]] if "tile" in d else [])
    return t if isinstance(t, list) else [t]


# --------------------------------------------------------------------------- #
# Datum anchoring
# --------------------------------------------------------------------------- #
def datum_anchor(dims: list[dict], origin_xy: tuple[float, float],
                 px_per_unit: float = 1.0) -> list[dict]:
    """Re-express each dimension's anchor relative to the datum origin (sheet px
    -> drawing units from the datum), so positions enter the schema datum-first
    (feeds Workstream 3). Non-destructive: adds ``anchor_from_datum`` and leaves
    the raw anchor for the audit trail."""
    ox, oy = origin_xy
    out = []
    for d in dims:
        a = _anchor(d)
        e = dict(d)
        if a is not None and px_per_unit:
            e["anchor_from_datum"] = [round((a[0] - ox) / px_per_unit, 4),
                                      round((oy - a[1]) / px_per_unit, 4)]  # +Y up
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Cache + orchestration
# --------------------------------------------------------------------------- #
def cache_key(pdf_path, page: int, dpi: int, grid: tuple[int, int]) -> str:
    h = hashlib.sha256()
    try:
        h.update(Path(pdf_path).read_bytes())
    except OSError:
        h.update(str(pdf_path).encode())
    h.update(f"|{page}|{dpi}|{grid[0]}x{grid[1]}".encode())
    return h.hexdigest()[:16]


def tiled_extract(pdf_path, page: int, *,
                  extract_fn: Callable[[Any, dict], dict],
                  global_fn: Optional[Callable[[Any], dict]] = None,
                  cache_dir: Optional[Path] = None,
                  usage_out: Optional[dict] = None,
                  target_px: float = TARGET_LINE_PX) -> dict:
    """Orchestrate the zoom pass. ``extract_fn(tile_image, ctx) -> {dimensions,
    features,...}`` and ``global_fn(full_image) -> {views, datums, ...}`` are the
    (injected) VLM calls — production wires the real vision call; tests inject a
    fake so this runs with NO paid call. Returns the stitched extraction with
    per-dimension tile provenance and datum-anchored positions."""
    import json as _json

    img, dpi, lw = adaptive_render(pdf_path, page, target_px=target_px)
    tiles = make_tiles(img.width, img.height)
    grid = (max(t.row for t in tiles) + 1, max(t.col for t in tiles) + 1)

    if cache_dir is not None:
        ck = cache_key(pdf_path, page, dpi, grid)
        cpath = Path(cache_dir) / f"tiled_{ck}.json"
        if cpath.is_file():
            try:
                log.info("tiled_extract: cache hit %s", cpath.name)
                return _json.loads(cpath.read_text(encoding="utf-8"))
            except Exception:
                pass

    global_map = global_fn(img) if global_fn else {}
    origin = tuple(global_map.get("datum_origin_px", (0.0, img.height)))

    all_dims: list[dict] = []
    tiles_sent = 0
    for t in tiles:
        if not tile_has_content(img, t):
            continue  # skip blank margins (cost control)
        tiles_sent += 1
        ctx = {"tile": t.as_dict(), "offset": t.offset, "global_map": global_map,
               "dpi": dpi}
        res = extract_fn(crop_tile(img, t), ctx) or {}
        for d in res.get("dimensions", []) or []:
            d = dict(d)
            # Apply the tile offset so anchors are in SHEET coordinates.
            a = _anchor(d)
            if a is not None:
                d["anchor"] = [a[0] + t.x0, a[1] + t.y0]
            d.setdefault("source_tiles", []).append(f"r{t.row}c{t.col}")
            all_dims.append(d)

    merged = stitch(all_dims)
    merged = datum_anchor(merged, origin)
    result = {"dimensions": merged, "views": global_map.get("views", []),
              "tiled": {"dpi": dpi, "median_line_px": round(lw, 2),
                        "grid": {"rows": grid[0], "cols": grid[1]},
                        "tiles_total": len(tiles), "tiles_sent": tiles_sent}}
    if usage_out is not None:
        usage_out.setdefault("stage", EXTRACTION_TILED_STAGE)
        usage_out["tiles_sent"] = tiles_sent
    if cache_dir is not None:
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            (Path(cache_dir) / f"tiled_{cache_key(pdf_path, page, dpi, grid)}.json"
             ).write_text(_json.dumps(result, indent=2), encoding="utf-8")
        except OSError:
            pass
    return result
