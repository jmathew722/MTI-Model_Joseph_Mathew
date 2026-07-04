"""Raster fallback: OpenCV HoughCircles hole-candidate detection.

Used ONLY when no vector source exists (scanned PDF, photo, plain image).
Raster centers are estimates, never exact — every position that comes from
this path is tagged ``position_source="hough"`` with reduced confidence and a
flag, per the precedence rules in pipeline/hole_resolution.py.

Sub-pixel refinement: where a hole has a centerline cross drawn through it,
``cv2.cornerSubPix`` is seeded at the Hough center — the cross intersection is
a saddle point the refiner locks onto. Refinements that wander further than
2 px are rejected (the seed was not actually on a cross).

Positioned-callout association (the eDOCr role): eDOCr's TensorFlow stack
(imgaug, efficientnet==1.0.0) does not install cleanly next to this pipeline,
so its function is replaced by :func:`callout_crops` — targeted crops around
each detected hole that a caller can send to Claude Vision asking only
"what is the callout at this location" (positioned crops, not whole-drawing
interpretation). The pipeline itself stays fully offline here.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .geometry import DocGeometry, OutlineBox, SOURCE_HOUGH, VCircle

log = logging.getLogger(__name__)


def extract_raster_geometry(image_path: str | Path) -> DocGeometry:
    """Detect hole candidates in a raster drawing image (pixel coordinates,
    bottom-left y-up frame)."""
    notes: list[str] = []
    geom = DocGeometry(source_kind="raster", is_raster=True, notes=notes)
    try:
        import cv2
        import numpy as np
    except Exception as e:
        notes.append(f"OpenCV unavailable ({e}); raster hole detection skipped — "
                     "positions fall back to vision (flagged).")
        return geom

    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        notes.append(f"Could not read image: {image_path}")
        return geom
    h, w = img.shape[:2]

    blur = cv2.GaussianBlur(img, (5, 5), 1.2)
    # Drawings are dark lines on light paper; HoughCircles works on the gradient.
    min_r = max(4, int(min(h, w) * 0.004))
    max_r = int(min(h, w) * 0.25)
    circles = cv2.HoughCircles(
        blur, cv2.HOUGH_GRADIENT, dp=1.2, minDist=min_r * 3,
        param1=120, param2=42, minRadius=min_r, maxRadius=max_r,
    )

    def fy(y: float) -> float:  # image y-down -> drawing y-up
        return float(h) - y

    if circles is not None:
        # Sub-pixel refinement on centerline crossings: seed cornerSubPix at each
        # Hough center; keep the refined point only if it stays within 2 px.
        seeds = circles[0][:, :2].astype("float32").reshape(-1, 1, 2)
        refined = seeds.copy()
        try:
            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01)
            refined = cv2.cornerSubPix(255 - blur, seeds.copy(), (5, 5), (-1, -1), criteria)
        except Exception:
            pass
        for (raw, ref) in zip(circles[0], refined.reshape(-1, 2)):
            cx, cy, r = float(raw[0]), float(raw[1]), float(raw[2])
            rx, ry = float(ref[0]), float(ref[1])
            marked = False
            if abs(rx - cx) <= 2.0 and abs(ry - cy) <= 2.0 and (rx != cx or ry != cy):
                cx, cy, marked = rx, ry, True
            geom.circles.append(VCircle(cx, fy(cy), r, SOURCE_HOUGH,
                                        meta="hough", center_marked=marked))
    else:
        notes.append("HoughCircles found no hole candidates in the raster image.")

    # Part-outline candidate: the largest dark contour's bounding box.
    try:
        _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            biggest = max(contours, key=cv2.contourArea)
            x, y, cw, ch = cv2.boundingRect(biggest)
            geom.outlines.append(OutlineBox(float(x), fy(float(y + ch)),
                                            float(x + cw), fy(float(y)), meta="contour-bbox"))
    except Exception as e:
        notes.append(f"Outline contour detection failed: {e}")

    notes.append("RASTER source: hole centers are Hough estimates, not vector-exact — "
                 "all positions from this document must carry a flag.")
    return geom


def callout_crops(image_path: str | Path, geom: DocGeometry,
                  pad_factor: float = 3.0) -> list[dict]:
    """Positioned crop regions around each detected hole, for targeted semantic
    reading ("what is the callout text at this location?") by Claude Vision.

    Returns [{'cx','cy','r','bbox':(x0,y0,x1,y1 image frame)}] — the caller
    crops and sends them; this module makes no API calls.
    """
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            w, h = im.size
    except Exception:
        return []
    out = []
    for c in geom.circles:
        pad = max(24.0, c.r * pad_factor)
        y_img = h - c.cy  # back to image frame
        out.append({
            "cx": c.cx, "cy": c.cy, "r": c.r,
            "bbox": (max(0, int(c.cx - pad)), max(0, int(y_img - pad)),
                     min(w, int(c.cx + pad)), min(h, int(y_img + pad))),
        })
    return out
