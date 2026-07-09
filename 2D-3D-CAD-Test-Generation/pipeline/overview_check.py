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

# Fix 4.1 (learning-loop 2026-07-09: 15 recurring "cannot auto-match" noise
# flags). Legitimate drawing content that isn't a machinable feature — classify
# it and give each a reconciliation rule instead of the generic noise flag.
_NONFEATURE_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("stock_thickness_view", ("thick", "thickness", "stock", "gauge", "gage", "material thick")),
    ("surface_finish_note",  ("finish", "coat", "plat", "anodiz", "cfs", "paint", "powder", "zinc")),
    ("hardware_reference_note", ("hardware", "screw", "nut", "rivet", "standoff", "insert",
                                 "pem", "washer", "bolt ", "fastener")),
    ("reference_boundary", ("dashed", "phantom", "hidden line", "reference", "ref only",
                            "envelope", "boundary")),
    ("formed_profile", ("form", "bend", "bent", "flange", "tab", "brake", "developed")),
)
_NUM_RE = __import__("re").compile(r"(\d*\.\d+|\d+)")  # captures ".105", "0.105", "105"


def _classify_nonfeature(text: str) -> str:
    t = (text or "").lower()
    for kind, needles in _NONFEATURE_KINDS:
        if any(k in t for k in needles):
            return kind
    return "unknown"


def _extraction_thickness_in(extraction: dict) -> Optional[float]:
    """Part thickness/extrude depth from the extraction (drawing units), if any."""
    for d in extraction.get("dimensions", []) or []:
        applies = str(d.get("applies_to", "")).lower()
        if any(t in applies for t in ("thick", "depth", "height")) and (d.get("value") or 0) > 0:
            return float(d["value"])
    return None


def _reconcile_nonfeature(fid: str, desc: str, where: str, clearly: bool,
                          extraction: dict) -> Optional[dict[str, Any]]:
    """Reconcile a non-machinable overview item by kind. Returns a review item,
    or None when it reconciles cleanly (no noise flag)."""
    kind = _classify_nonfeature(f"{desc} {where}")
    loc = f" ({where})" if where else ""

    if kind == "stock_thickness_view":
        m = _NUM_RE.search(desc)
        thk_build = _extraction_thickness_in(extraction)
        if m and thk_build:
            shown = float(m.group(1))
            if abs(shown - thk_build) <= max(0.01, 0.03 * thk_build):
                return None  # thickness view agrees with the built extrude depth
            return _item("HIGH", fid,
                         what=f"Thickness mismatch: the overview's thickness view shows {shown} but "
                              f"the build's extrude depth is {thk_build}{loc}.",
                         decision="build kept its extracted thickness",
                         why="stock-thickness view disagrees with the extrude depth",
                         affects="part thickness / extrude depth")
        return _item("LOW", fid,
                     what=f"Stock/thickness view noted: {desc}{loc}.",
                     decision="recorded as the part's thickness reference",
                     why="a thickness edge view is not a machinable feature",
                     affects="metadata: stock thickness")

    if kind in ("surface_finish_note", "hardware_reference_note"):
        label = "surface finish" if kind == "surface_finish_note" else "hardware reference"
        return _item("LOW", fid,
                     what=f"{label.title()} note: {desc}{loc}.",
                     decision=f"attached to part metadata as a {label} note",
                     why="a note, not part geometry — no build counterpart expected",
                     affects=f"metadata: {label}")

    if kind == "reference_boundary":
        return _item("LOW", fid,
                     what=f"Reference/phantom geometry noted: {desc}{loc}.",
                     decision="treated as a reference boundary, not built geometry",
                     why="dashed/phantom/reference linework is not a solid feature",
                     affects="metadata: reference boundary")

    if kind == "formed_profile":
        return _item("MEDIUM" if clearly else "LOW", fid,
                     what=f"Formed/bent profile shown: {desc}{loc}.",
                     decision="not built — the pipeline models machined prismatic parts, "
                              "not sheet-metal forming",
                     why="a bend/flange is an unsupported feature kind (escalate if the part "
                         "is truly sheet metal)",
                     affects="unsupported feature kind: formed profile")

    # Genuinely unknown content keeps the honest generic flag (now rare, so it
    # regains signal value).
    return _item("MEDIUM" if clearly else "LOW", fid,
                 what=f"The overview shows content the checker cannot auto-match: {desc}{loc}",
                 decision="not automatically verified against the build",
                 why="no direct counterpart in the build inventory or the non-feature taxonomy",
                 affects="manual visual comparison recommended")


def cross_check(overview: dict, extraction: dict) -> list[dict[str, Any]]:
    """Diff the overview feature list against the consolidated extraction.
    Returns engineering-review item dicts (source="overview"), worst first.
    Only overview->build gaps are flagged; extra build features are fine."""
    items: list[dict[str, Any]] = []
    inv = _build_inventory(extraction)
    # Count checking is AGGREGATE, not per-callout: the overview lists holes per
    # callout GROUP (e.g. ".406 DIA (2) HL'S" -> 2, ".422 6-HOLES" -> 6) while the
    # build inventory is the TOTAL of every group. Comparing one group's count to
    # the grand total is apples-to-oranges and fired false "2 vs 5" HIGH flags
    # (learning-loop 2026-07-09: A001211E, A001271E, A001621E, A001821M). Instead,
    # sum the overview's per-group counts for a kind and compare that TOTAL to the
    # build total — flag only a genuine shortfall (overview total > build total),
    # which is the only direction that means a feature is missing.
    ov_counts: dict[str, int] = {}
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
        if kind in _COUNT_CHECKED:
            ov_counts[kind] = ov_counts.get(kind, 0) + count

        if kind == "other":
            item = _reconcile_nonfeature(fid, desc, where, clearly, extraction)
            if item is not None:
                items.append(item)
            # a reconciled non-feature that checks out (None) raises no noise flag
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
        # have>0: per-group count is NOT compared to the total here (see the
        # aggregate check below), so a valid multi-group callout raises no flag.

    # Aggregate shortfall check: only when the overview's TOTAL for a kind exceeds
    # the build's total (build is genuinely missing some), and only for kinds that
    # still have at least one built feature (a total absence was already flagged
    # CRITICAL per-callout above).
    for kind, ov_total in sorted(ov_counts.items()):
        have = inv.get(kind, 0)
        if have > 0 and ov_total > have:
            n += 1
            items.append(_item(
                "HIGH", f"OV{n:03d}",
                what=f"Count shortfall for {kind}: the overview's callouts total "
                     f"{ov_total}, but the build contains {have}.",
                decision=f"build kept its extracted total of {have}",
                why="the summed overview callout counts exceed the built total — a "
                    "group of this feature may be missing (per-group counts that sum "
                    "to the build total are treated as consistent and NOT flagged)",
                affects=f"{kind} total count",
            ))
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
