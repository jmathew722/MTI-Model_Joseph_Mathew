"""Overview cross-check — the FINAL verification pass against the overview drawing.

The overview image (the ``full`` view: the whole drawing / isometric shot) is fed
into extraction as context, but until now nothing ever came back to it. This
module closes the loop: after resolution and the build, the overview is
re-examined ALONE with a focused Claude Vision pass that lists every discrete
feature it shows, and that list is diffed against what the pipeline actually
captured and built.

Direction of the check (by design):
  * A feature clearly visible in the overview but MISSING from the build is
    CRITICAL — the model is missing geometry the drawing shows.
  * A count mismatch (overview shows 6 holes, build has 4) is HIGH.
  * A feature the overview only *possibly* shows (small/ambiguous) is MEDIUM.
  * Features in the build but not visible in the overview are FINE and never
    flagged — an overview cannot show every hidden feature.

The check is exception-safe and never blocks a build by crashing: no overview
image, no API key, or an API failure produce a note and an empty finding list.
A CRITICAL finding gates READY (see ``batch.process_drawing_data``) unless the
run passes ``--skip-overview-check``.

Public entry points: :func:`run_overview_check`, :func:`cross_check`,
:func:`extract_overview_features`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

OVERVIEW_TOOL_NAME = "report_overview_features"
# Mixed into the cache key — bump when the prompt below changes meaningfully.
OVERVIEW_PROMPT_VERSION = "1"
MAX_TOKENS = 4000

# Feature kinds the overview pass may report. Kept deliberately coarse — this is
# a cross-check, not a second extraction.
_KINDS = ("hole", "counterbore", "countersink", "thread", "slot", "cutout",
          "fillet", "chamfer", "rib", "boss", "pattern", "shell", "other")

_SYSTEM = """\
You are a senior inspection engineer looking at the OVERVIEW sheet of a 2D
engineering drawing. Your only job is to list every DISCRETE FEATURE the
drawing visibly shows, with honest counts, so a reviewer can confirm nothing
was missed by an earlier extraction. Do NOT extract dimensions in detail —
report features, counts, and the overall envelope only. Report a feature as
clearly_visible=false when it is small, partially hidden, or you are unsure it
exists. Call the required tool exactly once."""

_USER_TEXT = """\
List every discrete feature visible in this drawing (holes, counterbores,
countersinks, threads, slots, cutouts, fillets, chamfers, ribs, bosses,
patterns, shells). Give the count of each repeated feature (e.g. a 4-hole bolt
pattern -> kind "hole", count 4). Also report the overall part envelope
(width/height and depth if readable) with its units. Be honest: mark anything
uncertain with clearly_visible=false rather than omitting or inventing it."""


def _overview_tool() -> dict[str, Any]:
    return {
        "name": OVERVIEW_TOOL_NAME,
        "description": "Report every discrete feature visible in the overview drawing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "features": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": list(_KINDS)},
                            "count": {"type": "integer", "minimum": 1},
                            "description": {"type": "string"},
                            "location": {"type": "string"},
                            "clearly_visible": {"type": "boolean"},
                        },
                        "required": ["kind", "count", "description"],
                    },
                },
                "envelope": {
                    "type": "object",
                    "properties": {
                        "width": {"type": "number"},
                        "height": {"type": "number"},
                        "depth": {"type": "number"},
                        "units": {"type": "string"},
                    },
                },
                "notes": {"type": "string"},
            },
            "required": ["features"],
        },
    }


def _cache_key(image_b64: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(b"overview\0")
    h.update(OVERVIEW_PROMPT_VERSION.encode("utf-8"))
    h.update(b"\0")
    h.update(model.encode("utf-8"))
    h.update(b"\0")
    h.update(image_b64.encode("utf-8"))
    return h.hexdigest()


def extract_overview_features(
    image_b64: str,
    media_type: str = "image/png",
    model: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    usage_out: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    """One focused vision call on the overview image; returns the tool input dict
    ``{"features": [...], "envelope": {...}, "notes": ...}``. Cached like the main
    extraction so re-runs are free."""
    from pipeline.extractor import (
        DEFAULT_MODEL,
        _accumulate_usage,
        _build_client,
        _cache_lookup,
        _cache_store,
        _image_block,
        SDK_MAX_RETRIES,
    )

    model = model or os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL
    key = _cache_key(image_b64, model)
    cached = _cache_lookup(cache_dir, key)
    if cached is not None:
        log.info("Overview-check cache hit (%s...)", key[:12])
        return cached

    client = _build_client(max_retries=SDK_MAX_RETRIES)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                _image_block(image_b64, media_type),
                {"type": "text", "text": _USER_TEXT},
            ],
        }],
        tools=[_overview_tool()],
        tool_choice={"type": "tool", "name": OVERVIEW_TOOL_NAME},
    )
    if usage_out is not None:
        _accumulate_usage(usage_out, response)
    tool_use = next(
        (b for b in response.content
         if getattr(b, "type", None) == "tool_use" and b.name == OVERVIEW_TOOL_NAME),
        None,
    )
    if tool_use is None:
        raise RuntimeError(
            f"Overview pass did not call {OVERVIEW_TOOL_NAME!r} "
            f"(stop_reason={getattr(response, 'stop_reason', None)})"
        )
    data = dict(tool_use.input or {})
    data.setdefault("features", [])
    _cache_store(cache_dir, key, data)
    return data


# --------------------------------------------------------------------------- #
# Diff logic (pure, unit-testable — no API)
# --------------------------------------------------------------------------- #

def _build_inventory(extraction: dict) -> dict[str, int]:
    """Count what the pipeline captured, per overview kind. Conservative and
    generous on the build side: a kind is matched by feature types AND hole
    callout types AND description keywords, so an overview finding is only
    raised when the build genuinely has nothing that could account for it."""
    inv = {k: 0 for k in _KINDS}
    callouts = extraction.get("hole_callouts") or []
    for h in callouts:
        qty = max(int(h.get("qty") or 1), 1)
        htype = str(h.get("type") or "").lower()
        inv["hole"] += qty
        if "counterbore" in htype or "spotface" in htype:
            inv["counterbore"] += qty
        if "countersink" in htype:
            inv["countersink"] += qty
        if "tap" in htype or h.get("thread_spec"):
            inv["thread"] += qty
        if str(h.get("pattern") or "").strip():
            inv["pattern"] += 1

    text_kinds = {
        "slot": ("slot", "keyway", "groove"),
        "cutout": ("cutout", "cut-out", "window", "notch", "opening"),
        "rib": ("rib", "web", "gusset"),
        "boss": ("boss", "standoff", "pad"),
    }
    for f in extraction.get("features") or []:
        ftype = str(f.get("type") or "").lower()
        desc = f"{f.get('description') or ''} {f.get('notes') or ''}".lower()
        if ftype == "hole":
            # counted via callouts when present; count the feature only if no callouts
            if not callouts:
                inv["hole"] += 1
        elif ftype in ("fillet", "chamfer", "pattern", "shell", "thread"):
            inv[ftype] += 1
        elif ftype == "extrude_boss":
            inv["boss"] += 1
        elif ftype == "extrude_cut":
            inv["cutout"] += 1
        for kind, needles in text_kinds.items():
            if any(n in desc for n in needles):
                inv[kind] += 1
    return inv


def _envelope_items(envelope: dict, extraction: dict) -> list[dict[str, Any]]:
    """Check the overview's overall envelope numbers against the extracted
    dimensions (with inch<->mm conversion). An axis value with no matching
    dimension anywhere is HIGH — the build may be the wrong overall size."""
    items: list[dict[str, Any]] = []
    if not envelope:
        return items
    dims = extraction.get("dimensions") or []
    values: list[float] = []
    for d in dims:
        for k in ("resolved_value", "value"):
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 0:
                values.append(float(v))
    if not values:
        return items

    def _matched(v: float) -> bool:
        for cand in (v, v * 25.4, v / 25.4):
            for known in values:
                if known > 0 and abs(cand - known) / max(known, 1e-9) <= 0.02:
                    return True
        return False

    for axis in ("width", "height", "depth"):
        v = envelope.get(axis)
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        if not _matched(float(v)):
            units = envelope.get("units") or "drawing units"
            items.append(_item(
                "HIGH", f"OV-ENV-{axis.upper()}",
                what=f"The overview shows an overall {axis} of {v:g} {units}, but no "
                     f"extracted dimension matches it (±2%, inch/mm checked).",
                decision="build proceeded with the extracted dimensions",
                why="overall envelope read from the overview drawing disagrees with "
                    "or is missing from the extraction",
                affects="overall part envelope",
            ))
    return items


def _item(severity: str, item_id: str, what: str, decision: str, why: str,
          affects: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "source": "overview",
        "id": item_id,
        "what": what,
        "decision": decision,
        "why": why,
        "affects": affects,
    }


# Kinds where the overview's count is reliable enough to flag a mismatch.
_COUNT_CHECKED = {"hole", "counterbore", "countersink", "thread"}


def cross_check(overview: dict, extraction: dict) -> list[dict[str, Any]]:
    """Diff the overview feature list against the consolidated extraction.
    Returns engineering-review item dicts (source="overview"), worst first.
    Only overview->build gaps are flagged; extra build features are fine."""
    items: list[dict[str, Any]] = []
    inv = _build_inventory(extraction)
    n = 0
    for f in overview.get("features") or []:
        kind = str(f.get("kind") or "other").lower()
        if kind not in _KINDS:
            kind = "other"
        count = max(int(f.get("count") or 1), 1)
        clearly = bool(f.get("clearly_visible", True))
        desc = str(f.get("description") or kind)
        where = str(f.get("location") or "").strip()
        n += 1
        fid = f"OV{n:03d}"
        have = inv.get(kind, 0)

        if kind == "other":
            items.append(_item(
                "MEDIUM" if clearly else "LOW", fid,
                what=f"The overview shows a feature the checker cannot auto-match: "
                     f"{desc}" + (f" ({where})" if where else ""),
                decision="not automatically verified against the build",
                why="feature kind has no direct counterpart in the build inventory",
                affects="manual visual comparison recommended",
            ))
        elif have == 0:
            items.append(_item(
                "CRITICAL" if clearly else "MEDIUM", fid,
                what=(f"The overview clearly shows {count}x {kind} ({desc}"
                      + (f", {where}" if where else "") + ") but the build contains "
                      "no matching feature.") if clearly else
                     (f"The overview POSSIBLY shows {count}x {kind} ({desc}"
                      + (f", {where}" if where else "") + ") with no matching "
                      "feature in the build."),
                decision="feature is absent from the final part",
                why="present in the overview drawing, missing from the extraction/build",
                affects=f"{kind} feature(s) — verify against the drawing",
            ))
        elif kind in _COUNT_CHECKED and have != count:
            items.append(_item(
                "HIGH", fid,
                what=f"Count mismatch for {kind}: the overview shows {count}, the "
                     f"build contains {have} ({desc}"
                     + (f", {where}" if where else "") + ").",
                decision=f"build kept its extracted count of {have}",
                why="overview count disagrees with the consolidated extraction",
                affects=f"{kind} count",
            ))
        # matched (have>0, count agrees or kind not count-checked) -> no item
    items.extend(_envelope_items(overview.get("envelope") or {}, extraction))
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    items.sort(key=lambda it: order.get(it["severity"], 4))
    return items


def run_overview_check(
    overview_image: Path,
    extraction: dict,
    cache_dir: Optional[Path] = None,
    usage_out: Optional[dict[str, int]] = None,
    page: int = 1,
) -> tuple[list[dict[str, Any]], str]:
    """The exception-safe wrapper the pipeline calls.

    Returns ``(items, note)``. ``items`` is empty and ``note`` explains why when
    the check could not run (no image / no key / API failure) — the pipeline
    proceeds either way; only a successful check with CRITICAL findings gates.
    """
    try:
        if overview_image is None or not Path(overview_image).is_file():
            return [], "skipped: no overview image for this part"
        from utils.image_prep import prepare_image

        prepared = prepare_image(str(overview_image), page=page, return_details=True)
        overview = extract_overview_features(
            prepared.base64, media_type=prepared.media_type,
            cache_dir=cache_dir, usage_out=usage_out,
        )
        items = cross_check(overview, extraction)
        n_feat = len(overview.get("features") or [])
        return items, (f"checked {n_feat} overview feature(s): "
                       f"{len(items)} finding(s)")
    except EnvironmentError as e:
        return [], f"skipped: {e}"
    except Exception as e:  # never sink a build over the cross-check
        log.warning("Overview check failed (non-fatal): %s", e)
        return [], f"skipped: {type(e).__name__}: {e}"
