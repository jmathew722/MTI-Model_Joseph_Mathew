"""Stage 1.5 — Holistic Overview Analysis (cross-view relational extraction).

Runs BETWEEN rasterization and the per-view cropped extraction: the FULL,
uncropped drawing sheet (the same image Tab 1 renders as "FULL OVERVIEW VIEW")
is sent to Claude Sonnet 5 with a prompt whose job is explicitly RELATIONAL,
not per-feature extraction:

  * how many distinct views are on the sheet and what each one is;
  * which features correspond to each other across views (e.g. a DIA circle in
    the front view matching full-height hidden lines in the side view confirms
    a THROUGH-bore, not a blind hole);
  * the overall 3D shape implied by combining all views;
  * features visible in one view but absent/contradicted in another (flagged
    explicitly with a severity and a recommendation);
  * part symmetry (rotational / mirror) that should constrain patterning;
  * global notes ("FINISH ALL OVER", "(6) HLS") and what they actually govern.

The per-view extraction stage remains the authority on individual dimensions
(priority tier 1); this stage is authoritative on cross-view RELATIONSHIPS
(tier 2) — the things a single cropped view cannot determine alone. Operator
must-meet specifications stay tier 0. Stage 2.5 records which tier resolved
each ambiguity flag (``resolved_by_tier``).

The result is saved as ``overview_analysis.json`` in the part's run folder and
fed into :func:`pipeline.resolver.resolve_extraction`. Token usage is logged
as its own cost line tagged :data:`STAGE_TAG` — a distinct cost center from
the per-view extraction calls.

This stage can only ADD signal, never break the run: any failure (no API key,
API error, unvalidatable output) returns ``None`` and the pipeline proceeds
exactly as before.

Public entry points: :func:`analyze_overview`, :func:`analyze_overview_file`,
:func:`save_overview_analysis`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, ValidationError

from utils.logger import get_logger

log = get_logger()

# Ledger stage tag: Stage 1.5 is its own cost center, separate from the
# per-view extraction calls (token-log separation).
STAGE_TAG = "stage_1_5_overview_analysis"

TOOL_NAME = "report_overview_analysis"
MAX_TOKENS = 8000

# Mixed into the cache key — bump on any meaningful prompt change.
PROMPT_VERSION = "1"

OVERVIEW_ANALYSIS_FILENAME = "overview_analysis.json"


# --------------------------------------------------------------------------- #
# Output schema (overview_analysis.json)
# --------------------------------------------------------------------------- #
class ViewDetected(BaseModel):
    view_id: str = Field(description="Short id: front, side, top, section_a, detail_b, title_block, ...")
    description: str = Field(default="", description="What this view shows, one sentence.")


class CrossViewCorrespondence(BaseModel):
    feature: str = Field(description="Short feature name, e.g. center_bore, bolt_pattern, bottom_tab.")
    seen_in: list[str] = Field(default_factory=list, description="view_ids where this feature is visible.")
    relation: str = Field(
        default="",
        description="How the appearances relate and what that CONFIRMS (e.g. through-hole vs blind).",
    )
    confidence: str = Field(default="medium", description="high | medium | low")


class GlobalNote(BaseModel):
    note: str = Field(description="The note/callout text as written, e.g. 'FINISH ALL OVER', '(6) HLS'.")
    applies_to: str = Field(default="", description="Which views/features this note actually governs.")
    resolved_count: Optional[int] = Field(
        default=None,
        description="If the note states a COUNT of features (e.g. '(6) HLS' -> 6), that count.",
    )


class CrossViewConflict(BaseModel):
    description: str = Field(description="What disagrees between views (or between a view and a callout).")
    views_involved: list[str] = Field(default_factory=list)
    severity: str = Field(default="MEDIUM", description="CRITICAL | HIGH | MEDIUM | LOW")
    recommendation: str = Field(
        default="",
        description="Concrete next step for resolving it (where to look, what to verify).",
    )


class SymmetryInfo(BaseModel):
    type: str = Field(
        default="none_detected",
        description="rotational | mirror | both | none_detected",
    )
    notes: str = Field(default="", description="Evidence and caveats (e.g. dimensioning style).")


class OverviewAnalysis(BaseModel):
    part_number: str = Field(default="", description="Part number from the title block, if readable.")
    views_detected: list[ViewDetected] = Field(default_factory=list)
    cross_view_correspondences: list[CrossViewCorrespondence] = Field(default_factory=list)
    overall_shape_summary: str = Field(
        default="",
        description="The overall 3D shape implied by combining ALL views, one or two sentences.",
    )
    global_notes: list[GlobalNote] = Field(default_factory=list)
    cross_view_conflicts: list[CrossViewConflict] = Field(default_factory=list)
    symmetry: SymmetryInfo = Field(default_factory=SymmetryInfo)


SYSTEM_PROMPT = """\
You are a senior design engineer looking at ONE complete engineering drawing \
sheet — every view at once. Your job is RELATIONAL analysis of the whole sheet, \
NOT per-dimension extraction (a separate stage extracts every dimension from \
cropped views). Reason about the drawing as a single coherent 3D object and \
report by calling the required tool.

Answer these questions:
1. VIEWS: how many distinct views are on the sheet, and what is each one
   (front, side/profile, top, section, detail, title block)? List them all.
2. CORRESPONDENCES: for each pair of views that share geometry, which features
   correspond across views, and what does the correspondence CONFIRM? e.g. "the
   3.880 DIA bore in the front view corresponds to full-height vertical hidden
   lines in the side view — confirms a THROUGH-bore, not a blind hole". This is
   the most valuable output: through vs blind, tab profiles, feature depths.
3. OVERALL SHAPE: the 3D shape implied by combining all views, in one or two
   sentences (e.g. "flat circular flange, .50 thick, with a through bore, a
   6-hole bolt pattern, and a bottom tab with an additional through-hole").
4. CONFLICTS: features visible in one view that are absent, contradicted, or
   ambiguous in another — including a callout COUNT that does not match what is
   visibly drawn (e.g. "(6) HLS" but only 5 hole circles are clearly rendered:
   check for an occluded hole behind the title block or a leader line pointing
   to a location not clearly drawn). Flag each explicitly with a severity
   (CRITICAL when building from one reading alone would produce a wrong part)
   and a concrete recommendation.
5. SYMMETRY: rotational or mirror symmetry that should constrain how features
   are patterned — but verify from the DIMENSIONING style (X/Y offset
   dimensioning is evidence AGAINST assuming a polar pattern) and say so.
6. GLOBAL NOTES: leader lines, notes, or symbols that apply across the part
   ("FINISH ALL OVER", "(6) HLS", "BREAK ALL SHARP EDGES") rather than to one
   view — state which views/features each note actually governs, and when a
   note states a count, report it as resolved_count.

Be honest: use the conflicts list for anything you cannot reconcile. Never
invent features to make views agree.\
"""

USER_TEXT = (
    "This is the FULL drawing sheet (all views, uncropped). Perform the holistic "
    "cross-view analysis and report it by calling the tool."
)


def _cache_key(image_b64: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(b"overview-analysis\0")
    h.update(PROMPT_VERSION.encode("utf-8"))
    h.update(b"\0")
    h.update(model.encode("utf-8"))
    h.update(b"\0")
    h.update(image_b64.encode("utf-8"))
    return h.hexdigest()


def _tool() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Report the holistic cross-view analysis of this drawing sheet.",
        "input_schema": OverviewAnalysis.model_json_schema(),
    }


def _find_tool_use(response) -> Any:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block
    return None


def analyze_overview(
    image_b64: str,
    media_type: str = "image/png",
    part_number: str = "",
    model: Optional[str] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    usage_out: Optional[dict[str, int]] = None,
) -> Optional[dict[str, Any]]:
    """Run the Stage 1.5 holistic analysis on the full-sheet overview image.

    Returns the :class:`OverviewAnalysis` dict, or ``None`` when the stage is
    unavailable or fails (no API key, API error, unvalidatable output) — the
    pipeline proceeds without it. Reuses the extraction on-disk cache dir (with
    a distinct key namespace) so identical re-runs are free.
    """
    from pipeline.extractor import (  # shared client/image/usage plumbing
        DEFAULT_MODEL,
        SDK_MAX_RETRIES,
        _accumulate_usage,
        _build_client,
        _cache_lookup,
        _cache_store,
        _image_block,
    )

    model = model or os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL

    key = _cache_key(image_b64, model)
    cached = _cache_lookup(cache_dir, key)
    if cached is not None:
        log.info("Overview analysis cache HIT (%s…) — skipping API call.", key[:12])
        if usage_out is not None:
            usage_out["cache_hits"] = usage_out.get("cache_hits", 0) + 1
        return cached

    if not os.getenv("ANTHROPIC_API_KEY"):
        log.info("Overview analysis skipped: no ANTHROPIC_API_KEY.")
        return None

    client = _build_client(SDK_MAX_RETRIES)
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": [
            _image_block(image_b64, media_type, cache=True),
            {"type": "text", "text": USER_TEXT},
        ],
    }]

    def _call(msgs):
        return client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=msgs,
            tools=[_tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )

    usage: dict[str, int] = {}
    try:
        response = _call(messages)
        _accumulate_usage(usage, response)
        tool_use = _find_tool_use(response)
        if tool_use is None:
            raise ValueError(f"model did not call {TOOL_NAME!r}")
        try:
            data = OverviewAnalysis.model_validate(tool_use.input)
        except ValidationError as e:
            # One repair retry: every tool_use in the turn needs a tool_result.
            log.warning("Overview analysis failed validation; requesting a repair: %s", e)
            results = [{
                "type": "tool_result", "tool_use_id": tu.id,
                "content": (f"Your input did not match the required schema:\n{e}\n"
                            f"Call {TOOL_NAME} again with corrected input.")
                           if tu.id == tool_use.id else "Duplicate call ignored.",
                "is_error": True,
            } for tu in response.content if getattr(tu, "type", None) == "tool_use"]
            response2 = _call(messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": results},
            ])
            _accumulate_usage(usage, response2)
            tool_use2 = _find_tool_use(response2)
            if tool_use2 is None:
                raise ValueError(f"model did not call {TOOL_NAME!r} on repair")
            data = OverviewAnalysis.model_validate(tool_use2.input)
    except Exception as e:
        log.warning("Overview analysis failed (stage skipped, pipeline proceeds): %s: %s",
                    type(e).__name__, e)
        if usage_out is not None:
            for k, v in usage.items():
                usage_out[k] = usage_out.get(k, 0) + v
        return None

    if part_number and not any(ch.isalnum() for ch in data.part_number):
        data.part_number = part_number
    result = data.model_dump(mode="json")
    log.info(
        "Overview analysis: %d view(s), %d correspondence(s), %d conflict(s), symmetry=%s",
        len(result.get("views_detected", [])),
        len(result.get("cross_view_correspondences", [])),
        len(result.get("cross_view_conflicts", [])),
        (result.get("symmetry") or {}).get("type", "?"),
    )
    if usage_out is not None:
        for k, v in usage.items():
            usage_out[k] = usage_out.get(k, 0) + v
    _cache_store(cache_dir, key, result)
    return result


def analyze_overview_file(
    image_path: Union[str, Path],
    page: int = 1,
    part_number: str = "",
    cache_dir: Optional[Union[str, Path]] = None,
    usage_out: Optional[dict[str, int]] = None,
) -> Optional[dict[str, Any]]:
    """:func:`analyze_overview` on an on-disk overview image/PDF (prepared with
    the same rasterization settings as extraction — the on-disk crop-flow image
    is reused as-is; nothing is re-rasterized from the source document)."""
    from utils.image_prep import prepare_image

    try:
        prepared = prepare_image(str(image_path), page=page, return_details=True)
    except Exception as e:
        log.warning("Overview analysis: could not prepare %s (%s); stage skipped.",
                    image_path, e)
        return None
    return analyze_overview(
        prepared.base64, media_type=prepared.media_type, part_number=part_number,
        cache_dir=cache_dir, usage_out=usage_out,
    )


def save_overview_analysis(part_dir: Union[str, Path], analysis: dict) -> Path:
    """Persist ``overview_analysis.json`` into the part's run folder."""
    part_dir = Path(part_dir)
    part_dir.mkdir(parents=True, exist_ok=True)
    path = part_dir / OVERVIEW_ANALYSIS_FILENAME
    path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    return path
