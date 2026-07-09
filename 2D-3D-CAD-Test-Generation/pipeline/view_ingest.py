"""Discover per-part, per-view drawing images from a folder.

Input model (separate image per view): each PART is a subfolder of view images,
one image per orthographic view. Views are always processed in this exact order:

    1. front   -> Front Plane
    2. top     -> Top Plane
    3. side     -> Right Plane
    4. second_side -> Right Plane (opposite face; through-cuts are direction-proof)
    5. bottom  -> Top Plane (opposite face)

Expected layout::

    <folder>/
        <PartNumber>/
            01_front.png        (front / elevation)
            02_top.png          (top / plan)
            03_side.png         (right / side)
            04_second_side.png  (left / second side)   [optional]
            05_bottom.png       (bottom)               [optional]
        <PartNumber2>/
            ...

Naming is flexible: the view is classified from keywords in the filename
(front/top/side/left/right/bottom/second/back, or a leading 01..05 that maps to
the canonical order). Only ``front`` is required; the rest are optional. If the
top-level folder contains images directly (no subfolders), the whole folder is
treated as a single part named after the folder.

Public entry points: :func:`discover_parts`, :data:`VIEW_ORDER`, :data:`VIEW_PLANES`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Canonical processing order — NEVER reordered.
VIEW_ORDER: tuple[str, ...] = ("front", "top", "side", "second_side", "bottom")

# Each view's SolidWorks sketch plane. second_side/bottom reuse the orthogonal
# plane of their opposite face — through-cuts are direction-proof, so a cut
# sketched on Right/Top reaches the material from either side.
VIEW_PLANES: dict[str, str] = {
    "front": "Front Plane",
    "top": "Top Plane",
    "side": "Right Plane",
    "second_side": "Right Plane",
    "bottom": "Top Plane",
}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".pdf"}

# The overview / whole-drawing view: not an orthographic sketch plane, but the
# full drawing image, passed to extraction as whole-part CONTEXT (the extractor
# recognizes this exact view type). It is never built as a plane.
OVERVIEW_VIEW = "full"

# The human-annotated composite (drawing + colored reference-region boxes),
# written by the web UI. It is NOT an orthographic view and never a sketch
# plane — it rides alongside the part as ``PartViews.marked_view`` and is fed
# into extraction as ground truth for hole placement.
MARKED_VIEW_FILENAME = "full_marked_view.jpg"

# Keyword rules (most specific first) mapping a filename to a canonical view.
_VIEW_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (OVERVIEW_VIEW, ("full", "overview", "isometric", "pictorial", "wholedrawing", "fulldrawing")),
    ("second_side", ("second_side", "secondside", "second", "2nd", "left", "back", "rear")),
    ("bottom", ("bottom", "btm", "underside", "under")),
    ("top", ("top", "plan")),
    ("side", ("side", "right", "rhs", "profile", "elevation_side")),
    ("front", ("front", "elevation", "fwd", "face")),
)
# Leading numeric prefix -> canonical order fallback (01=front .. 05=bottom).
_NUM_TO_VIEW = {1: "front", 2: "top", 3: "side", 4: "second_side", 5: "bottom"}


@dataclass
class PartViews:
    name: str
    views: dict[str, Path] = field(default_factory=dict)  # view_type -> image path
    warnings: list[str] = field(default_factory=list)
    marked_view: Path | None = None  # full_marked_view.jpg (human region markup)

    @property
    def ordered_views(self) -> list[tuple[str, Path]]:
        """(view_type, path) pairs in canonical VIEW_ORDER, present ones only."""
        return [(v, self.views[v]) for v in VIEW_ORDER if v in self.views]


def classify_view(filename: str) -> str:
    """Map an image filename to a canonical view type ("" if undetermined)."""
    stem = Path(filename).stem.lower()
    for view, needles in _VIEW_RULES:
        if any(n in stem for n in needles):
            return view
    # Fallback: a leading number (01_, 1-, 02 ...) maps to the canonical order.
    m = re.match(r"^0*([1-5])\b", stem)
    if m:
        return _NUM_TO_VIEW[int(m.group(1))]
    return ""


def _collect_part(name: str, image_paths: list[Path]) -> PartViews:
    part = PartViews(name=name)
    for path in sorted(image_paths):
        # The annotated composite is not a view — remember it and move on.
        if path.name == MARKED_VIEW_FILENAME:
            part.marked_view = path
            continue
        view = classify_view(path.name)
        # A file named after the part folder itself (e.g. A001271E.png alongside
        # A001271E_front_view.png) is the full drawing — use it as overview context.
        if not view and path.stem.lower() == name.lower():
            view = OVERVIEW_VIEW
        if not view:
            part.warnings.append(f"Could not classify view for {path.name}; skipped.")
            continue
        if view in part.views:
            part.warnings.append(
                f"Multiple images map to the {view} view ({part.views[view].name}, "
                f"{path.name}); keeping the first."
            )
            continue
        part.views[view] = path
    if "front" not in part.views:
        part.warnings.append("No FRONT view found — the base profile cannot be built without it.")
    return part


def discover_parts(folder: Path | str) -> list[PartViews]:
    """Discover all parts and their view images under ``folder``.

    Each immediate subfolder is one part; if the folder holds images directly,
    it is treated as a single part named after the folder.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a folder: {folder}")

    def images_in(d: Path) -> list[Path]:
        return [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]

    subdirs = sorted(p for p in folder.iterdir() if p.is_dir())
    parts: list[PartViews] = []
    if subdirs:
        for d in subdirs:
            imgs = images_in(d)
            if imgs:
                parts.append(_collect_part(d.name, imgs))
    direct = images_in(folder)
    if direct and not parts:
        parts.append(_collect_part(folder.name, direct))
    return parts
