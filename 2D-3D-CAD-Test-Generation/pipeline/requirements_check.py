"""Human-authored specification notes -> tracked, graded requirements.

The operator's "must-meet" notes (one requirement per line/bullet, written in
the web UI's Part Setup tab and saved as ``notes.txt`` in the part's views
folder, or passed with ``--requirements FILE``) stop being a comment here: each
line becomes a requirement with an id and a checked status, persisted as
``<Part>_requirements.json`` and folded into the engineering review.

Statuses (honest by design — compliance is NEVER fabricated):

    met             checkable against the extraction/build and satisfied
    partial         the feature exists but a number in the note has no matching
                    dimension (or vice versa)
    unmet           checkable and NOT satisfied
    not_applicable  cannot be verified against drawing geometry (process,
                    finish, deburr...) — listed so it is never silently dropped

Severity mapping into the engineering review: unmet -> CRITICAL (gates READY),
partial -> HIGH, not_applicable -> MEDIUM, met -> LOW (informational).

Checking is deliberately conservative keyword/number matching, not NLP: a
number in a note matches when some extracted dimension value agrees within
1.5% (inch<->mm conversions tried); a feature keyword matches when the build
inventory contains that feature type. Anything the checker cannot decide is
marked honestly rather than guessed.

Public entry points: :func:`parse_requirements`, :func:`check_requirements`,
:func:`run_requirements_check`, :func:`write_requirements_json`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

_BULLET_RE = re.compile(r"^\s*(?:[-*•·>]+|\(?\d+[.)])\s*")
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")

# Requirement keyword -> the build-inventory kinds that satisfy it.
_FEATURE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "hole": ("hole", "drill", "bore", "drilled"),
    "thread": ("thread", "tap", "tapped", "unc", "unf", "npt", "metric thread"),
    "fillet": ("fillet", "radius", "radii", "rounded corner"),
    "chamfer": ("chamfer", "bevel", "beveled"),
    "cutout": ("slot", "pocket", "cutout", "cut-out", "groove", "keyway", "notch"),
    "pattern": ("pattern", "array", "bolt circle", "equally spaced", "spaced holes"),
    "shell": ("shell", "hollow", "wall thickness"),
    "counterbore": ("counterbore", "c'bore", "cbore", "spotface"),
    "countersink": ("countersink", "csink", "c'sink"),
    "boss": ("boss", "standoff", "extrude", "pad"),
    "revolve": ("revolve", "revolved", "turned", "lathe"),
}

# Notes about process/finish/documentation — real requirements, but not
# checkable against extracted geometry. Material IS checkable (title block).
_NONGEOM_KEYWORDS = (
    "finish", "coat", "coating", "anodize", "anodized", "paint", "plate",
    "plated", "plating", "heat treat", "hardness", "deburr", "break sharp",
    "break all", "passivate", "polish", "clean", "packag", "label", "mark",
    "inspect", "certif",
)
_MATERIAL_KEYWORDS = (
    "material", "aluminum", "aluminium", "steel", "stainless", "brass",
    "copper", "bronze", "titanium", "delrin", "nylon", "abs", "peek",
    "6061", "7075", "5052", "2024", "304", "316", "4140", "1018", "a36",
)


def parse_requirements(text: str) -> list[dict[str, Any]]:
    """Split the notes text into discrete requirements (one per line/bullet).
    Blank lines and pure headings (lines ending with ':') are skipped."""
    reqs: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        cleaned = _BULLET_RE.sub("", line).strip()
        if not cleaned or cleaned.endswith(":"):
            continue
        reqs.append({"id": f"R{len(reqs) + 1:03d}", "text": cleaned})
    return reqs


def _dimension_values(extraction: dict) -> list[float]:
    vals: list[float] = []
    for d in extraction.get("dimensions") or []:
        for k in ("resolved_value", "value"):
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 0:
                vals.append(float(v))
    for h in extraction.get("hole_callouts") or []:
        for k in ("diameter", "depth", "cbore_diameter", "csink_diameter",
                  "bolt_circle_diameter"):
            v = h.get(k)
            if isinstance(v, (int, float)) and v > 0:
                vals.append(float(v))
    return vals


def _number_matched(v: float, known: list[float]) -> bool:
    """True when some extracted value agrees with ``v`` within 1.5%
    (the value as written, or converted inch<->mm)."""
    for cand in (v, v * 25.4, v / 25.4):
        for k in known:
            if k > 0 and abs(cand - k) / max(k, 1e-9) <= 0.015:
                return True
    return False


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def check_requirements(reqs: list[dict[str, Any]], extraction: dict) -> list[dict[str, Any]]:
    """Grade each requirement against the (resolved) extraction. Mutates and
    returns ``reqs`` with ``status`` and ``note`` filled per requirement."""
    from pipeline.overview_check import _build_inventory

    inv = _build_inventory(extraction)
    dim_values = _dimension_values(extraction)
    material = str(extraction.get("material") or "")
    finish = str(extraction.get("finish") or "")

    for r in reqs:
        text = r["text"]
        low = text.lower()
        numbers = [float(m) for m in _NUM_RE.findall(low)]
        kinds_hit = [k for k, needles in _FEATURE_KEYWORDS.items()
                     if any(n in low for n in needles)]
        is_material = any(k in low for k in _MATERIAL_KEYWORDS)
        is_nongeom = any(k in low for k in _NONGEOM_KEYWORDS)

        if is_material:
            req_tok = _tokens(text)
            mat_tok = _tokens(material) | _tokens(finish)
            # Meaningful overlap = an alloy/material token, not glue words.
            overlap = {t for t in req_tok & mat_tok
                       if t in _MATERIAL_KEYWORDS or any(c.isdigit() for c in t)}
            if overlap:
                r["status"] = "met"
                r["note"] = (f"title block matches ({', '.join(sorted(overlap))}: "
                             f"material '{material}'" + (f", finish '{finish}'" if finish else "") + ")")
            elif material or finish:
                r["status"] = "unmet"
                r["note"] = (f"title block reads material '{material}'"
                             + (f", finish '{finish}'" if finish else "")
                             + " — does not match this requirement")
            else:
                r["status"] = "not_applicable"
                r["note"] = "no material/finish read from the drawing — verify manually"
            continue

        if is_nongeom and not kinds_hit:
            r["status"] = "not_applicable"
            r["note"] = "process/finish requirement — cannot be verified against geometry; verify manually"
            continue

        num_ok = any(_number_matched(v, dim_values) for v in numbers) if numbers else None
        feat_ok = any(inv.get(k, 0) > 0 for k in kinds_hit) if kinds_hit else None

        if feat_ok is None and num_ok is None:
            r["status"] = "not_applicable"
            r["note"] = "no checkable feature keyword or numeric value found — verify manually"
        elif feat_ok is False:
            r["status"] = "unmet"
            r["note"] = (f"no {'/'.join(kinds_hit)} feature found in the build"
                         + (" (numeric value matched a dimension)" if num_ok else ""))
        elif feat_ok is True and num_ok is False:
            r["status"] = "partial"
            r["note"] = (f"{'/'.join(kinds_hit)} feature(s) exist, but no extracted "
                         f"dimension matches the value(s) {', '.join(f'{v:g}' for v in numbers)} (±1.5%)")
        elif feat_ok is None and num_ok is False:
            r["status"] = "unmet"
            r["note"] = (f"no extracted dimension matches {', '.join(f'{v:g}' for v in numbers)} "
                         "(±1.5%, inch/mm checked)")
        else:  # everything checkable matched
            parts = []
            if feat_ok:
                parts.append(f"{'/'.join(kinds_hit)} present in the build")
            if num_ok:
                parts.append("numeric value(s) match extracted dimensions")
            r["status"] = "met"
            r["note"] = "; ".join(parts) or "matched"
    return reqs


_STATUS_SEVERITY = {
    "unmet": "CRITICAL",
    "partial": "HIGH",
    "not_applicable": "MEDIUM",
    "met": "LOW",
}


def review_items(reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Engineering-review item dicts (source="requirement") for graded reqs."""
    items = []
    for r in reqs:
        status = r.get("status", "not_applicable")
        items.append({
            "severity": _STATUS_SEVERITY.get(status, "MEDIUM"),
            "source": "requirement",
            "id": r["id"],
            "status": status,
            "what": f"Operator requirement: \"{r['text']}\"",
            "decision": f"status: {status.upper()}",
            "why": r.get("note", ""),
            "affects": "human-specified must-meet requirement",
        })
    return items


def write_requirements_json(part_dir: Path, safe_name: str,
                            reqs: list[dict[str, Any]]) -> Path:
    counts: dict[str, int] = {}
    for r in reqs:
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    path = Path(part_dir) / f"{safe_name}_requirements.json"
    path.write_text(json.dumps({"requirements": reqs, "summary": counts}, indent=2),
                    encoding="utf-8")
    return path


# Filenames auto-discovered in a part's views folder (first hit wins).
NOTES_FILENAMES = ("notes.txt", "requirements.txt")


def find_notes_file(folder: Path, part_name: str = "") -> Optional[Path]:
    """The human notes file for a part folder, if any."""
    folder = Path(folder)
    candidates = list(NOTES_FILENAMES)
    if part_name:
        candidates.insert(0, f"{part_name}_notes.txt")
    for name in candidates:
        p = folder / name
        if p.is_file():
            return p
    return None


def run_requirements_check(
    notes_file: Optional[Path],
    extraction: dict,
) -> tuple[list[dict[str, Any]], str]:
    """Exception-safe wrapper: returns ``(graded_requirements, note)``.
    Empty list + note when there is no notes file or it cannot be read."""
    try:
        if notes_file is None or not Path(notes_file).is_file():
            return [], "skipped: no requirements/notes file for this part"
        text = Path(notes_file).read_text(encoding="utf-8", errors="replace")
        reqs = parse_requirements(text)
        if not reqs:
            return [], f"skipped: {Path(notes_file).name} contains no requirement lines"
        check_requirements(reqs, extraction)
        n_unmet = sum(1 for r in reqs if r["status"] == "unmet")
        return reqs, (f"{len(reqs)} requirement(s) from {Path(notes_file).name}: "
                      f"{n_unmet} unmet")
    except Exception as e:  # never sink a build over the notes check
        log.warning("Requirements check failed (non-fatal): %s", e)
        return [], f"skipped: {type(e).__name__}: {e}"
