"""Severity-ranked engineering review — the human-facing decision report.

Every run produces one ranked list of every assumption, ambiguity resolution,
and skipped/manual feature, sorted most urgent first. It is written per part as
``<Part>_engineering_review.txt``, embedded in ``<Part>_build_plan.json`` under
``engineering_review``, and rendered in the web UI's Engineering Flags tab.

Severity scale (this file's contract — most urgent first):

    CRITICAL  build proceeded on a guess that could produce a wrong part;
              a human must verify before this part is trusted
    HIGH      resolved from limited information but reasonably confident;
              spot-check recommended
    MEDIUM    minor ambiguity, low risk if wrong
    LOW       cosmetic/non-dimensional assumption, informational only

Note the Stage 2.5 resolver uses its own tier vocabulary where HIGH means
"confident" and LOW means "low confidence". This module maps resolver tiers to
the review scale so the human-facing report reads urgency-first:

    resolver CRITICAL -> CRITICAL
    resolver LOW      -> HIGH      (low-confidence assumption: spot-check)
    resolver MEDIUM   -> MEDIUM
    resolver HIGH     -> LOW when an assumption was still made, else omitted
                         (a confirmed reading needs no human attention)

Skipped or manual-only features are never below HIGH: a feature the macros or
the COM build could not produce automatically is CRITICAL when it removes or
omits geometry, HIGH when it is a cosmetic/manual finishing step.

Public entry points: :func:`build_review_items`, :func:`format_review`,
:func:`write_review`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}

# Resolver flag tier -> review severity (see module docstring).
_RESOLVER_TIER_TO_SEVERITY = {
    "CRITICAL": "CRITICAL",
    "LOW": "HIGH",
    "MEDIUM": "MEDIUM",
    "HIGH": "LOW",
}

_SEVERITY_GUIDE = """\
Severity guide (most urgent first):
  CRITICAL  build proceeded on a guess that could produce a wrong part;
            human must verify before this part is trusted
  HIGH      resolved from limited information but reasonably confident;
            spot-check recommended
  MEDIUM    minor ambiguity, low risk if wrong
  LOW       cosmetic/non-dimensional assumption, informational only"""

def _item(severity: str, source: str, item_id: str, what: str,
          decision: str, why: str, affects: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "source": source,          # dimension | feature | macro | build
        "id": item_id,
        "what": what,              # what was ambiguous / missing
        "decision": decision,      # what value/decision was made
        "why": why,                # basis for the decision
        "affects": affects,        # which dimension/feature it affects
    }


def _dim_items(resolution) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for res in resolution.dim_resolutions.values():
        severity = _RESOLVER_TIER_TO_SEVERITY.get(res.flag_tier, "HIGH")
        if res.flag_tier == "HIGH" and not res.assumption_made:
            continue  # confirmed reading — nothing for a human to check
        items.append(_item(
            severity, "dimension", res.dimension_id,
            what=res.human_note,
            decision=f"resolved value {res.resolved_value:.6g}",
            why=res.assumption_basis.replace("_", " "),
            affects=f"dimension {res.dimension_id}"
                    + (f" (chain {'+'.join(res.chain_ids_used)})" if res.chain_ids_used else ""),
        ))
    return items


def _feature_items(resolution) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for fres in resolution.feature_resolutions.values():
        severity = _RESOLVER_TIER_TO_SEVERITY.get(fres.flag_tier, "HIGH")
        if fres.flag_tier == "HIGH":
            continue  # position read from the drawing — no action needed
        items.append(_item(
            severity, "feature", fres.feature_id,
            what=fres.human_note,
            decision=fres.position_assumption or "build",
            why="position not dimensioned on the drawing" if fres.position_assumption
                else "resolver decision",
            affects=f"feature {fres.feature_id}",
        ))
    return items


def _macro_items(pkg) -> list[dict[str, Any]]:
    """Skipped-prohibited and needs-review macro steps. Never silent, never LOW."""
    items: list[dict[str, Any]] = []
    for step in pkg.skipped:
        items.append(_item(
            "CRITICAL", "macro", step.feature_id,
            what=f"Feature {step.feature_id} ({step.feature_type}) cannot be scripted: "
                 f"{step.description}",
            decision="emitted as a numbered MANUAL step in the macros; no geometry "
                     "is created automatically",
            why=step.notes or "feature type is prohibited/unsupported for macro generation",
            affects=f"feature {step.feature_id}",
        ))
    for step in pkg.needs_review:
        items.append(_item(
            "HIGH", "macro", step.feature_id,
            what=f"Macro for {step.feature_id} ({step.feature_type}) needs manual "
                 f"review: {step.notes or step.description}",
            decision=f"macro {step.macro_file} generated with manual instructions",
            why=step.notes or "could not be fully scripted from extracted data",
            affects=f"feature {step.feature_id}",
        ))
    return items


def _overview_analysis_items(resolution) -> list[dict[str, Any]]:
    """Stage 1.5 holistic overview flags (cross-view conflicts, count
    disagreements). These carry ``source='overview_analysis'`` and record that
    tier 2 (the whole-sheet relational pass) raised them — exactly the class of
    finding a single cropped view cannot produce."""
    items: list[dict[str, Any]] = []
    for fl in getattr(resolution, "flags", []) or []:
        if fl.get("source") != "overview_analysis":
            continue
        severity = _RESOLVER_TIER_TO_SEVERITY.get(fl.get("flag_tier", ""), "MEDIUM")
        items.append(_item(
            severity, "overview_analysis", fl.get("dimension_id", ""),
            what=fl.get("human_note", ""),
            decision="flagged for human verification; the per-view extracted "
                     "geometry was kept (tier 1 owns dimensions)",
            why="raised by the Stage 1.5 holistic overview analysis "
                f"({fl.get('resolved_by_tier', 'tier2_overview')}) — cross-view "
                "context a single cropped view cannot provide",
            affects="cross-view consistency of the whole part",
        ))
    return items


def _build_items(build_skipped, build_caveats) -> list[dict[str, Any]]:
    """COM-build outcomes: skipped features are CRITICAL, caveats MEDIUM."""
    items: list[dict[str, Any]] = []
    for fid, ftype, reason in build_skipped or []:
        items.append(_item(
            "CRITICAL", "build", fid,
            what=f"The .sldprt build SKIPPED feature {fid} ({ftype}): {reason}",
            decision="feature is absent from the .sldprt; the VBA macros still "
                     "contain it as a step",
            why=reason,
            affects=f"feature {fid}",
        ))
    for caveat in build_caveats or []:
        items.append(_item(
            "MEDIUM", "build", "",
            what=f"Build caveat: {caveat}",
            decision="applied automatically during the .sldprt build",
            why="the 2D drawing carries no per-edge topology, so scope was chosen "
                "automatically",
            affects="see caveat text",
        ))
    return items


def build_review_items(
    resolution=None,
    pkg=None,
    build_skipped: Optional[list[tuple[str, str, str]]] = None,
    build_caveats: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Assemble every review item from all pipeline stages, sorted most urgent
    first (stable within a severity, in pipeline order)."""
    items: list[dict[str, Any]] = []
    if resolution is not None:
        items.extend(_dim_items(resolution))
        items.extend(_feature_items(resolution))
        items.extend(_overview_analysis_items(resolution))
    if pkg is not None:
        items.extend(_macro_items(pkg))
    items.extend(_build_items(build_skipped, build_caveats))
    items.sort(key=lambda it: _SEV_RANK.get(it["severity"], len(SEVERITY_ORDER)))
    return items


def severity_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {s: 0 for s in SEVERITY_ORDER}
    for it in items:
        counts[it["severity"]] = counts.get(it["severity"], 0) + 1
    return counts


def format_review(part: str, items: list[dict[str, Any]], resolution=None) -> str:
    """The plain-text report an executive can read without CAD background."""
    counts = severity_counts(items)
    lines = [
        f"ENGINEERING REVIEW - {part}",
        "=" * (21 + len(part)),
        "Every assumption, ambiguity resolution, and skipped/manual feature from",
        "this run, sorted most urgent first.",
        "",
        _SEVERITY_GUIDE,
        "",
        f"Totals: {counts['CRITICAL']} critical, {counts['HIGH']} high, "
        f"{counts['MEDIUM']} medium, {counts['LOW']} low.",
    ]
    if resolution is not None:
        s = resolution.summary
        lines.append(
            f"Rebuild confidence {s.rebuild_confidence:.0%}: "
            f"{s.assumptions_made} of {s.total_dimensions} dimension(s) required an "
            f"engineering assumption."
        )
    lines.append("")
    if not items:
        lines.append("No assumptions or manual steps. Every dimension was read directly "
                     "from the drawing and every feature was built automatically.")
    n = 0
    for severity in SEVERITY_ORDER:
        for it in items:
            if it["severity"] != severity:
                continue
            n += 1
            ident = f" {it['id']}" if it["id"] else ""
            lines.append(f"{n}. [{severity}]{ident} ({it['source']})")
            lines.append(f"   What:     {it['what']}")
            lines.append(f"   Decision: {it['decision']}")
            lines.append(f"   Why:      {it['why']}")
            lines.append(f"   Affects:  {it['affects']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_review(part_dir: Path, part: str, items: list[dict[str, Any]],
                 resolution=None) -> Path:
    """Write ``<part_dir>/<part>_engineering_review.txt`` and return the path."""
    part_dir = Path(part_dir)
    part_dir.mkdir(parents=True, exist_ok=True)
    path = part_dir / f"{part}_engineering_review.txt"
    path.write_text(format_review(part, items, resolution), encoding="utf-8")
    return path
