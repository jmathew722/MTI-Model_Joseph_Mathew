"""Iterative learning loop — one failure report per pipeline run.

Every time a part is run through the pipeline, this writes a human-readable
``.txt`` into the repo's top-level ``Learning Loop/`` folder capturing EVERY
failure and flag from that run: the READY/NOT-READY gate reasons, must-meet
constraint failures (measured vs required), Stage-1.5 cross-view conflicts, the
CRITICAL/HIGH engineering-review items, build/macro feature failures, and the
lessons-learned deltas. The report ends with a "FIXES FOR FABLE" section — a
concise, paste-ready brief (with suspected code areas per failure) that a human
hands to Claude to plan and apply code fixes, so the pipeline improves run over
run.

It reads the artifacts the pipeline already wrote into the part's output folder,
so it stays decoupled and can never break a run (every step is exception-safe).
A chronological ``Learning Loop/INDEX.md`` gets one line appended per run.

Public entry point: :func:`write_learning_log`.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

LEARNING_DIR_NAME = "Learning Loop"

# Failure category -> the code area most likely responsible, so the report can
# point a fix pass at the right module without a human having to triage first.
_SUSPECTED_AREA = {
    "constraint": "constraint_verify.py / cq_prevalidate.py (measurement) + macro_generator.py "
                  "& solidworks_builder.py (the feature that produced the wrong geometry; for "
                  "hole counts check the circular-pattern seed inclusion)",
    "overview": "overview_analysis.py (holistic read) + resolver.py (tier-2 flag / count "
                "cross-check) + extractor.py (per-view hole capture)",
    "requirement": "requirements_check.py (grading) + extractor.py/resolver.py (specs-first "
                   "application of the must-meet line)",
    "skipped": "macro_generator.py / solidworks_builder.py (prohibited/unsupported feature — "
               "add real support or a better manual step)",
    "macro": "solidworks_builder.py (COM feature build) + macro_generator.py (VBA for that "
             "feature type)",
    "prevalidation": "cq_prevalidate.py (CadQuery geometry) vs build_plan.json (the single "
                     "source of truth both build paths consume)",
}


def _repo_root(output_dir: Path) -> Path:
    """The repo root that CONTAINS this run's output (the folder holding
    ``Learning Loop/``). Walk up from the OUTPUT dir to the first ``.git`` — so
    real runs (output inside the repo) land in the repo-root ``Learning Loop/``,
    while a run whose output lives outside any repo (e.g. a pytest tmp dir)
    falls back to its own tree and never pollutes the real repo."""
    out = Path(output_dir).resolve()
    for parent in [out, *out.parents]:
        if (parent / ".git").exists():
            return parent
    return out.parent


def _load_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _macro_results(part_dir: Path) -> list[dict]:
    """macro_result.json is either {"results":[...]} (COM) or JSON Lines (VBA)."""
    p = part_dir / "macro_result.json"
    if not p.is_file():
        return []
    raw = _load_json(p)
    if isinstance(raw, dict):
        return raw.get("results", []) or []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name)) or "part"


def write_learning_log(part_dir: Path, part: str, status: str,
                       gate_reasons: list[str], output_dir: Path,
                       model: str = "") -> Optional[Path]:
    """Write one ``Learning Loop/<part>__<timestamp>.txt`` failure report for a
    completed run. Returns the path, or ``None`` on any failure (never raises)."""
    try:
        part_dir = Path(part_dir)
        ts = datetime.now()
        categories: set[str] = set()   # which suspected-area hints to include
        sections: list[str] = []

        # 1. Gate / status ---------------------------------------------------
        lines = [f"1. GATE / STATUS — {status}"]
        if gate_reasons:
            for g in gate_reasons:
                lines.append(f"   - {g}")
        else:
            lines.append("   - No gate failures (run reached READY).")
        sections.append("\n".join(lines))

        # 2. Must-meet constraint failures (post-build wins; else prevalidation)
        cv = _load_json(part_dir / "constraint_verification.json")
        pv = _load_json(part_dir / "prevalidation_report.json")
        data, stage = (cv, "post-build (measured from the built STL)") if cv else \
                      (pv, "pre-validation (CadQuery, before SolidWorks)") if pv else (None, "")
        mm_lines = ["2. MUST-MEET CONSTRAINT FAILURES"]
        n_mm_fail = 0
        if data:
            for c in data.get("constraints", []) or []:
                if str(c.get("status", "")).upper() == "FAIL":
                    n_mm_fail += 1
                    mm_lines.append(f"   - [{c.get('id')}] FAIL — required {c.get('required')}, "
                                    f"measured {c.get('measured')}: {c.get('detail', '')}")
            if data.get("error"):
                mm_lines.append(f"   - verification error: {data['error']}")
                n_mm_fail += 1
            mm_lines[0] += f"  ({stage})"
        if n_mm_fail:
            categories.update(("constraint", "prevalidation"))
        else:
            mm_lines.append("   - none")
        sections.append("\n".join(mm_lines))

        # 3. Cross-view conflicts (Stage 1.5 overview analysis) --------------
        ov = _load_json(part_dir / "overview_analysis.json")
        ov_lines = ["3. CROSS-VIEW CONFLICTS (Stage 1.5 overview analysis)"]
        conflicts = (ov or {}).get("cross_view_conflicts", []) or []
        if conflicts:
            categories.add("overview")
            for c in conflicts:
                sev = str(c.get("severity", "MEDIUM")).upper()
                ov_lines.append(f"   - [{sev}] {c.get('description', '')}")
                if c.get("recommendation"):
                    ov_lines.append(f"       -> {c['recommendation']}")
        else:
            ov_lines.append("   - none")
        sections.append("\n".join(ov_lines))

        # 4. Engineering review — CRITICAL & HIGH (from build_plan.json) -----
        plan = _load_json(next(iter(part_dir.glob("*_build_plan.json")), Path("/nonexistent")))
        review = (plan or {}).get("engineering_review", []) if isinstance(plan, dict) else []
        er_lines = ["4. ENGINEERING REVIEW — CRITICAL & HIGH"]
        urgent = [i for i in review if i.get("severity") in ("CRITICAL", "HIGH")]
        if urgent:
            for i in urgent:
                src = i.get("source", "")
                if src == "overview_analysis":
                    categories.add("overview")
                elif src in ("macro", "build"):
                    categories.add("skipped" if "skipp" in (i.get("what", "").lower()) else "macro")
                elif src == "requirement":
                    categories.add("requirement")
                er_lines.append(f"   - [{i.get('severity')}] {i.get('id', '')} ({src}): {i.get('what', '')}")
                if i.get("decision"):
                    er_lines.append(f"       decision: {i['decision']}")
                if i.get("why"):
                    er_lines.append(f"       why: {i['why']}")
        else:
            er_lines.append("   - none above HIGH")
        # Counts of everything for context
        counts: dict[str, int] = {}
        for i in review:
            counts[i.get("severity", "?")] = counts.get(i.get("severity", "?"), 0) + 1
        if counts:
            er_lines.append("   (all severities: " +
                            ", ".join(f"{k} {v}" for k, v in counts.items()) + ")")
        sections.append("\n".join(er_lines))

        # 5. Build / macro feature failures ----------------------------------
        mr = _macro_results(part_dir)
        mk_lines = ["5. BUILD / MACRO FEATURE FAILURES"]
        fails = [r for r in mr if str(r.get("status", "")).upper() == "FAIL"]
        if fails:
            categories.add("macro")
            for r in fails:
                mk_lines.append(f"   - feature {r.get('feature', '?')} FAILED: {r.get('detail', '')}")
        else:
            mk_lines.append("   - none")
        sections.append("\n".join(mk_lines))

        # 6. FIXES FOR FABLE (paste-ready) -----------------------------------
        fix_lines = ["FIXES FOR FABLE (paste this whole file to Claude to plan & apply code fixes)"]
        if not (gate_reasons or n_mm_fail or conflicts or urgent or fails):
            fix_lines.append("   - Clean run: no failures to address this time. Keep as a "
                             "positive baseline for regression comparison.")
        else:
            fix_lines.append(f"   - Part '{part}' finished {status}. Address the items above so the "
                             "next run on this drawing improves. Suspected code areas:")
            for cat in sorted(categories):
                fix_lines.append(f"     * {cat}: {_SUSPECTED_AREA[cat]}")
            fix_lines.append("   - Prefer a fix that generalizes (fixes the class of failure), not "
                             "a one-off patch for this single part.")
            fix_lines.append("   - Cross-check against the artifacts in: " + str(part_dir))
        sections.append("\n".join(fix_lines))

        header = [
            f"LEARNING LOOP — {part}",
            "=" * 60,
            f"Run:     {ts.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Status:  {status}",
            f"Model:   {model or '(default)'}",
            f"Outputs: {part_dir}",
            "",
            "Every failure/flag from this run, followed by a paste-ready brief for Claude.",
            "=" * 60,
            "",
        ]
        body = "\n".join(header) + "\n\n".join(sections) + "\n"

        root = _repo_root(output_dir)
        ldir = root / LEARNING_DIR_NAME
        ldir.mkdir(parents=True, exist_ok=True)
        path = ldir / f"{_safe(part)}__{ts.strftime('%Y-%m-%d_%H%M%S')}.txt"
        path.write_text(body, encoding="utf-8")

        # Chronological index (one line per run).
        try:
            idx = ldir / "INDEX.md"
            n_fail = len(gate_reasons) + n_mm_fail + len(conflicts) + len(fails)
            first = "index" if not idx.exists() else ""
            if first:
                idx.write_text("# Learning Loop index\n\nOne line per pipeline run, newest at the "
                               "bottom. Hand any run's `.txt` to Claude to plan code fixes.\n\n"
                               "| Time | Part | Status | Failures | File |\n"
                               "|------|------|--------|---------:|------|\n", encoding="utf-8")
            with idx.open("a", encoding="utf-8") as f:
                f.write(f"| {ts.strftime('%Y-%m-%d %H:%M')} | {part} | {status} | "
                        f"{n_fail} | {path.name} |\n")
        except OSError:
            pass

        log.info("Learning loop: wrote %s", path)
        return path
    except Exception as e:  # never let the learning log break a run
        log.warning("Learning loop write failed (non-fatal): %s: %s", type(e).__name__, e)
        return None
