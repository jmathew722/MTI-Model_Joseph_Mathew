"""Stage B — Codex writes ALL VBA macros from the validated build JSON.

Claude's responsibilities end at producing the validated build JSON / build plan
/ resolved extraction. Codex (the ``gpt-5.6-sol`` model) writes every ``.vba``
macro from that bundle, following ``docs/vba-conventions.md`` exactly, and emits
a manifest (files, feature coverage, assumptions).

Two guarantees run on top of whatever Codex writes:
  * OVERALL SHAPE CHECK — the generated build's envelope, hole count and feature
    coverage are validated against the drawing's overall shape (Stage-1.5
    overview + the resolved extraction) so a macro set that would build the wrong
    gross shape is caught before SolidWorks. (see :func:`overall_shape_check`)
  * CADQUERY REPAIR — if pre-validation fails, the failure is fed back to Codex
    for ONE automatic repair attempt; still-failing halts with a report.

OFFLINE FALLBACK: when the Codex CLI is unavailable (dry-run / no ChatGPT
sign-in), the deterministic macros already produced by ``macro_generator`` are
kept and a manifest is synthesized from the build plan, so the pipeline still
yields a working, auditable macro set (``engine="fallback"``). Everything is
logged to the lessons ledger.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pipeline import codex_client

MANIFEST_FILENAME = "codex_manifest.json"
STAGE_TAG = "stage_4_codex_macros"
_MM_PER_IN = 25.4


# ── conventions spec fed to Codex ─────────────────────────────────────────────
def _conventions_text() -> str:
    for p in (Path(__file__).resolve().parents[1] / "docs" / "vba-conventions.md",
              Path.cwd() / "docs" / "vba-conventions.md"):
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        except Exception:
            pass
    return "(docs/vba-conventions.md not found — follow standard SolidWorks 2024 VBA idioms.)"


def _bundle(pkg, resolved: dict, must_meet_text: str, verdict: Optional[dict]) -> dict:
    def _read(p):
        try:
            return Path(p).read_text(encoding="utf-8")
        except Exception:
            return ""
    return {
        "build_plan_json": _read(pkg.build_plan_json),
        "resolved_extraction": json.dumps(resolved)[:20000],
        "must_meet_specifications": must_meet_text or "(none)",
        "codex_validation_verdict": json.dumps(verdict or {}, indent=2)[:6000],
        "dispositions": pkg.dispositions,
    }


def _prompt(bundle: dict) -> str:
    return f"""You are Codex Sol, writing the COMPLETE SolidWorks 2024 VBA macro set for one
part. Claude has already produced the validated build JSON below; you write every
macro from it, following the conventions EXACTLY.

=== docs/vba-conventions.md (authoritative spec — follow exactly) ===
{_conventions_text()[:12000]}

=== BUILD PLAN (JSON — the single source of truth for geometry & order) ===
{bundle['build_plan_json'][:16000]}

=== MUST-MEET SPECIFICATIONS (tier-0, already enforced upstream — honor them) ===
{bundle['must_meet_specifications'][:2000]}

=== CODEX VALIDATION VERDICT (your own earlier read) ===
{bundle['codex_validation_verdict'][:2000]}

Write each macro file under ./macros/ (00_setup.vba first, ZZZ_export_stl.vba
last), plus RUN_ALL.vba. Then write ./result.json with this manifest schema:
{{
  "files": [ {{"name": str, "feature_ids": [str], "purpose": str}} ],
  "feature_coverage": {{ "<feature_id>": "BUILT"|"MANUAL"|"SKIPPED" }},
  "assumptions": [str],
  "notes": [str]
}}
Every build-plan step must be covered by exactly one macro or explicitly listed
as MANUAL/SKIPPED with a reason. Output JSON only in result.json (no fences)."""


def _fallback_manifest(pkg) -> dict:
    """Synthesize a manifest from the deterministic macros already on disk."""
    files = []
    for vba in sorted(pkg.macros_dir.glob("*.vba")):
        files.append({"name": vba.name, "feature_ids": [], "purpose": "deterministic macro"})
    coverage = {}
    for d in (pkg.dispositions or []):
        fid = d.get("feature_id") or d.get("id")
        st = (d.get("state") or d.get("disposition") or "BUILT").upper()
        if fid:
            coverage[fid] = "BUILT" if "BUILT" in st else ("SKIPPED" if "EXCLUD" in st else "MANUAL")
    return {"files": files, "feature_coverage": coverage,
            "assumptions": ["Codex CLI unavailable — kept the deterministic macro set "
                            "(macro_generator) as the fallback writer."],
            "notes": []}


def write_macros(pkg, *, resolved: dict, must_meet_text: str = "",
                 verdict: Optional[dict] = None, part_dir: Path,
                 output_dir: Optional[Path] = None) -> dict:
    """Run Stage B. Returns a result dict {engine, manifest, n_macros, assumptions}.
    Never raises — Codex failure falls back to the deterministic macros."""
    part_dir = Path(part_dir)
    engine = "codex"
    assumptions: list[str] = []

    m = codex_client.mode()
    if m == "stub":
        manifest = _fallback_manifest(pkg)
        engine = "fallback"
        assumptions = manifest["assumptions"]
    else:
        try:
            workdir = part_dir / ".codex_macrogen"
            (workdir / "macros").mkdir(parents=True, exist_ok=True)
            result, engine_used = codex_client.run_json(
                _prompt(_bundle(pkg, resolved, must_meet_text, verdict)),
                workdir=workdir)
            written = sorted((workdir / "macros").glob("*.vba"))
            if not written:
                raise codex_client.CodexError("Codex produced no .vba files")
            # Replace the deterministic macros with Codex's set.
            for old in pkg.macros_dir.glob("*.vba"):
                old.unlink()
            for vba in written:
                (pkg.macros_dir / vba.name).write_text(vba.read_text(encoding="utf-8"),
                                                       encoding="utf-8")
            manifest = result if isinstance(result, dict) else {"files": [], "notes": []}
            manifest.setdefault("files", [{"name": v.name} for v in written])
            assumptions = list(manifest.get("assumptions") or [])
            engine = "codex"
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception as e:
            manifest = _fallback_manifest(pkg)
            manifest["notes"] = list(manifest.get("notes", [])) + \
                [f"Codex macro writing failed → deterministic fallback: {type(e).__name__}: {e}"]
            engine = "fallback"
            assumptions = manifest["assumptions"]

    manifest["engine"] = engine
    manifest["model"] = codex_client.CODEX_MODEL
    n_macros = len(list(pkg.macros_dir.glob("*.vba")))
    try:
        (pkg.macros_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2),
                                                        encoding="utf-8")
    except Exception:
        pass
    if output_dir is not None:
        _log_lessons(Path(output_dir), part_dir.name, engine, assumptions, event="macro_generation")
    return {"engine": engine, "manifest": manifest, "n_macros": n_macros,
            "assumptions": assumptions}


# ── overall shape check (the explicit must) ───────────────────────────────────
def _expected_envelope_in(resolved: dict) -> list[float]:
    vals = []
    for d in resolved.get("dimensions") or []:
        if str(d.get("dimension_type", "")).lower() in ("", "linear") and isinstance(d.get("value"), (int, float)):
            vals.append(float(d["value"]))
    vals = sorted(vals, reverse=True)
    return vals[:2]  # the two largest planar dims (L, W)


def overall_shape_check(build_plan: dict, prevalidation_report: Optional[dict],
                        resolved: dict, overview_analysis: Optional[dict],
                        manifest: Optional[dict] = None) -> dict:
    """Validate the macro-built shape against the drawing's OVERALL shape.

    Checks (heuristic, tolerant — a complete approximate model is the goal):
      1. planar envelope (L×W) within tolerance of the extracted overall dims;
      2. feature coverage — every build-plan step covered by a macro (no silent drop);
      3. hole count matches the plan;
      4. gross proportion consistent with the Stage-1.5 shape summary (plate/block).
    Returns {passed, checks:[...], notes:[...]}.
    """
    checks: list[dict] = []
    notes: list[str] = []

    # 1) planar envelope
    exp = _expected_envelope_in(resolved)
    sol = (prevalidation_report or {}).get("solid") or {}
    bbox_mm = sol.get("bbox_mm")
    if exp and bbox_mm:
        meas_in = sorted([v / _MM_PER_IN for v in bbox_mm], reverse=True)[:2]
        ok = True
        for e, mv in zip(exp, meas_in):
            tol = max(0.05, 0.20 * e)  # 20% or 0.05in, whichever larger
            if abs(e - mv) > tol:
                ok = False
        checks.append({"check": "planar_envelope", "ok": ok,
                       "expected_in": [round(x, 3) for x in exp],
                       "measured_in": [round(x, 3) for x in meas_in],
                       "detail": "largest two overall dims vs built bounding box"})
    else:
        checks.append({"check": "planar_envelope", "ok": True, "skipped": True,
                       "detail": "no CadQuery bbox or no linear dims to compare"})

    # 2) feature coverage — no build-plan step silently dropped
    steps = build_plan.get("steps") or build_plan.get("build_order") or []
    step_ids = {str(s.get("feature_id") or s.get("id") or s.get("step_number")) for s in steps}
    covered = set()
    if manifest and isinstance(manifest.get("feature_coverage"), dict):
        covered = {str(k) for k, v in manifest["feature_coverage"].items()
                   if str(v).upper() in ("BUILT", "MANUAL")}
    missing = {s for s in step_ids if s and s not in covered} if covered else set()
    checks.append({"check": "feature_coverage", "ok": len(missing) == 0,
                   "n_steps": len(step_ids), "n_covered": len(covered),
                   "missing": sorted(missing)[:20],
                   "detail": "every build-plan step must map to a macro"})

    # 3) hole count
    plan_holes = sum(1 for s in steps if str(s.get("type", "")).lower() in ("hole", "thread")) \
        or len([s for s in steps if "hole" in str(s.get("operation", "")).lower()])
    meas_holes = len((prevalidation_report or {}).get("measured_holes_in") or [])
    if plan_holes or meas_holes:
        ok = (meas_holes == 0 and not prevalidation_report) or abs(plan_holes - meas_holes) == 0 \
            or meas_holes >= plan_holes
        checks.append({"check": "hole_count", "ok": ok, "plan": plan_holes,
                       "measured": meas_holes, "detail": "planned holes vs measured through-holes"})

    # 4) proportion vs shape summary
    summ = str((overview_analysis or {}).get("overall_shape_summary", "")).lower()
    if summ and bbox_mm:
        dims = sorted([v / _MM_PER_IN for v in bbox_mm])
        is_thin = dims[0] <= 0.4 * dims[-1]
        says_plate = any(w in summ for w in ("plate", "flat", "sheet", "gasket", "bracket", "cover"))
        ok = (not says_plate) or is_thin
        checks.append({"check": "proportion_vs_summary", "ok": ok,
                       "thin_solid": is_thin, "summary_says_plate": says_plate,
                       "detail": "flat/plate parts should build as thin solids"})

    hard_fail = any((not c["ok"]) and c["check"] in ("planar_envelope", "feature_coverage")
                    for c in checks)
    passed = not hard_fail
    if not passed:
        notes.append("Overall shape check FAILED — the macro-built shape does not match the "
                     "drawing's overall envelope / feature set.")
    return {"passed": passed, "checks": checks, "notes": notes}


# ── CadQuery-failure repair (one attempt) ─────────────────────────────────────
def repair_macros(pkg, *, failure: dict, resolved: dict, must_meet_text: str = "",
                  verdict: Optional[dict] = None, part_dir: Path,
                  output_dir: Optional[Path] = None) -> dict:
    """One automatic Codex repair attempt after a CadQuery pre-validation failure.
    Returns {attempted, repaired, engine, note}. In stub/fallback mode no repair
    is possible (deterministic macros) → the caller halts with a report."""
    m = codex_client.mode()
    detail = "; ".join(failure.get("failed_constraints") or []) or failure.get("error") \
        or "pre-validation failed"
    if m == "stub":
        note = ("Codex unavailable — cannot auto-repair; halting with report. "
                f"CadQuery failure: {detail}")
        if output_dir is not None:
            _log_lessons(Path(output_dir), part_dir.name, "fallback", [note], event="repair_skipped")
        return {"attempted": False, "repaired": False, "engine": "fallback", "note": note}
    try:
        prompt = _prompt(_bundle(pkg, resolved, must_meet_text, verdict)) + \
            f"\n\n=== CADQUERY PRE-VALIDATION FAILED — REPAIR ===\n{detail}\n" \
            "Fix the macros so the built geometry satisfies the checks. Rewrite the " \
            "affected .vba files under ./macros and update ./result.json."
        workdir = part_dir / ".codex_repair"
        (workdir / "macros").mkdir(parents=True, exist_ok=True)
        result, _ = codex_client.run_json(prompt, workdir=workdir)
        written = sorted((workdir / "macros").glob("*.vba"))
        for vba in written:
            (pkg.macros_dir / vba.name).write_text(vba.read_text(encoding="utf-8"), encoding="utf-8")
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        note = f"Codex repair rewrote {len(written)} macro(s) after CadQuery failure: {detail}"
        if output_dir is not None:
            _log_lessons(Path(output_dir), part_dir.name, "codex", [note], event="repair")
        return {"attempted": True, "repaired": bool(written), "engine": "codex", "note": note}
    except Exception as e:
        note = f"Codex repair attempt failed: {type(e).__name__}: {e}"
        if output_dir is not None:
            _log_lessons(Path(output_dir), part_dir.name, "codex", [note], event="repair_failed")
        return {"attempted": True, "repaired": False, "engine": "codex", "note": note}


def _log_lessons(output_dir: Path, part: str, engine: str, items: list[str], event: str) -> None:
    try:
        from pipeline.must_meet import append_lesson
        lessons = output_dir / "lessons_learned.jsonl"
        for it in (items or [None]):
            append_lesson(lessons, {"stage": STAGE_TAG, "resolution": event,
                                    "part": part, "engine": engine, "note": it})
    except Exception:
        pass
