"""Dimension extraction via the Claude Vision API.

Uses ``claude-sonnet-5`` (override with ``EXTRACTION_MODEL``) with a **forced
tool call** — Claude must respond by calling the ``report_drawing_data`` tool,
whose ``input_schema`` is generated from :class:`pipeline.schema.DrawingData`.
The tool's JSON input is then validated against the same Pydantic model.

Note: this intentionally avoids Anthropic's *strict* structured outputs
(``output_format=``), whose server-side grammar compiler cannot handle
``DrawingData``'s nested arrays of objects ("Schema is too complex" /
"Grammar compilation timed out"). Non-strict tool use has no such compiler
step, at the cost of needing a Pydantic-validation repair retry instead of a
server-side guarantee.

Public entry point: :func:`extract_drawing_data`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional, Union

from dotenv import load_dotenv
from pydantic import ValidationError

from pipeline.schema import DrawingData
from utils.logger import get_logger

log = get_logger()
load_dotenv()

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
CONFIDENCE_THRESHOLD = 0.7  # below this, re-query once for a closer look
SDK_MAX_RETRIES = 3  # SDK-level retries for transient API errors
# Extra retry rounds in _call AFTER the SDK's own budget is exhausted (covers
# longer outages so a paid multi-view extraction isn't lost to a blip).
EXTRA_RETRY_ROUNDS = 2
EXTRA_RETRY_BACKOFF_S = 15.0

TOOL_NAME = "report_drawing_data"

# Bump this whenever the prompt (system or user text) changes meaningfully. It is
# mixed into the extraction cache key so an improved prompt forces a fresh
# extraction instead of silently returning a result captured under the old prompt.
# v3: emphatic fillet/chamfer capture + extrude-depth & per-instance-hole rules.
# v4: revolve half-profiles, bolt-circle patterns, and mirror features.
# v5: operator must-meet specifications injected into the extraction prompt
#     (specs-first enforcement; the spec text is also mixed into the cache key).
PROMPT_VERSION = "5"

# Token usage fields summed across the (possibly several) calls of one extraction.
_USAGE_FIELDS = (
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
)


def _confidence_threshold() -> float:
    raw = os.getenv("EXTRACTION_CONFIDENCE_THRESHOLD")
    if raw:
        try:
            return float(raw)
        except ValueError:
            log.warning("Ignoring non-numeric EXTRACTION_CONFIDENCE_THRESHOLD=%r", raw)
    return CONFIDENCE_THRESHOLD


def _accumulate_usage(acc: dict[str, int], response: Any) -> None:
    """Add one response's token usage into ``acc`` (no-op if absent, e.g. mocks)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    for field in _USAGE_FIELDS:
        acc[field] = acc.get(field, 0) + (getattr(usage, field, 0) or 0)
    acc["calls"] = acc.get("calls", 0) + 1


# --- Extraction cache: skip the API entirely for an already-seen image ------- #
def _cache_key(image_b64: str, model: str,
               requirements: Optional[list[str]] = None) -> str:
    h = hashlib.sha256()
    h.update(PROMPT_VERSION.encode("utf-8"))
    h.update(b"\0")
    h.update(model.encode("utf-8"))
    h.update(b"\0")
    h.update(image_b64.encode("utf-8"))
    # Must-meet specs shape the extraction prompt, so changed notes must
    # invalidate the cache (never serve a spec-blind cached extraction).
    for req in requirements or []:
        h.update(b"\0req\0")
        h.update(req.encode("utf-8"))
    return h.hexdigest()


def _requirements_block(requirements: Optional[list[str]]) -> Optional[str]:
    """The specs-first prompt block: operator must-meet specifications are given
    to the model BEFORE it extracts, so it actively looks for those features from
    the start (they are re-verified against the final build afterwards)."""
    reqs = [r.strip() for r in (requirements or []) if r and r.strip()]
    if not reqs:
        return None
    lines = "\n".join(f"- {r}" for r in reqs)
    return (
        "OPERATOR MUST-MEET SPECIFICATIONS (human-authored, highest priority):\n"
        f"{lines}\n"
        "Actively look for and extract EVERY feature, dimension, and callout these "
        "specifications reference — do not miss them. If a specification names a "
        "value that clarifies an ambiguous or illegible callout, list that value in "
        "possible_values for the dimension (the drawing remains the source of "
        "truth: never fabricate a number that appears in neither the drawing nor "
        "these specifications, and report disagreements honestly in warnings)."
    )


def _cache_lookup(cache_dir: Optional[Union[str, Path]], key: str) -> Optional[dict]:
    if not cache_dir:
        return None
    path = Path(cache_dir) / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cache_store(cache_dir: Optional[Union[str, Path]], key: str, data: dict) -> None:
    if not cache_dir:
        return
    try:
        d = Path(cache_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write extraction cache: %s", e)

SYSTEM_PROMPT = """\
You are a senior design engineer interpreting a 2D engineering drawing with exact \
accuracy. Fully extract, interpret, and cross-check the drawing, then report it by \
calling the required tool. A bad extraction becomes a wrong 3D model — be thorough \
and honest about uncertainty.

TITLE BLOCK & STANDARD:
- Read the title block: part_name, part_number, revision, scale, material, finish,
  and the general tolerance block text (general_tolerance).
- Identify drawing_standard (ASME/ISO/DIN) and units (inch vs mm; fractional inches
  are still "inch").

VIEWS:
- Identify and classify EVERY view (Front/Top/Right/Section/Detail/Auxiliary/...).
- For each view list which dimension ids are readable in it (dimensions_shown),
  which features are visible vs hidden (dashed), and what the center lines imply
  (holes, symmetry, revolved geometry) in centerline_notes.

DIMENSIONS:
- Extract EVERY dimension — miss nothing. Stable ids D001, D002, ...
- All numeric values are numbers, never strings (25.4, not "25.4mm"); unit goes in `unit`.
- Record tolerances (explicit, or note when only the general block applies),
  GD&T symbols and datum references where present.
- Mark REF / parenthesized dimensions with is_reference=true — they are non-controlling.
- If a value is illegible or ambiguous: set value to your best guess, value_unclear=true,
  fill ambiguity_reason and possible_values (best guess first), and set
  resolution_required=true if a human MUST resolve it before building. NEVER silently skip.
- Set feature_ref to the feature (F###) each dimension controls when determinable.

HOLE CALLOUTS:
- Extract every hole callout into hole_callouts (H001, ...): type
  (thru/blind/counterbore/countersink/spotface/tapped), diameter, depth or thru,
  thread_spec (e.g. "1/4-20 UNC"), cbore/csink data, qty, pattern and spacing.
- If the drawing dimensions the hole position from the part origin/center, set
  x_position/y_position and position_known=true. Otherwise leave position_known=false.
- For multi-instance callouts (qty>1), fill instance_positions with the [x, y] center
  of EVERY instance in drawing units, edge-referenced from the part's lower-left
  corner (e.g. a 2x2 bolt pattern -> four [x,y] pairs). This is the most reliable way
  to place patterns; len(instance_positions) should equal qty. Leave empty only when
  the individual positions genuinely cannot be read.
- CIRCULAR / BOLT-CIRCLE patterns (holes equally spaced on a circle): set
  pattern="circular", bolt_circle_diameter to the B.C. diameter, bolt_circle_center
  to the [x,y] circle center (edge-referenced; leave empty to use the part center),
  start_angle to the first hole's angle in degrees (CCW from +X, 0 if unknown), and
  qty to the hole count. Prefer this over guessing instance_positions for a B.C.

FEATURES & BUILD ORDER (F001, F002, ...):
- Use ONLY these feature `type` values: extrude_boss, extrude_cut, revolve, hole,
  fillet, chamfer, thread, pattern, mirror, shell.
- build_order: base solid first (largest extrude_boss or revolve), then primary
  cuts/through holes, then counterbores/countersinks/taps, fillets and chamfers
  LAST, patterns after their seed. The FIRST feature MUST be extrude_boss or revolve.
- related_dimensions / depth_dimension_id must reference existing dimension ids.
- EVERY extrude_boss and extrude_cut MUST have a depth: set depth_dimension_id to
  the dimension giving its thickness/height/depth (read the side/section view for
  plate thickness). A blind cut needs its depth; a cut that goes fully through the
  material is through-all (leave depth unset). Never emit an extrude with no depth.
- Set parent_feature for dependent features (pattern seed, fillet's host feature).
- If a feature's sketch center is dimensioned from the part origin/center, set
  offset_x/offset_y and position_known=true.

FILLETS & CHAMFERS — CAPTURE EVERY ONE (these are routinely missed):
- Scan the WHOLE drawing — every view, every section/detail, AND the notes block —
  for fillet and chamfer callouts. Common forms in these drawings:
    * a radius leader on a corner: ".531 R", "R.531", ".531 R. TYP", "R3 TYP";
    * an inside corner of a section/profile (e.g. where a web meets a flange, or a
      pocket/slot bottom) with a small radius leader — this is a fillet;
    * a chamfer/bevel callout: "0.06 x 45°", "CHAMFER .03", or a clipped corner;
    * a GENERAL NOTE governing many edges: "ALL FILLETS R___", "FILLET ALL INTERNAL
      CORNERS R___", "BREAK ALL SHARP EDGES/CORNERS", "ALL CORNERS R___ UNLESS NOTED".
- For EACH fillet found, emit a `fillet` FEATURE (not just a dimension) and add the
  radius to `dimensions` with applies_to "fillet_radius", linked via the feature's
  related_dimensions. For EACH chamfer, emit a `chamfer` feature with its distance
  (applies_to "chamfer") and angle. A general note that covers many edges is ONE
  feature; put the scope (e.g. "all internal corners") in its description.
- "TYP"/"TYPICAL" means the same radius repeats — still ONE fillet feature; note TYP.
- Range or dual callouts like ".06/.09 R" (min/max acceptable radius): use the value
  closest to the typical/nominal (here .06), set value_unclear=true with
  ambiguity_reason and possible_values listing both — but STILL create the feature.
- If you are unsure whether a corner is filleted, prefer creating the fillet feature
  and flagging it over omitting it. Never silently drop a radius/fillet callout.

REVOLVES (turned / cylindrical / symmetric-about-an-axis parts):
- If the part is turned on a lathe or is a solid of revolution (a shaft, hub, pin,
  flange, bushing — its section view is symmetric about a centerline), make the base
  feature a `revolve` and fill revolve_profile with the HALF-profile as ordered
  [axial, radial] points in drawing units: 'axial' along the centerline, 'radial' the
  distance from it (>=0). Trace the OUTER boundary in order; it is closed back to the
  axis automatically. e.g. a stepped shaft ø10 x 10 then ø16 x 15 ->
  [[0,5],[10,5],[10,8],[25,8]]. Read each diameter as radius = diameter/2.
- Only use revolve when the part truly revolves; a prism stays an extrude_boss.

MIRRORS (symmetric features):
- When a feature (or group) is the MIRROR IMAGE of another about a plane, you may add
  a `mirror` feature: set parent_feature to the feature being mirrored and
  mirror_plane to the plane (front/top/right). Optional — only when it is clearer
  than extracting both halves directly.

RELATIONSHIPS (fill the relationships object — this enables arithmetic verification):
- symmetry: planes of symmetry and which features mirror about them.
- concentric_groups: coaxial holes/bosses.
- equal_spacing: equally spaced patterns with computed spacing (state the arithmetic
  in computed_from, e.g. "overall 80 / 4 gaps = 20").
- dimension_chains: EVERY closed dimension loop you can identify, e.g. overall
  length = left offset + feature + right offset. These are arithmetically checked.
- derived_dimension_ids: dimensions you computed (implied by symmetry/spacing) rather
  than read. Also add each derived dimension to `dimensions` with a note.
- reference_dimension_ids: ids of REF dimensions.

GENERAL:
- confidence: 1.0 = everything clear; below 0.7 triggers a closer second look. Be honest.
- If you genuinely cannot determine the units, use millimeters and add a warning.
- Use empty string "" (not null) for any text field you cannot determine; use 0.0 for
  unknown numeric fields that have a 0.0 default.
- Put any remaining doubts in `warnings`.
"""

INITIAL_USER_TEXT = (
    "Extract all dimensions, tolerances, views, geometric tolerances, and build "
    "features from this engineering drawing, then call the tool with the result."
)


class ExtractionError(Exception):
    """Raised when extraction cannot produce a valid DrawingData object."""


def _build_client(max_retries: int):
    """Construct the Anthropic client, failing clearly if the key is absent."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not found. Copy .env.template to .env and set your key."
        )
    import anthropic  # imported here so the module imports without the package present

    return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)


def _image_block(image_b64: str, media_type: str, cache: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
    }
    if cache:
        # Cache the image so the low-confidence re-query reads it from cache
        # (~90% cheaper) instead of re-paying full image input tokens.
        block["cache_control"] = {"type": "ephemeral"}
    return block


def _drawing_data_tool() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Report the fully extracted structured data for this engineering drawing.",
        "input_schema": DrawingData.model_json_schema(),
    }


def _call(client, model: str, messages: list[dict[str, Any]]):
    # cache_control on the system block caches the static prefix (tools + system
    # ~4.6k tokens), which is byte-identical across every call and every drawing
    # in a batch — so within the cache window nearly all calls read it at ~10%
    # cost. The image carries its own cache breakpoint (see _image_block).
    #
    # Transient-failure policy: the SDK client itself retries connection errors,
    # 429s and 5xx with exponential backoff (max_retries=SDK_MAX_RETRIES). On top
    # of that, one extra retry round here with a longer backoff covers the case
    # where the SDK's whole retry budget is exhausted during a burst outage —
    # a paid multi-view extraction should not be lost to a 30-second blip.
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

    def _once():
        return client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=[_drawing_data_tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )

    import time

    import anthropic

    attempts = 1 + EXTRA_RETRY_ROUNDS
    for attempt in range(attempts):
        try:
            return _once()
        except anthropic.APIConnectionError as e:
            last = e
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 500, 502, 503, 529):
                raise  # non-transient (auth, bad request, ...) — fail immediately
            last = e
        if attempt < attempts - 1:
            delay = EXTRA_RETRY_BACKOFF_S * (2 ** attempt)
            log.warning("Transient API failure (%s); retrying in %.0fs "
                        "(extra round %d/%d)...", last, delay, attempt + 1, attempts - 1)
            time.sleep(delay)
    raise last


def _find_tool_use(response) -> Any:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block
    raise ExtractionError(
        f"Model did not call {TOOL_NAME!r} (stop_reason={getattr(response, 'stop_reason', None)})."
    )


def _tool_result_message(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }
        ],
    }


def _parse(
    client, model: str, messages: list[dict[str, Any]], usage: Optional[dict[str, int]] = None
) -> tuple[DrawingData, Any, Any]:
    """Call the model and return (validated DrawingData, response, tool_use block).

    Raises ExtractionError if the model refused, didn't call the tool, or the
    tool input fails Pydantic validation even after one repair retry. Token usage
    from every underlying call is accumulated into ``usage`` when provided.
    """
    response = _call(client, model, messages)
    if usage is not None:
        _accumulate_usage(usage, response)
    if getattr(response, "stop_reason", None) == "refusal":
        raise ExtractionError(
            "Model refused the extraction request "
            f"(stop_details={getattr(response, 'stop_details', None)})."
        )
    tool_use = _find_tool_use(response)

    try:
        return DrawingData.model_validate(tool_use.input), response, tool_use
    except ValidationError as e:
        log.warning("Tool input failed validation; requesting a repair: %s", e)
        # The API requires a tool_result for EVERY tool_use block in the
        # assistant turn — the model may emit more than one — or the repair
        # call itself 400s ("tool_use ids found without tool_result blocks").
        all_tool_uses = [b for b in response.content
                         if getattr(b, "type", None) == "tool_use"]
        repair_text = (f"Your input did not match the required schema:\n{e}\n"
                       f"Call {TOOL_NAME} again with corrected input.")
        results = [{
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": repair_text if tu.id == tool_use.id
                       else "Duplicate call ignored; see the other tool_result.",
            "is_error": True,
        } for tu in all_tool_uses]
        repair_messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": results},
        ]
        response2 = _call(client, model, repair_messages)
        if usage is not None:
            _accumulate_usage(usage, response2)
        if getattr(response2, "stop_reason", None) == "refusal":
            raise ExtractionError(
                "Model refused the repair request "
                f"(stop_details={getattr(response2, 'stop_details', None)})."
            )
        tool_use2 = _find_tool_use(response2)
        try:
            return DrawingData.model_validate(tool_use2.input), response2, tool_use2
        except ValidationError as e2:
            raise ExtractionError(f"Tool input failed validation after repair retry: {e2}") from e2


MULTIVIEW_USER_TEXT = (
    "You are given the SEPARATE orthographic views of ONE part — one image per "
    "view, labeled above in build order (front, top, side, second side, bottom). "
    "Build a SINGLE combined extraction by calling the tool:\n"
    "- The FRONT view defines the base profile; extrude it to the part depth read "
    "from the TOP or SIDE view. The FIRST build_order feature MUST be an "
    "extrude_boss with sketch_plane \"front\".\n"
    "- For EACH feature, set sketch_plane to the view it is seen/dimensioned in, "
    "using EXACTLY one of these words: \"front\", \"top\", \"side\", "
    "\"second_side\", \"bottom\".\n"
    "- A hole or cut visible in several views is the SAME feature — extract it "
    "ONCE, on the plane where its position is best dimensioned; never double-count.\n"
    "- Read dimensions, hole callouts, and per-instance positions from whichever "
    "view shows them; set instance_positions ([x,y] for EVERY instance, edge-"
    "referenced from the lower-left corner, len == qty) whenever a view dimensions "
    "the holes.\n"
    "- Every extrude_boss/extrude_cut MUST link a depth: set depth_dimension_id to "
    "the thickness/height read from the SIDE view (a through cut may stay through-"
    "all). Never emit an extrude with no depth.\n"
    "- FILLETS & CHAMFERS are routinely missed — scan EVERY view, section/detail, "
    "AND the notes block for radius/'R'/'R TYP'/chamfer callouts, inside corners of "
    "section profiles (web-to-flange, slot/pocket bottoms), and general notes ('ALL "
    "FILLETS R__', 'FILLET ALL INTERNAL CORNERS', 'BREAK SHARP EDGES'). For each, "
    "emit a fillet/chamfer FEATURE with its radius/distance linked in "
    "related_dimensions. A range callout like '.06/.09 R' still becomes a feature "
    "(use the nominal value, flag value_unclear). Never silently drop a fillet.\n"
    "- A TURNED/REVOLVED part (shaft/hub/flange, section symmetric about a "
    "centerline): make the base a `revolve` and fill revolve_profile with the "
    "ordered [axial, radial] half-profile points (radius = diameter/2).\n"
    "- A CIRCULAR/BOLT-CIRCLE hole pattern: set the callout's pattern='circular', "
    "bolt_circle_diameter, optional bolt_circle_center/start_angle, and qty.\n"
    "- build_order: base extrude/revolve first, then cuts/holes in view order, "
    "fillets and chamfers last, patterns after their seed.\n"
    "Extract everything; be honest about uncertainty (confidence, value_unclear, warnings)."
)


def _multiview_cache_key(views: list[tuple[str, str, str]], model: str,
                         requirements: Optional[list[str]] = None,
                         marked_b64: Optional[str] = None,
                         region_legend: Optional[str] = None) -> str:
    h = hashlib.sha256()
    h.update(PROMPT_VERSION.encode("utf-8"))
    h.update(b"\0")
    h.update(model.encode("utf-8"))
    for view_type, b64, _ in views:
        h.update(b"\0")
        h.update(view_type.encode("utf-8"))
        h.update(b"\0")
        h.update(b64.encode("utf-8"))
    for req in requirements or []:
        h.update(b"\0req\0")
        h.update(req.encode("utf-8"))
    # The human-marked view + its legend change the prompt, so changed markup
    # must force a fresh extraction (never serve a markup-blind cached result).
    if marked_b64:
        h.update(b"\0marked\0")
        h.update(marked_b64.encode("utf-8"))
    if region_legend:
        h.update(b"\0legend\0")
        h.update(region_legend.encode("utf-8"))
    return h.hexdigest()


MARKED_VIEW_INTRO = (
    "HUMAN-MARKED REFERENCE VIEW (operator ground truth): the next image is the "
    "SAME drawing with reviewer-drawn reference regions overlaid as colored boxes. "
    "Boxes sharing a color are ONE feature — typically a hole together with its "
    "X-dimension, Y-dimension, and center. Treat these as authoritative for "
    "identifying every hole/feature and placing it and its spacing correctly, "
    "especially where the raw linework or overlapping leaders are ambiguous. Do "
    "not miss a hole the operator boxed, and prefer the boxed count over an "
    "uncertain visual count. If a cyan crosshair labeled (0,0) is present, it is "
    "the operator-locked ORIGIN at the bottom-left of the top view — use it as the "
    "datum for hole positions so the model's orientation matches the drawing."
)


def extract_drawing_data_multiview(
    views: list[tuple[str, str, str]],
    model: Optional[str] = None,
    prep_warnings: Optional[list[str]] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    usage_out: Optional[dict[str, int]] = None,
    requirements: Optional[list[str]] = None,
    marked_view: Optional[tuple[str, str]] = None,
    region_legend: Optional[str] = None,
) -> dict[str, Any]:
    """Extract one combined part from SEPARATE per-view images.

    Args:
        views: ordered ``(view_type, image_b64, media_type)`` — one per view,
            in canonical order (front, top, side, second_side, bottom).
        requirements: operator must-meet specification lines (specs-first
            enforcement): injected into the extraction prompt so the model
            actively looks for those features from the start. Also part of the
            cache key, so changed notes force a fresh extraction.
        marked_view: optional ``(image_b64, media_type)`` of the human-annotated
            composite (drawing + colored reference-region boxes). When given, it
            is added as an extra whole-part context image with a legend so the
            model uses the reviewer's boxes for correct hole placement.
        region_legend: optional text describing each marked feature group
            (colors, roles, transcribed values, normalized positions).
        Other args mirror :func:`extract_drawing_data`.

    Each view image is labeled with its sketch plane so the model sets every
    feature's ``sketch_plane`` from the view it came from. Returns a dict
    conforming to :class:`DrawingData`.
    """
    from pipeline.view_ingest import VIEW_PLANES  # local import: avoids any cycle

    model = model or os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL
    if not views:
        raise ExtractionError("No view images supplied for multi-view extraction.")

    key = _multiview_cache_key(
        views, model, requirements,
        marked_b64=marked_view[0] if marked_view else None,
        region_legend=region_legend,
    )
    cached = _cache_lookup(cache_dir, key)
    if cached is not None:
        log.info("Multi-view extraction cache HIT (%s…) — skipping API call.", key[:12])
        if usage_out is not None:
            usage_out["cache_hits"] = usage_out.get("cache_hits", 0) + 1
        return cached

    client = _build_client(SDK_MAX_RETRIES)
    usage: dict[str, int] = {}

    # Overview/pictorial views (a full assembly or isometric shot) carry the whole
    # part for context but don't define a single sketch plane — label them so the
    # model still reads every dimension/feature there yet assigns each feature's
    # sketch_plane from the orthographic view it appears in.
    overview_views = {"full", "isometric", "iso", "overview", "pictorial", "3d"}
    content: list[dict[str, Any]] = []
    # The API allows at most 4 cache_control blocks per request and the system
    # prompt uses one, so cache at most 3 view images (a 4+-view part would
    # otherwise 400 outright, and the re-query re-sends images uncached anyway).
    cached_images = 0
    for view_type, b64, media_type in views:
        label = view_type.upper().replace("_", " ")
        if view_type.lower() in overview_views:
            content.append({"type": "text", "text": (
                f"=== {label} VIEW (OVERVIEW — whole-part context; read every dimension and "
                f"feature visible here too, but assign each feature's sketch_plane from the "
                f"orthographic view it appears in) ===")})
        else:
            plane = VIEW_PLANES.get(view_type, "Front Plane")
            content.append({"type": "text", "text": f"=== {label} VIEW — sketch on {plane} ==="})
        content.append(_image_block(b64, media_type, cache=cached_images < 3))
        cached_images += 1
    # Human-marked reference view (drawing + colored region boxes): added as an
    # extra whole-part context image so the model places holes per the operator's
    # boxes. Uncached (the cap of 3 cache breakpoints is spent on the views).
    if marked_view is not None:
        mb64, mmedia = marked_view
        content.append({"type": "text", "text": MARKED_VIEW_INTRO})
        content.append(_image_block(mb64, mmedia, cache=False))
        if region_legend and region_legend.strip():
            content.append({"type": "text", "text": region_legend.strip()})
        log.info("Human-marked reference view included in multiview extraction.")
    req_block = _requirements_block(requirements)
    if req_block:
        content.append({"type": "text", "text": req_block})
        log.info("Specs-first: %d must-meet specification(s) injected into extraction.",
                 len([r for r in requirements or [] if r and r.strip()]))
    content.append({"type": "text", "text": MULTIVIEW_USER_TEXT})

    log.info("Multi-view extraction: %d views, model %s", len(views), model)
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    data, response, tool_use = _parse(client, model, messages, usage)
    log.info(
        "Multi-view extraction: %d dimensions, %d features, confidence=%.2f",
        len(data.dimensions), len(data.features), data.confidence,
    )

    threshold = _confidence_threshold()
    has_focus = bool(data.warnings) or any(
        d.value_unclear or d.resolution_required for d in data.dimensions
    )
    if data.confidence < threshold and has_focus:
        log.warning("Confidence %.2f < %.2f — re-querying the views.", data.confidence, threshold)
        unclear = "; ".join(data.warnings) or "any dimensions you were unsure about"
        # The user turn after an assistant tool_use MUST lead with a tool_result
        # for EVERY tool_use block, or the API 400s ("tool_use ids were found
        # without tool_result blocks immediately after").
        requery: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": tu.id,
             "content": "Received. Take another look before finalizing."}
            for tu in response.content if getattr(tu, "type", None) == "tool_use"
        ]
        # Re-send the views WITHOUT cache_control: the originals already hold
        # the cache breakpoints and the API caps cache_control blocks at 4.
        for block in content:
            b = dict(block)
            b.pop("cache_control", None)
            requery.append(b)
        requery.append({
            "type": "text",
            "text": (
                f"Re-examine the views, paying special attention to: {unclear}. "
                "Produce a corrected, complete combined extraction by calling the tool again."
            ),
        })
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": requery})
        try:
            data, _, _ = _parse(client, model, messages, usage)
        except ExtractionError as e:
            log.warning("Re-query failed (%s); keeping initial extraction.", e)
            data.warnings.append(f"Low-confidence re-query failed: {e}")

    if prep_warnings:
        data.warnings.extend(prep_warnings)
    if usage:
        log.info(
            "Token usage: %d calls, input=%d (cache read=%d), output=%d",
            usage.get("calls", 0), usage.get("input_tokens", 0),
            usage.get("cache_read_input_tokens", 0), usage.get("output_tokens", 0),
        )
    if usage_out is not None:
        for k, v in usage.items():
            usage_out[k] = usage_out.get(k, 0) + v

    result = data.model_dump(mode="json")
    _cache_store(cache_dir, key, result)
    return result


def extract_drawing_data(
    image_b64: str,
    media_type: str = "image/png",
    model: Optional[str] = None,
    prep_warnings: Optional[list[str]] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    usage_out: Optional[dict[str, int]] = None,
    requirements: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Extract structured drawing data from a base64 image.

    Args:
        image_b64: Base64-encoded image (from :func:`utils.image_prep.prepare_image`).
        media_type: MIME type of the image (default ``image/png``).
        model: Model override; defaults to ``$EXTRACTION_MODEL`` or ``claude-sonnet-5``.
        prep_warnings: Warnings from image preparation, merged into the result so the
            operator sees them alongside the model's own warnings.
        cache_dir: If given, an on-disk extraction cache keyed by image+model hash;
            an exact re-run returns the cached result with NO API call (zero tokens).
        usage_out: If given, a dict updated with summed token usage for this call
            (``input_tokens``/``output_tokens``/cache fields/``calls``).
        requirements: operator must-meet specification lines, injected into the
            extraction prompt (specs-first) and mixed into the cache key.

    Returns:
        A JSON-serializable dict conforming to :class:`DrawingData`.

    Raises:
        EnvironmentError: if the API key is missing.
        ExtractionError: if the model refuses or returns unvalidatable output.
    """
    model = model or os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL

    # --- Extraction cache: identical image+model returns the saved result free ---
    key = _cache_key(image_b64, model, requirements)
    cached = _cache_lookup(cache_dir, key)
    if cached is not None:
        log.info("Extraction cache HIT (%s…) — skipping API call.", key[:12])
        if usage_out is not None:
            usage_out.setdefault("cache_hits", 0)
            usage_out["cache_hits"] += 1
        return cached

    client = _build_client(SDK_MAX_RETRIES)
    usage: dict[str, int] = {}

    log.info("Extracting drawing data with model %s", model)
    single_content: list[dict[str, Any]] = [_image_block(image_b64, media_type, cache=True)]
    req_block = _requirements_block(requirements)
    if req_block:
        single_content.append({"type": "text", "text": req_block})
        log.info("Specs-first: %d must-meet specification(s) injected into extraction.",
                 len([r for r in requirements or [] if r and r.strip()]))
    single_content.append({"type": "text", "text": INITIAL_USER_TEXT})
    messages: list[dict[str, Any]] = [{"role": "user", "content": single_content}]

    data, response, tool_use = _parse(client, model, messages, usage)
    log.info(
        "Initial extraction: %d dimensions, %d features, confidence=%.2f",
        len(data.dimensions),
        len(data.features),
        data.confidence,
    )

    # --- Domain-specific re-query on low confidence (once) ---
    # Only re-query when the model flagged something specific to re-examine
    # (unclear/resolution-required dims or warnings); a blind re-query with
    # nothing to point at rarely helps and doubles the tokens.
    threshold = _confidence_threshold()
    has_focus = bool(data.warnings) or any(
        d.value_unclear or d.resolution_required for d in data.dimensions
    )
    if data.confidence < threshold and has_focus:
        log.warning(
            "Confidence %.2f < %.2f — re-querying for a closer look.",
            data.confidence,
            threshold,
        )
        unclear = "; ".join(data.warnings) or "any dimensions you were unsure about"
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": "Received. Take another look before finalizing.",
                    },
                    _image_block(image_b64, media_type, cache=True),
                    {
                        "type": "text",
                        "text": (
                            "Re-examine the drawing carefully, paying special attention "
                            f"to these unclear areas: {unclear}. Zoom mentally into the "
                            "dimension callouts and produce a corrected, complete "
                            "extraction by calling the tool again. Improve confidence "
                            "only if the drawing genuinely supports it."
                        ),
                    },
                ],
            }
        )
        try:
            requeried, _, _ = _parse(client, model, messages, usage)
            log.info(
                "Re-query result: %d dimensions, confidence %.2f -> %.2f",
                len(requeried.dimensions),
                data.confidence,
                requeried.confidence,
            )
            data = requeried
        except ExtractionError as e:
            # Keep the first (valid) result; just note the re-query failed.
            log.warning("Re-query failed (%s); keeping initial extraction.", e)
            data.warnings.append(f"Low-confidence re-query failed: {e}")

    # Merge image-prep warnings so nothing is lost downstream.
    if prep_warnings:
        data.warnings.extend(prep_warnings)

    if usage:
        log.info(
            "Token usage: %d calls, input=%d (cache read=%d, write=%d), output=%d",
            usage.get("calls", 0), usage.get("input_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0), usage.get("output_tokens", 0),
        )
    if usage_out is not None:
        for k, v in usage.items():
            usage_out[k] = usage_out.get(k, 0) + v

    result = data.model_dump(mode="json")
    _cache_store(cache_dir, key, result)
    return result
