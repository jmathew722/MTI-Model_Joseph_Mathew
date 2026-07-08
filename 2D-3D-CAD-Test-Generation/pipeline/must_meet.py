"""Stage 2.6 — Must-Meet Spec Reconciliation (operator ground truth).

The operator's MUST-MEET SPECIFICATIONS text (the amber box in the web UI,
persisted as ``must_meet_spec.txt`` with the run) is parsed into structured,
machine-checkable constraints (``must_meet_constraints.json``) and applied to
the extraction as **priority tier 0**: a must-meet value overrides a
vision-extracted value on any conflict. Conflicts are never silently
discarded — both sides are logged to the lessons-learned JSONL with
``resolution: "spec_override"``.

Two parsers, same output schema:

  * :func:`parse_spec_text_llm` — a dedicated Claude call (forced tool use)
    for arbitrary natural-language spec text.
  * :func:`parse_spec_text_fallback` — a deterministic clause parser that
    needs no API key; also the safety net when the LLM call fails.

Constraint schema (see the project prompt; permissive — additive fields OK)::

    {"id": "MM-001", "source_text": "...", "type": "circular_pattern",
     "hole_count_total": 6, "notes": "count includes seed"}
    {"id": "MM-002", "type": "cut_extrude", "shape": "circle",
     "diameter_in": 3.88, "position": "center", "end_condition": "through_all"}
    {"id": "MM-003", "type": "cut_extrude", "diameter_in": 1.25,
     "position": {"reference": "center_of_MM-002", "offset_in": 2.94,
                  "direction": "down", "view": "front"},
     "end_condition": "through_all"}
    {"id": "MM-004", "type": "global_modifier", "applies_to": ["all_holes"],
     "end_condition": "through_all"}

Convention asserted here once and never re-interpreted downstream:
``hole_count_total`` / ``total_instances`` INCLUDES the seed (6 = seed + 5).

When the spec is ambiguous on a value the geometry needs (e.g. "6 holes
circular pattern" with no bolt circle diameter), the missing parameter is
derived from the vision extraction (:func:`fit_bolt_circle` — radius = mean of
sqrt(x²+y²) about the hole-centroid; CRITICAL if the fitted radii disagree by
more than ``BOLT_CIRCLE_FIT_TOL_IN``). Every derived value is recorded with
its derivation method so it lands in ``resolved_extraction.json``.

Public entry points: :func:`parse_spec_text`, :func:`apply_must_meet`,
:func:`run_spec_reconciliation`, :func:`fit_bolt_circle`,
:func:`equal_spacing_deviation`, :func:`append_lesson`.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

# The authoritative spec file persisted with a run (webapp writes it alongside
# notes.txt; the CLI discovers it like notes.txt).
MUST_MEET_FILENAME = "must_meet_spec.txt"
CONSTRAINTS_FILENAME = "must_meet_constraints.json"
LESSONS_FILENAME = "lessons_learned.jsonl"

# Fitted bolt-circle radii disagreeing by more than this (inches) = CRITICAL.
BOLT_CIRCLE_FIT_TOL_IN = 0.005
# Match tolerance when reconciling a spec diameter with an extracted one.
DIAMETER_MATCH_REL = 0.015
# Default positional tolerance (inches) when the drawing states none —
# used by the pattern-vs-coordinate routing deviation check.
DEFAULT_POSITION_TOL_IN = 0.01

_MM_TOOL_NAME = "report_must_meet_constraints"

_MM_SYSTEM_PROMPT = """You convert an operator's free-text MUST-MEET manufacturing \
specifications for a single machined part into structured constraints.

Rules:
- One constraint per requirement clause; ids MM-001, MM-002, ... in reading order.
- source_text quotes the clause (verbatim or lightly trimmed).
- type is one of: circular_pattern | cut_extrude | boss_extrude | hole | \
global_modifier | material | other.
- "N holes ... circular pattern" -> type circular_pattern with hole_count_total=N \
(the count INCLUDES the seed hole) and notes "count includes seed".
- A circular cut ("extrude cut ... D diameter") -> type cut_extrude, shape "circle", \
diameter_in=D. position is "center" when cut from the part center, otherwise an \
object {reference, offset_in, direction, view} where reference names the constraint \
whose center it is measured from (e.g. "center_of_MM-002"), direction is \
up|down|left|right, and view is the drawing view named.
- "through all" / "thru" anywhere in a clause -> end_condition "through_all" on that \
constraint; a standalone "all holes must be through all" -> a global_modifier with \
applies_to ["all_holes"] and end_condition "through_all".
- Numbers are inches unless the text says mm.
- Never invent values not present in the text; omit unknown fields entirely."""

_MM_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "source_text": {"type": "string"},
                    "type": {"type": "string"},
                    "hole_count_total": {"type": "integer"},
                    "shape": {"type": "string"},
                    "diameter_in": {"type": "number"},
                    "position": {},
                    "end_condition": {"type": "string"},
                    "applies_to": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["id", "source_text", "type"],
            },
        }
    },
    "required": ["constraints"],
}


# --------------------------------------------------------------------------- #
# Parsing — LLM primary, deterministic fallback
# --------------------------------------------------------------------------- #
def parse_spec_text_llm(text: str, usage_out: Optional[dict] = None) -> list[dict]:
    """Parse spec text with a dedicated Claude call (forced tool use).
    Raises on any API/parse problem — the caller falls back deterministically."""
    from pipeline.extractor import DEFAULT_MODEL, SDK_MAX_RETRIES, _build_client

    client = _build_client(SDK_MAX_RETRIES)
    model = os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=_MM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
        tools=[{
            "name": _MM_TOOL_NAME,
            "description": "Report the structured must-meet constraints.",
            "input_schema": _MM_TOOL_SCHEMA,
        }],
        tool_choice={"type": "tool", "name": _MM_TOOL_NAME},
    )
    if usage_out is not None:
        u = getattr(resp, "usage", None)
        if u is not None:
            usage_out["input_tokens"] = getattr(u, "input_tokens", 0)
            usage_out["output_tokens"] = getattr(u, "output_tokens", 0)
            usage_out["calls"] = 1
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == _MM_TOOL_NAME:
            return _normalize_constraints(list(block.input.get("constraints") or []))
    raise ValueError("model response contained no tool call")


_CLAUSE_SPLIT_RE = re.compile(
    r"(?:(?<=[.;])\s+)|(?:,\s*(?=there\s+(?:must|should|needs|shall)\b))",
    re.IGNORECASE,
)
_DIA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:in(?:ch(?:es)?)?\.?\s*)?(?:diameter|dia\b|ø)",
                     re.IGNORECASE)
_COUNT_HOLES_RE = re.compile(r"(\d+)\s*(?:x\s*)?holes?\b", re.IGNORECASE)
_OFFSET_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:in(?:ch(?:es)?)?\.?\s*)?"
                        r"(?:from|off)\s+the\s+cent", re.IGNORECASE)
_THRU_RE = re.compile(r"th(?:ro?u|rough)[\s-]*all", re.IGNORECASE)
_VIEW_RE = re.compile(r"\b(front|side|top|bottom|back|right|left)\s+view", re.IGNORECASE)


def parse_spec_text_fallback(text: str) -> list[dict]:
    """Deterministic clause parser: no API key needed, and the safety net when
    the LLM parse fails. Handles counted circular patterns, circular extrude
    cuts (centered or offset-from-center), and global through-all modifiers."""
    constraints: list[dict] = []
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(text or "") if c and c.strip()]

    def _next_id() -> str:
        return f"MM-{len(constraints) + 1:03d}"

    center_cut_id: Optional[str] = None
    for clause in clauses:
        low = clause.lower()
        thru = bool(_THRU_RE.search(low))

        m_count = _COUNT_HOLES_RE.search(clause)
        if m_count and "circular pattern" in low:
            constraints.append({
                "id": _next_id(),
                "source_text": clause,
                "type": "circular_pattern",
                "hole_count_total": int(m_count.group(1)),
                "notes": "count includes seed",
                **({"end_condition": "through_all"} if thru else {}),
            })
            continue

        m_dia = _DIA_RE.search(clause)
        if m_dia and ("extrude cut" in low or "cut" in low or "bore" in low):
            c: dict[str, Any] = {
                "id": _next_id(),
                "source_text": clause,
                "type": "cut_extrude",
                "shape": "circle",
                "diameter_in": float(m_dia.group(1)),
                "end_condition": "through_all" if thru else "through_all",
            }
            m_off = _OFFSET_RE.search(clause)
            if m_off:
                pos: dict[str, Any] = {"offset_in": float(m_off.group(1))}
                if center_cut_id:
                    pos["reference"] = f"center_of_{center_cut_id}"
                if re.search(r"\b(bottom|below|down)\b", low):
                    pos["direction"] = "down"
                elif re.search(r"\b(top|above|up)\b", low):
                    pos["direction"] = "up"
                elif re.search(r"\bleft\b", low):
                    pos["direction"] = "left"
                elif re.search(r"\bright\b", low):
                    pos["direction"] = "right"
                m_view = _VIEW_RE.search(clause)
                if m_view:
                    pos["view"] = m_view.group(1).lower()
                c["position"] = pos
            elif "center" in low or "centre" in low:
                c["position"] = "center"
                center_cut_id = c["id"]
            constraints.append(c)
            continue

        if thru and re.search(r"\ball\s+holes\b", low):
            constraints.append({
                "id": _next_id(),
                "source_text": clause,
                "type": "global_modifier",
                "applies_to": ["all_holes"],
                "end_condition": "through_all",
            })
            continue
        # Unrecognized clause: keep it (never silently drop operator intent).
        constraints.append({
            "id": _next_id(),
            "source_text": clause,
            "type": "other",
        })
    return _normalize_constraints(constraints)


def _normalize_constraints(constraints: list[dict]) -> list[dict]:
    """Re-sequence ids to MM-001.. (order preserved), coerce numerics, and fix
    stale cross-references after re-sequencing."""
    id_map: dict[str, str] = {}
    for i, c in enumerate(constraints, 1):
        old = str(c.get("id") or "")
        new = f"MM-{i:03d}"
        if old:
            id_map[old] = new
        c["id"] = new
        for k in ("diameter_in",):
            if c.get(k) is not None:
                c[k] = float(c[k])
        if c.get("hole_count_total") is not None:
            c["hole_count_total"] = int(c["hole_count_total"])
    for c in constraints:
        pos = c.get("position")
        if isinstance(pos, dict) and isinstance(pos.get("reference"), str):
            for old, new in id_map.items():
                if old != new and old in pos["reference"]:
                    pos["reference"] = pos["reference"].replace(old, new)
    return constraints


def parse_spec_text(text: str, use_llm: bool = True,
                    usage_out: Optional[dict] = None) -> tuple[list[dict], str]:
    """Parse spec text into constraints. Returns ``(constraints, parser_note)``.
    Tries the Claude parser first (when a key is available), falls back to the
    deterministic parser — the pipeline never blocks on a parse failure."""
    text = (text or "").strip()
    if not text:
        return [], "no must-meet specification text"
    if use_llm and os.getenv("ANTHROPIC_API_KEY"):
        try:
            constraints = parse_spec_text_llm(text, usage_out=usage_out)
            if constraints:
                return constraints, "parsed by Claude (dedicated spec-reconciliation call)"
        except Exception as e:  # fall back — never block Stage 2.6 on the API
            log.warning("LLM spec parse failed (%s) — using deterministic parser", e)
    return parse_spec_text_fallback(text), "parsed deterministically (no API call)"


# --------------------------------------------------------------------------- #
# Geometry helpers — bolt-circle fitting & equal-spacing deviation
# --------------------------------------------------------------------------- #
def fit_bolt_circle(positions: list[list[float]]) -> Optional[dict]:
    """Fit a bolt circle to hole centers: center = centroid, radius = mean of
    sqrt(dx²+dy²). Returns ``{center, radius, radii, max_disagreement}`` in the
    positions' units, or None with fewer than 3 points."""
    pts = [(float(p[0]), float(p[1])) for p in (positions or []) if len(p) >= 2]
    if len(pts) < 3:
        return None
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    radii = [math.hypot(x - cx, y - cy) for x, y in pts]
    mean_r = sum(radii) / len(radii)
    return {
        "center": [round(cx, 6), round(cy, 6)],
        "radius": round(mean_r, 6),
        "radii": [round(r, 6) for r in radii],
        "max_disagreement": round(max(abs(r - mean_r) for r in radii), 6),
    }


def equal_spacing_deviation(positions: list[list[float]],
                            center: list[float]) -> Optional[dict]:
    """Worst positional deviation of hole centers from ideal equal spacing on
    the fitted bolt circle (best-fit rotation offset). Units = input units."""
    pts = [(float(p[0]), float(p[1])) for p in (positions or []) if len(p) >= 2]
    if len(pts) < 3:
        return None
    cx, cy = float(center[0]), float(center[1])
    angles = sorted(math.atan2(y - cy, x - cx) for x, y in pts)
    n = len(angles)
    step = 2.0 * math.pi / n
    # Best-fit rotation offset: mean residual of each angle vs its ideal slot.
    resid = [a - i * step for i, a in enumerate(angles)]
    # Wrap-safe mean via vector average of residuals.
    mx = sum(math.cos(r) for r in resid) / n
    my = sum(math.sin(r) for r in resid) / n
    offset = math.atan2(my, mx)
    worst_ang = 0.0
    for i, a in enumerate(angles):
        d = a - (offset + i * step)
        d = math.atan2(math.sin(d), math.cos(d))  # wrap to [-pi, pi]
        worst_ang = max(worst_ang, abs(d))
    radius = sum(math.hypot(x - cx, y - cy) for x, y in pts) / n
    return {
        "worst_angle_deg": round(math.degrees(worst_ang), 4),
        "worst_arc_deviation": round(worst_ang * radius, 6),
        "seed_angle_deg": round(math.degrees(offset), 4),
    }


# --------------------------------------------------------------------------- #
# Lessons-learned JSONL
# --------------------------------------------------------------------------- #
def append_lesson(lessons_path: Path, record: dict) -> None:
    """Append one lessons-learned record (never raises)."""
    try:
        record = dict(record)
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        lessons_path = Path(lessons_path)
        lessons_path.parent.mkdir(parents=True, exist_ok=True)
        with lessons_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning("Could not append lessons-learned record: %s", e)


# --------------------------------------------------------------------------- #
# Application — tier-0 override of the extraction
# --------------------------------------------------------------------------- #
@dataclass
class MustMeetApplication:
    """Everything Stage 2.6 produced when applying constraints to an extraction."""

    extraction: dict                    # modified deep copy (schema-legal fields only)
    constraints: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)     # spec vs vision, spec won
    derived: list[dict] = field(default_factory=list)       # values derived + method
    flags: list[dict] = field(default_factory=list)         # CRITICAL/HIGH findings
    notes: list[str] = field(default_factory=list)

    def as_record(self) -> dict:
        """The block folded into resolved_extraction.json (additive key)."""
        return {
            "constraints": self.constraints,
            "conflicts": self.conflicts,
            "derived_values": self.derived,
            "flags": self.flags,
            "notes": self.notes,
        }


def _envelope_center(extraction: dict) -> Optional[tuple[float, float]]:
    """Best-effort (x, y) of the base-solid center in drawing units,
    lower-left origin (the build plan's coordinate convention)."""
    length = width = 0.0
    for d in extraction.get("dimensions") or []:
        token = (d.get("applies_to") or "").lower()
        v = d.get("value")
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        if "length" in token or token in ("overall_length", "od", "outside_diameter",
                                          "outer_diameter", "diameter"):
            length = max(length, float(v))
        if "width" in token or "height" in token:
            width = max(width, float(v))
    if length > 0 and width <= 0:
        width = length  # round part: envelope is the OD both ways
    if length <= 0:
        return None
    return (length / 2.0, width / 2.0)


def _match_diameter(target: float, value: Any) -> bool:
    return (isinstance(value, (int, float)) and value > 0
            and abs(float(value) - target) / target <= DIAMETER_MATCH_REL)


def apply_must_meet(extraction: dict, constraints: list[dict], *,
                    lessons_path: Optional[Path] = None,
                    part: str = "") -> MustMeetApplication:
    """Apply the constraints to a DEEP COPY of the extraction as priority
    tier 0. Only schema-legal fields are written (the strict ``extra='forbid'``
    schema must keep validating); all annotations live on the returned
    :class:`MustMeetApplication`."""
    ext = copy.deepcopy(extraction)
    app = MustMeetApplication(extraction=ext, constraints=constraints)
    if not constraints:
        return app

    def _conflict(cid: str, fld: str, vision_val: Any, spec_val: Any, where: str) -> None:
        rec = {
            "part": part, "constraint_id": cid, "field": fld, "where": where,
            "vision_value": vision_val, "spec_value": spec_val,
            "resolution": "spec_override",
        }
        app.conflicts.append(rec)
        if lessons_path is not None:
            append_lesson(lessons_path, {"kind": "spec_override_conflict", **rec})

    holes: list[dict] = ext.get("hole_callouts") or []

    # Pass 1 — reconcile cut_extrude constraints against extracted geometry so
    # the pattern pass can exclude those callouts from the pattern group.
    matched_cut_ids: set[int] = set()
    for c in constraints:
        if c.get("type") != "cut_extrude" or not c.get("diameter_in"):
            continue
        dia = float(c["diameter_in"])
        hit = None
        for h in holes:
            if id(h) in matched_cut_ids:
                continue
            if _match_diameter(dia, h.get("diameter")):
                hit = h
                break
        if hit is not None:
            matched_cut_ids.add(id(hit))
            c["matched_hole_callout"] = hit.get("id", "")
            if c.get("end_condition") == "through_all" and not hit.get("thru"):
                _conflict(c["id"], "thru", hit.get("thru"), True,
                          f"hole_callout {hit.get('id')}")
                hit["thru"] = True
            app.notes.append(f"{c['id']}: matched extracted callout "
                             f"{hit.get('id')} (Ø{dia:g})")
            continue
        # Also satisfied by a dimension on an extrude_cut feature?
        dim_hit = any(
            _match_diameter(dia, d.get("value"))
            for d in ext.get("dimensions") or []
            if "diam" in ((d.get("applies_to") or "") + (d.get("type") or "")).lower()
        )
        if dim_hit:
            app.notes.append(f"{c['id']}: Ø{dia:g} matched an extracted dimension")
            continue
        # Vision missed a must-have feature: synthesize it from the spec
        # (operator values are ground truth, not fabrication) and flag HIGH.
        pos = c.get("position")
        center = _envelope_center(ext)
        x = y = 0.0
        position_known = False
        if center is not None:
            x, y = center
            position_known = isinstance(pos, str) and pos == "center"
            if isinstance(pos, dict) and pos.get("offset_in"):
                off = float(pos["offset_in"])
                d = (pos.get("direction") or "down").lower()
                dx, dy = {"down": (0, -off), "up": (0, off),
                          "left": (-off, 0), "right": (off, 0)}.get(d, (0, -off))
                x, y = x + dx, y + dy
                position_known = True
        new_id = c["id"].replace("-", "")
        holes.append({
            "id": new_id,
            "type": "thru",
            "diameter": dia,
            "thru": True,
            "qty": 1,
            "x_position": round(x, 6),
            "y_position": round(y, 6),
            "position_known": position_known,
            "instance_positions": [[round(x, 6), round(y, 6)]] if position_known else [],
        })
        ext["hole_callouts"] = holes
        matched_cut_ids.add(id(holes[-1]))
        c["matched_hole_callout"] = new_id
        _conflict(c["id"], "feature_presence", "absent from vision extraction",
                  f"circle Ø{dia:g} ({pos})", "hole_callouts (synthesized)")
        app.flags.append({
            "severity": "HIGH", "constraint_id": c["id"],
            "what": (f"{c['id']}: Ø{dia:g} cut was in the must-meet spec but not in "
                     "the vision extraction — synthesized from the spec; verify "
                     "its position against the drawing."),
        })

    # Pass 2 — circular-pattern constraints (the pattern group = the callout
    # with the most instances that is NOT one of the matched single cuts).
    for c in constraints:
        if c.get("type") != "circular_pattern":
            continue
        n_req = int(c.get("hole_count_total") or 0)
        if n_req <= 0:
            continue
        group = None
        for h in holes:
            if id(h) in matched_cut_ids:
                continue
            if group is None or int(h.get("qty") or 1) > int(group.get("qty") or 1):
                group = h
        if group is None:
            app.flags.append({
                "severity": "CRITICAL", "constraint_id": c["id"],
                "what": (f"{c['id']}: spec requires {n_req} holes in a circular "
                         "pattern but the extraction has no candidate hole group — "
                         "a human must confirm the drawing before build."),
            })
            continue
        c["matched_hole_callout"] = group.get("id", "")
        qty = int(group.get("qty") or 1)
        n_pos = len(group.get("instance_positions") or [])
        if qty != n_req:
            if n_pos >= 3 and n_pos == qty:
                # The drawing explicitly dimensions n_pos hole positions and the
                # spec demands a different count: a GENUINE spec-vs-drawing
                # disagreement. Geometry keeps the drawing's positions (vector/
                # drawing owns position); the MM constraint keeps the spec count
                # and will grade FAIL (measured vs required) until a human
                # confirms which is right. Neither side is discarded.
                rec = {
                    "part": part, "constraint_id": c["id"], "field": "qty",
                    "where": f"hole_callout {group.get('id')}",
                    "vision_value": qty, "spec_value": n_req,
                    "resolution": "spec_vs_drawing_disagreement",
                }
                app.conflicts.append(rec)
                if lessons_path is not None:
                    append_lesson(lessons_path, {"kind": "spec_vs_drawing_count", **rec})
                app.flags.append({
                    "severity": "CRITICAL", "constraint_id": c["id"],
                    "what": (f"{c['id']}: spec requires {n_req} holes but the drawing "
                             f"explicitly dimensions {qty} — a human must confirm; the "
                             f"build keeps the drawing's {qty} positions and the "
                             f"constraint will grade against {n_req}."),
                })
            else:
                _conflict(c["id"], "qty", qty, n_req, f"hole_callout {group.get('id')}")
                group["qty"] = n_req
        if (group.get("pattern") or "none") != "circular":
            _conflict(c["id"], "pattern", group.get("pattern") or "none",
                      "circular", f"hole_callout {group.get('id')}")
            group["pattern"] = "circular"
        if c.get("end_condition") == "through_all" and not group.get("thru"):
            _conflict(c["id"], "thru", group.get("thru"), True,
                      f"hole_callout {group.get('id')}")
            group["thru"] = True

        # Derive the bolt circle when the spec doesn't give one.
        positions = group.get("instance_positions") or []
        bcd = group.get("bolt_circle_diameter")
        if not (isinstance(bcd, (int, float)) and bcd > 0):
            fit = fit_bolt_circle(positions)
            if fit is not None:
                group["bolt_circle_diameter"] = round(2.0 * fit["radius"], 6)
                group["bolt_circle_center"] = fit["center"]
                spacing = equal_spacing_deviation(positions, fit["center"])
                if spacing is not None and not group.get("start_angle"):
                    group["start_angle"] = spacing["seed_angle_deg"]
                app.derived.append({
                    "constraint_id": c["id"],
                    "value_name": "bolt_circle_radius_in",
                    "value": fit["radius"],
                    "derivation": ("fitted from extracted hole coordinates: radius = "
                                   "mean of sqrt(x²+y²) about the hole centroid"),
                    "fitted_radii": fit["radii"],
                    "max_radius_disagreement_in": fit["max_disagreement"],
                })
                if fit["max_disagreement"] > BOLT_CIRCLE_FIT_TOL_IN:
                    app.flags.append({
                        "severity": "CRITICAL", "constraint_id": c["id"],
                        "what": (f"{c['id']}: fitted bolt-circle radii disagree by "
                                 f"{fit['max_disagreement']:.4f} in "
                                 f"(> {BOLT_CIRCLE_FIT_TOL_IN} in) — the extracted hole "
                                 "coordinates do not lie on one circle; a human must "
                                 "confirm the pattern before trusting the build."),
                    })
                if lessons_path is not None and fit["max_disagreement"] > BOLT_CIRCLE_FIT_TOL_IN:
                    append_lesson(lessons_path, {
                        "kind": "bolt_circle_fit_disagreement", "part": part,
                        "constraint_id": c["id"], "fit": fit,
                    })
            else:
                app.flags.append({
                    "severity": "CRITICAL", "constraint_id": c["id"],
                    "what": (f"{c['id']}: circular pattern of {n_req} holes required "
                             "but no bolt circle diameter is available and fewer than "
                             "3 hole coordinates were extracted to fit one."),
                })

        # Routing-rule deviation check (Part 2c): drawing dimensioned X/Y but
        # spec demands a circular pattern — flag HIGH if the coordinates deviate
        # from ideal equal spacing beyond the positional tolerance.
        if len(positions) >= 3:
            center = group.get("bolt_circle_center") or fit_bolt_circle(positions)["center"]
            spacing = equal_spacing_deviation(positions, center)
            if spacing is not None:
                tol = _stated_position_tolerance(ext)
                c["equal_spacing_check"] = {**spacing, "tolerance_in": tol}
                if spacing["worst_arc_deviation"] > tol:
                    app.flags.append({
                        "severity": "HIGH", "constraint_id": c["id"],
                        "what": (f"{c['id']}: drawing hole coordinates deviate from "
                                 f"equal spacing by {spacing['worst_arc_deviation']:.4f} in "
                                 f"(> tol {tol:g}) — the spec (circular pattern) and the "
                                 "drawing genuinely disagree; a human must confirm "
                                 "before build."),
                    })
                    if lessons_path is not None:
                        append_lesson(lessons_path, {
                            "kind": "pattern_vs_coordinate_disagreement", "part": part,
                            "constraint_id": c["id"], "spacing": spacing,
                            "tolerance_in": tol, "resolution": "spec_override",
                        })

    # Pass 3 — global through-all modifier.
    for c in constraints:
        if c.get("type") != "global_modifier":
            continue
        if c.get("end_condition") == "through_all" and "all_holes" in (c.get("applies_to") or []):
            for h in holes:
                if not h.get("thru"):
                    _conflict(c["id"], "thru", h.get("thru"), True,
                              f"hole_callout {h.get('id')}")
                    h["thru"] = True
                    if (h.get("type") or "") == "blind":
                        h["type"] = "thru"
            app.notes.append(f"{c['id']}: through-all enforced on every hole callout")
    return app


def _stated_position_tolerance(extraction: dict) -> float:
    """The drawing's stated general tolerance (3-place decimal tolerance if
    present), else the default positional tolerance."""
    for d in extraction.get("dimensions") or []:
        tol = d.get("tolerances") or {}
        if isinstance(tol, dict):
            for k in ("general", "linear", "decimal_3"):
                v = tol.get(k)
                if isinstance(v, (int, float)) and 0 < v < 0.1:
                    return float(v)
    gt = extraction.get("general_tolerance")
    if isinstance(gt, (int, float)) and 0 < gt < 0.1:
        return float(gt)
    return DEFAULT_POSITION_TOL_IN


# --------------------------------------------------------------------------- #
# Constraint evaluation against measured geometry (shared by the CadQuery
# pre-validation and the trimesh post-build verification — same checks, two
# measurement backends)
# --------------------------------------------------------------------------- #
def evaluate_constraints(holes: list[dict], constraints: list[dict], *,
                         dia_tol_in: float = 0.01,
                         pos_tol_in: float = 0.03) -> list[dict]:
    """Grade every MM constraint against measured through-holes.

    ``holes``: ``[{"x", "y", "diameter", "through"}]`` in INCHES, from either
    backend. Returns one ``{"id", "status", "required", "measured", "detail"}``
    per constraint — PASS/FAIL with measured-vs-required numbers (never a
    generic error). Compliance is never fabricated: a constraint the geometry
    cannot express is reported ``NOT_CHECKED``."""
    from collections import Counter

    results: list[dict] = []

    # Resolve each diameter-bearing cut_extrude to its closest measured hole.
    cut_targets: dict[str, Optional[dict]] = {}
    for c in constraints:
        if c.get("type") == "cut_extrude" and c.get("diameter_in"):
            target = float(c["diameter_in"])
            best = None
            for hl in holes:
                if abs(hl["diameter"] - target) <= max(dia_tol_in, 0.02 * target):
                    if best is None or (abs(hl["diameter"] - target)
                                        < abs(best["diameter"] - target)):
                        best = hl
            cut_targets[c["id"]] = best
    matched = {id(hl) for hl in cut_targets.values() if hl is not None}

    for c in constraints:
        ctype = c.get("type")
        if ctype == "circular_pattern":
            n_req = int(c.get("hole_count_total") or 0)
            group = [hl for hl in holes if id(hl) not in matched]
            if group:
                mode = Counter(round(hl["diameter"], 2) for hl in group).most_common(1)[0][0]
                group = [hl for hl in group
                         if abs(hl["diameter"] - mode) <= max(dia_tol_in, 0.02 * mode)]
            n_thru = sum(1 for hl in group if hl["through"])
            detail = (f"{n_thru} equal-diameter through-hole(s) found in the "
                      f"pattern group; required {n_req}")
            if len(group) >= 3:
                fit = fit_bolt_circle([[hl["x"], hl["y"]] for hl in group])
                if fit is not None:
                    spacing = equal_spacing_deviation(
                        [[hl["x"], hl["y"]] for hl in group], fit["center"])
                    detail += (f"; bolt circle R {fit['radius']:.3f} in"
                               + (f", worst equal-spacing deviation "
                                  f"{spacing['worst_arc_deviation']:.4f} in"
                                  if spacing else ""))
            results.append({"id": c["id"], "status": "PASS" if n_thru == n_req else "FAIL",
                            "required": n_req, "measured": n_thru, "detail": detail})

        elif ctype == "cut_extrude" and c.get("diameter_in"):
            target = float(c["diameter_in"])
            hl = cut_targets.get(c["id"])
            if hl is None:
                results.append({"id": c["id"], "status": "FAIL",
                                "required": f"Ø{target:g} in",
                                "measured": "no matching hole",
                                "detail": f"no circular cut of Ø{target:g} in found in the solid"})
                continue
            ok = True
            notes = [f"Ø measured {hl['diameter']:.3f} in (required {target:.3f})"]
            required: Any = f"Ø{target:g} in"
            measured: Any = f"Ø{hl['diameter']:.3f} in"
            if c.get("end_condition") == "through_all":
                if hl["through"]:
                    notes.append("through-all confirmed")
                else:
                    ok = False
                    notes.append("NOT through-all")
            pos = c.get("position")
            if isinstance(pos, dict) and pos.get("offset_in") is not None \
                    and isinstance(pos.get("reference"), str):
                ref_id = pos["reference"].replace("center_of_", "")
                ref = cut_targets.get(ref_id)
                if ref is not None:
                    dist = math.hypot(hl["x"] - ref["x"], hl["y"] - ref["y"])
                    off = float(pos["offset_in"])
                    required = f"Ø{target:g} in at {off:g} in from {ref_id} center"
                    measured = f"Ø{hl['diameter']:.3f} in at {dist:.3f} in"
                    notes.append(f"offset from {ref_id} center measured {dist:.3f} in "
                                 f"(required {off:g} ± {pos_tol_in:g})")
                    if abs(dist - off) > pos_tol_in:
                        ok = False
            results.append({"id": c["id"], "status": "PASS" if ok else "FAIL",
                            "required": required, "measured": measured,
                            "detail": "; ".join(notes)})

        elif ctype == "global_modifier" and c.get("end_condition") == "through_all":
            n_not = sum(1 for hl in holes if not hl["through"])
            results.append({"id": c["id"],
                            "status": "PASS" if n_not == 0 else "FAIL",
                            "required": "every hole through-all",
                            "measured": (f"all {len(holes)} hole(s) through" if n_not == 0
                                         else f"{n_not} hole(s) not through"),
                            "detail": f"{len(holes)} hole(s) checked, {n_not} not through-all"})
        else:
            results.append({"id": c.get("id", "?"), "status": "NOT_CHECKED",
                            "required": c.get("source_text", ""), "measured": "",
                            "detail": "constraint type is not geometrically checkable here"})
    return results


# --------------------------------------------------------------------------- #
# Orchestration + file discovery
# --------------------------------------------------------------------------- #
def find_spec_file(folder: Path, part_name: str = "") -> Optional[Path]:
    """The must-meet spec file for a part folder: ``must_meet_spec.txt`` wins;
    the legacy ``notes.txt``/``requirements.txt`` (same operator text) is the
    fallback so existing part folders keep working."""
    folder = Path(folder)
    p = folder / MUST_MEET_FILENAME
    if p.is_file():
        return p
    from pipeline.requirements_check import find_notes_file

    return find_notes_file(folder, part_name)


def write_constraints_json(part_dir: Path, constraints: list[dict],
                           parser_note: str = "") -> Path:
    path = Path(part_dir) / CONSTRAINTS_FILENAME
    path.write_text(
        json.dumps({"constraints": constraints, "parser": parser_note}, indent=2),
        encoding="utf-8",
    )
    return path


def run_spec_reconciliation(
    spec_text: str,
    extraction: dict,
    *,
    part: str = "",
    lessons_path: Optional[Path] = None,
    use_llm: bool = True,
    usage_out: Optional[dict] = None,
) -> tuple[MustMeetApplication, str]:
    """Stage 2.6 entry point: parse the spec text and apply it tier-0 to the
    extraction. Exception-safe: on any failure the original extraction is
    returned untouched with an explanatory note (the pipeline never blocks)."""
    try:
        constraints, parser_note = parse_spec_text(spec_text, use_llm=use_llm,
                                                   usage_out=usage_out)
        if not constraints:
            return MustMeetApplication(extraction=extraction), parser_note
        app = apply_must_meet(extraction, constraints,
                              lessons_path=lessons_path, part=part)
        note = (f"{len(constraints)} constraint(s) [{parser_note}]; "
                f"{len(app.conflicts)} spec-override conflict(s), "
                f"{len(app.derived)} derived value(s), "
                f"{len(app.flags)} flag(s)")
        return app, note
    except Exception as e:  # Stage 2.6 must never sink a run
        log.warning("Spec reconciliation failed (non-fatal): %s", e)
        return MustMeetApplication(extraction=extraction), f"skipped: {type(e).__name__}: {e}"
