"""Batch processing for many drawings (scale-to-thousands triage).

Walks a directory, runs the full Phase-1 + macro-generation pipeline for each
input, and writes a single ``batch_summary.csv`` so a large set of drawings can
be triaged at a glance: which are READY, which are BLOCKED (and why), and each
one's drawing-completeness scores.

Two input kinds are handled:
  * drawings (``.pdf/.png/.jpg/.tif``) — extracted via the injected ``extract_fn``
    (the Claude Vision call; costs API credits).
  * ``*_extraction.json`` — loaded directly (no API call, free re-runs).

Every input always gets its extraction + verification report persisted (even
BLOCKED), mirroring the single-part path. The extractor is injected so this
module — and its tests — never import the API client.

Public entry points: :func:`iter_inputs`, :func:`run_batch`.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from pipeline.macro_generator import MacroGenerationError, generate_macro_package
from pipeline.reconciliation import RECONCILIATION_REPORT_SUFFIX
from pipeline.validator import format_verification_report, run_verification
from utils.logger import get_logger

log = get_logger()

DRAWING_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
ExtractFn = Callable[[Path], dict]

# Common SolidWorks 2024 part templates, used if SOLIDWORKS_TEMPLATE_PATH is unset.
_TEMPLATE_FALLBACKS = (
    r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\Part.PRTDOT",
    r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\Part.PRTDOT",
)


def resolve_part_template(template_path: Optional[str] = None) -> Optional[str]:
    """Resolve a usable .prtdot template: explicit arg > env > known fallbacks."""
    import os

    cand = template_path or os.getenv("SOLIDWORKS_TEMPLATE_PATH")
    if cand and Path(cand).exists():
        return cand
    for fb in _TEMPLATE_FALLBACKS:
        if Path(fb).exists():
            return fb
    return cand  # may be None / nonexistent; create_new_part will report clearly


def build_sldprt_for_part(sw_app, model, part_dir: Path, name: str,
                          template_path: Optional[str] = None,
                          skipped_out: Optional[list] = None,
                          caveats_out: Optional[list] = None,
                          deferred_out: Optional[list] = None) -> str:
    """Build one validated part into ``<part_dir>/<name>.sldprt`` over COM and
    write a ``<name>_model_check.txt``. Non-strict: a fragile feature is skipped
    (and documented) rather than aborting the build. Returns the saved path.

    When ``skipped_out`` / ``caveats_out`` lists are given, every skipped feature
    ``(id, type, reason)`` and every build caveat string is appended so the
    caller can fold them into the engineering review."""
    from pipeline.model_validator import validate_model
    from pipeline.solidworks_builder import build_model, export_stl, save_model, SolidWorksError

    if not model.part_name:
        model.part_name = name
    template = resolve_part_template(template_path)

    skipped: list[tuple[str, str, str]] = []
    # Per-feature outcomes -> macro_result.json (feature name -> success/fail),
    # so a failed feature surfaces as ITS name + reason, never a generic exit code.
    feature_results: list[dict] = []
    deferred: list[dict] = []  # Workstream 1: features quarantined + retried
    # Snapshot warnings so we can report only the caveats the BUILD added (e.g. a
    # fillet auto-applied to all edges), separate from extraction/resolver notes.
    pre_warnings = list(getattr(model, "warnings", []) or [])
    try:
        interim_path, sw_doc = build_model(
            sw_app, model, output_dir=part_dir, template_path=template,
            strict=False, skipped_out=skipped, feature_results=feature_results,
            deferred_out=deferred,
        )
    finally:
        try:
            (Path(part_dir) / "macro_result.json").write_text(
                json.dumps({"results": feature_results}, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("Could not write macro_result.json: %s", e)
    build_caveats = [w for w in (getattr(model, "warnings", []) or []) if w not in pre_warnings]
    sldprt = save_model(sw_doc, name, part_dir)
    # Export an STL with the SAME base name as the .sldprt so the web UI's 3D
    # viewer can locate it. Non-fatal — a good .sldprt is not lost to an STL error.
    try:
        export_stl(sw_doc, name, part_dir)
    except SolidWorksError as e:
        log.warning("STL export failed for %s (continuing): %s", name, e)

    vreport = validate_model(sw_doc, model)
    lines = [f"MODEL CHECK — {name}", "=" * 40, f"Saved: {sldprt}", ""]
    for p in vreport.get("passed", []):
        lines.append(f"[PASS] {p}")
    for f in vreport.get("failed", []):
        lines.append(f"[FAIL] {f}")
    for w in vreport.get("warnings", []):
        lines.append(f"[WARN] {w}")
    if skipped:
        lines.append("")
        lines.append("Skipped features (build non-strict — verify manually):")
        for fid, ftype, reason in skipped:
            lines.append(f"  - {fid} ({ftype}): {reason}")
    if build_caveats:
        lines.append("")
        lines.append("Build caveats (applied, but verify against the drawing):")
        for w in build_caveats:
            lines.append(f"  - {w}")
    lines.append("")
    lines.append(f"Overall model validation: {'PASSED' if vreport.get('ok') else 'completed with issues'}.")
    (part_dir / f"{name}_model_check.txt").write_text("\n".join(lines), encoding="utf-8")

    if skipped_out is not None:
        skipped_out.extend(skipped)
    if caveats_out is not None:
        caveats_out.extend(build_caveats)
    if deferred_out is not None:
        deferred_out.extend(deferred)
    # Persist the deferred-retry ledger (Workstream 1) whenever anything was
    # quarantined — recovered or still-open — so the retry story is auditable.
    if deferred:
        try:
            (part_dir / "_deferred_log.json").write_text(
                json.dumps({"total": len(deferred),
                            "recovered": sum(1 for d in deferred if d.get("recovered")),
                            "open": sum(1 for d in deferred if not d.get("recovered")),
                            "items": deferred}, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("Could not write _deferred_log.json: %s", e)

    # Close the document so the .sldprt is not left locked (a locked file breaks
    # re-runs and the Downloads copy). CloseDoc keys on the window title, which can
    # be either the bare title or the file name depending on save state — try every
    # candidate. Under late-bound COM, GetTitle is a PROPERTY (calling it raises
    # "'str' object is not callable"), so read it defensively.
    titles: set[str] = {Path(sldprt).name, Path(sldprt).stem}
    try:
        t = sw_doc.GetTitle
        titles.add(str(t() if callable(t) else t))
    except Exception:
        pass
    for title in titles:
        if not title:
            continue
        try:
            sw_app.CloseDoc(title)
        except Exception:
            pass

    # Remove build-time intermediates so the delivered folder holds exactly one
    # model per part: SolidWorks autosaves, lock files, and the pre-rename save
    # (build_model saves under the drawing's part_name before the final SaveAs).
    final = Path(sldprt).resolve()
    junk: list[Path] = []
    junk += part_dir.glob("AUTOSAVE_*.sldprt")
    junk += part_dir.glob("~$*.sldprt")
    try:
        interim = Path(interim_path).resolve()
        if interim != final:
            junk.append(interim)
            junk.append(interim.with_suffix(".stl"))
    except Exception:
        pass
    for j in junk:
        try:
            if j.exists() and j.resolve() != final:
                j.unlink()
        except OSError:
            pass  # still locked — cosmetic only, never fail the build over it
    return sldprt


@dataclass
class BatchRow:
    source: str
    part: str
    status: str  # READY | NOT READY (gated by final checks) | BLOCKED | ERROR
    macro_readiness: float
    geometry_completeness: float
    dimension_completeness: float
    consistency: float
    feature_confidence: float
    n_macros: int
    n_needs_review: int
    n_skipped: int
    detail: str


def iter_inputs(directory: Path) -> list[tuple[Path, bool]]:
    """Return ``(path, is_json)`` for every processable input in ``directory``.

    ``*_extraction.json`` files are processed as saved extractions; other
    supported drawing files are processed via extraction. Sorted for
    deterministic ordering. A drawing and its own ``*_extraction.json`` can
    coexist; both are listed (the JSON is the free path).
    """
    items: list[tuple[Path, bool]] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        if p.name.endswith("_extraction.json"):
            items.append((p, True))
        elif p.suffix.lower() in DRAWING_EXTS:
            items.append((p, False))
    return items


def _scores(readiness: dict) -> dict:
    keys = ("macro_readiness", "geometry_completeness", "dimension_completeness",
            "consistency", "feature_confidence")
    return {k: float(readiness.get(k, 0.0)) for k in keys}


def process_drawing_data(drawing_data: dict, source: str, output_dir: Path,
                         sw_app=None, template_path: Optional[str] = None,
                         resolve: bool = True, strict_gate: bool = False,
                         overview_image: Optional[Path] = None,
                         overview_analysis: Optional[dict] = None,
                         requirements_file: Optional[Path] = None,
                         skip_overview_check: bool = False,
                         skip_requirements_check: bool = False,
                         human_answers: Optional[dict] = None) -> BatchRow:
    """Verify + (unless blocked) generate macros for one already-loaded extraction.

    By default the Stage 2.5 resolver runs first, so an ambiguous/under-dimensioned
    drawing is resolved to a defensible model rather than blocked ("an incomplete
    model is always the wrong outcome"). Pass ``resolve=False`` for the legacy v2
    behavior and ``strict_gate=True`` to restore the hard BLOCKED gate.

    When ``sw_app`` is provided (a connected SolidWorks application), a real
    ``.sldprt`` is also built into the part folder — making the 3D model a
    standard output of every run, not just the VBA macros.

    ``overview_analysis`` is the Stage 1.5 holistic pass over the FULL drawing
    sheet (run before per-view extraction): it is persisted as
    ``overview_analysis.json`` in the part folder and fed into Stage 2.5 as the
    tier-2 input (cross-view relationships; its conflicts become flags).

    Final checks (both graceful no-ops when their input is absent):
      * ``overview_image`` — the part's overview/full drawing is re-examined
        AFTER the build and diffed against what was captured; a CRITICAL gap
        (feature clearly in the overview, missing from the build) gates the
        status to NOT READY unless ``skip_overview_check``.
      * ``requirements_file`` — the operator's must-meet notes are graded
        against the build; an unmet requirement gates the status to NOT READY
        unless ``skip_requirements_check``.
    The macros/model are ALWAYS still produced — gating changes the status,
    never the outputs.
    """
    # [STAGE] markers: machine-readable progress lines for the web UI's stepper.
    raw_extraction = drawing_data

    # ── Specs-first: the operator's must-meet specifications are read at the
    # START of processing so Stage 2.5 resolution treats them as a first-class
    # input (they are still verified against the final build afterwards).
    spec_lines: list[str] = []
    spec_text = ""
    if not skip_requirements_check and requirements_file is not None:
        try:
            from pipeline.requirements_check import parse_requirements

            spec_text = Path(requirements_file).read_text(encoding="utf-8",
                                                          errors="replace").strip()
            spec_lines = [r["text"] for r in parse_requirements(spec_text)]
            if spec_lines:
                print(f"[SPEC] Enforcing {len(spec_lines)} must-meet specification(s) "
                      "from the start (extraction + resolution + final gate).", flush=True)
        except OSError as e:
            log.warning("Could not read requirements file %s: %s", requirements_file, e)

    # ── Stage 2.6: Spec Reconciliation — the operator's raw must-meet text is
    # parsed into structured MM constraints (priority tier 0: a spec value
    # overrides a vision-extracted value on any conflict; both sides go to the
    # lessons-learned JSONL, never silently discarded).
    mm_app = None
    mm_constraints: list = []
    mm_note = ""
    work_extraction = raw_extraction
    if spec_text:
        from pipeline.must_meet import run_spec_reconciliation

        print("[STAGE] Spec reconciliation", flush=True)
        mm_usage: dict = {}
        mm_app, mm_note = run_spec_reconciliation(
            spec_text, raw_extraction,
            part=(raw_extraction.get("part_number")
                  or raw_extraction.get("part_name") or source),
            lessons_path=output_dir / "lessons_learned.jsonl",
            usage_out=mm_usage,
        )
        mm_constraints = mm_app.constraints
        print(f"[SPEC] Stage 2.6: {mm_note}", flush=True)
        for fl in mm_app.flags:
            print(f"[SPEC] {fl['severity']}: {fl['what']}", flush=True)
        work_extraction = mm_app.extraction
        if mm_usage.get("calls"):
            try:  # the dedicated spec-parse call is paid — put it in the ledger
                import os as _os

                from pipeline.extractor import DEFAULT_MODEL
                from pipeline.usage_log import record_run

                record_run(output_dir, source,
                           _os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL, mm_usage,
                           stage="stage_2_6_spec_reconciliation")
            except Exception:
                pass

    resolution = None
    if resolve:
        from pipeline.resolution_cache import CACHE_DIRNAME, resolve_with_cache

        print("[STAGE] Resolving", flush=True)
        resolution = resolve_with_cache(work_extraction, cache_dir=output_dir / CACHE_DIRNAME,
                                        requirements=spec_lines,
                                        overview_analysis=overview_analysis,
                                        human_answers=human_answers)
        drawing_data = resolution.clean_extraction
        n_overview = sum(1 for f in resolution.flags
                         if f.get("source") == "overview_analysis")
        if n_overview:
            print(f"[OVERVIEW] Stage 1.5 contributed {n_overview} cross-view "
                  "flag(s) to resolution (tier2_overview).", flush=True)
            for f in resolution.flags:
                if f.get("source") == "overview_analysis":
                    print(f"[OVERVIEW] {f['flag_tier']}: {f['human_note']}", flush=True)
        n_spec = sum(1 for r in resolution.dim_resolutions.values()
                     if r.assumption_basis == "spec_driven")
        if n_spec:
            print(f"[SPEC] {n_spec} ambiguous dimension(s) resolved spec-driven "
                  "(operator specification took precedence).", flush=True)
        if mm_app is not None:
            # Every derived value + spec-override conflict lands in
            # resolved_extraction.json with its derivation method.
            resolution.resolved_extraction["must_meet"] = mm_app.as_record()
    elif mm_app is not None:
        drawing_data = work_extraction

    print("[STAGE] Verifying", flush=True)
    model, report = run_verification(drawing_data)
    scores = _scores(report.readiness or {})
    part = model.display_name if model is not None else (
        drawing_data.get("part_number") or drawing_data.get("part_name") or "part"
    )

    # Folder/file base name must be filesystem-safe (an illegible title block can
    # produce quotes or other characters Windows rejects). Same sanitizer as the
    # macro package, so both write into the SAME part folder.
    from pipeline.macro_generator import _safe_name

    safe = _safe_name(part)

    # Always persist the RAW extraction + report (READY or BLOCKED), like the single path.
    part_dir = output_dir / safe
    part_dir.mkdir(parents=True, exist_ok=True)
    (part_dir / f"{safe}_extraction.json").write_text(
        json.dumps(raw_extraction, indent=2), encoding="utf-8"
    )
    if resolution is not None:
        (part_dir / f"{safe}_resolved_extraction.json").write_text(
            json.dumps(resolution.resolved_extraction, indent=2), encoding="utf-8"
        )
    if overview_analysis:
        try:
            from pipeline.overview_analysis import save_overview_analysis

            save_overview_analysis(part_dir, overview_analysis)
        except OSError as e:
            log.warning("Could not persist overview_analysis.json: %s", e)
    verification_text = format_verification_report(model, report)
    (part_dir / f"{safe}_verification_report.txt").write_text(
        verification_text, encoding="utf-8"
    )

    # Persist the operator's authoritative inputs with the run: the raw spec
    # text (must_meet_spec.txt) and the parsed constraints (MM-001..).
    if spec_text:
        try:
            from pipeline.must_meet import MUST_MEET_FILENAME, write_constraints_json

            (part_dir / MUST_MEET_FILENAME).write_text(spec_text + "\n", encoding="utf-8")
            if mm_constraints:
                write_constraints_json(part_dir, mm_constraints, mm_note)
        except OSError as e:
            log.warning("Could not persist must-meet spec files: %s", e)

    # Early compliance pre-check (specs-first): grade the must-meet specs against
    # the resolved extraction IMMEDIATELY after resolution, so an unmet spec
    # surfaces before the build — not only in the final gate (which still
    # re-grades against the final build below and gates READY).
    if spec_lines:
        try:
            from pipeline.requirements_check import check_requirements, parse_requirements

            early = check_requirements(
                parse_requirements("\n".join(spec_lines)), drawing_data)
            n_early_unmet = sum(1 for r in early if r.get("status") == "unmet")
            if n_early_unmet:
                print(f"[SPEC] Early check after resolution: {n_early_unmet} "
                      "specification(s) not yet satisfied by the extracted/resolved "
                      "model — build proceeds; the final gate re-grades against the "
                      "built part.", flush=True)
            else:
                print("[SPEC] Early check after resolution: every checkable "
                      "specification is addressed in the resolved model.", flush=True)
        except Exception as e:  # advisory only — never sink a build over the pre-check
            log.warning("Early requirements pre-check failed (non-fatal): %s", e)

    # Genuinely uncoercible data always blocks; otherwise only --strict-gate blocks.
    if model is None or (not report.ok and strict_gate):
        return BatchRow(source, part, "BLOCKED", **scores, n_macros=0, n_needs_review=0,
                        n_skipped=0, detail="; ".join(report.errors)[:300])
    try:
        print("[STAGE] Building macros", flush=True)
        pkg = generate_macro_package(model, raw_extraction, verification_text, output_dir,
                                     resolution=resolution)
    except MacroGenerationError as e:
        return BatchRow(source, part, "ERROR", **scores, n_macros=0, n_needs_review=0,
                        n_skipped=0, detail=str(e)[:300])
    n_macros = sum(1 for s in pkg.steps if s.macro_file.endswith(".vba"))

    # ── CadQuery pre-validation: build the SAME geometry headlessly from
    # build_plan.json and check it against the MM constraints BEFORE SolidWorks
    # is touched. A failure aborts the SolidWorks build and surfaces the exact
    # constraint id — never a generic pipeline error.
    preval_ok = True
    preval_detail = ""
    preval: dict = {}
    try:
        from pipeline.cq_prevalidate import run_prevalidation, write_prevalidate_script

        print("[STAGE] Pre-validating (CadQuery)", flush=True)
        write_prevalidate_script(part_dir, pkg.build_plan_json.name)
        preval = run_prevalidation(pkg.build_plan_json, mm_constraints, part_dir)
        if preval.get("skipped"):
            print(f"[PREVAL] {preval['skipped']}", flush=True)
        elif preval.get("ok"):
            print("[PREVAL] PASS — watertight pre-validated solid"
                  + (", all must-meet constraints hold" if mm_constraints else "")
                  + " (prevalidation.stl written).", flush=True)
        else:
            preval_ok = False
            fails = (preval.get("failed_constraints")
                     or ([f"pre-validation error: {preval['error']}"]
                         if preval.get("error") else ["pre-validation failed"]))
            for f in fails:
                print(f"[PREVAL] {f}", flush=True)
            preval_detail = "; ".join(fails)[:300]
    except Exception as e:  # the optional pre-check must never sink a run
        log.warning("Pre-validation crashed (non-fatal): %s", e)

    # Build the real .sldprt into the part folder whenever SolidWorks is available,
    # so the 3D model is a required output of every run alongside the text files.
    # A failed pre-validation NEVER blocks the model output ("a complete
    # approximate model is always the correct outcome") — it is noted in the
    # verification report and gates the READY status instead.
    detail = ""
    build_skipped: list = []
    build_caveats: list = []
    if sw_app is not None and not preval_ok:
        print("[PREVAL] Pre-validation flagged issue(s) — the model is still "
              "built and the flags are recorded in the verification report / "
              "Must-Meet checklist for human review.", flush=True)
    build_deferred: list = []
    if sw_app is not None:
        print("[STAGE] Building .sldprt", flush=True)
        try:
            sldprt = build_sldprt_for_part(sw_app, model, part_dir, part_dir.name, template_path,
                                           skipped_out=build_skipped, caveats_out=build_caveats,
                                           deferred_out=build_deferred)
            log.info("Built .sldprt for %s: %s", part, sldprt)
            n_rec = sum(1 for d in build_deferred if d.get("recovered"))
            n_open = sum(1 for d in build_deferred if not d.get("recovered"))
            if build_deferred:
                print(f"[DEFER] {len(build_deferred)} feature(s) deferred: {n_rec} recovered on "
                      f"retry, {n_open} still open (see _deferred_log.json)", flush=True)
        except Exception as e:  # a build failure must not lose the macros/text output
            detail = f"sldprt build failed: {type(e).__name__}: {e}"[:300]
            log.warning("%s: %s", part, detail)

    # ── Stage 10.5: Reconciliation Pass — the self-correcting loop. Before the
    # part is reported done, re-check EVERY feature/hole-instance in the
    # ORIGINAL raw extraction (never the resolved/downstream artifacts, which
    # could themselves hide the bug) against what actually made it into
    # build_plan.json. Any gap gets a bounded, targeted re-resolution attempt
    # (never a paid re-extraction); anything still unresolved after the cap is
    # named explicitly — never silently dropped. See pipeline/reconciliation.py.
    reconciliation_result = None
    if resolution is None:
        log.info("%s: reconciliation skipped (--resolve=False; no Stage 2.5 resolution to "
                 "re-run against).", part)
    else:
        try:
            from pipeline.reconciliation import reconcile_part

            print("[STAGE] Reconciliation (Stage 10.5)", flush=True)
            build_plan_dict = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
            reconciliation_result = reconcile_part(
                raw_extraction=raw_extraction, resolution=resolution, model=model,
                dispositions=pkg.dispositions, build_plan=build_plan_dict,
                verification_text=verification_text, part_dir=part_dir, part=part,
                requirements=spec_lines or None, overview_analysis=overview_analysis,
            )
            rr_path = reconciliation_result.write(part_dir, safe)
            print(f"[RECONCILE] {reconciliation_result.confirmed_built}/"
                  f"{reconciliation_result.checklist_total} checklist items confirmed built "
                  f"after {reconciliation_result.loop_passes_used} pass(es); "
                  f"{len(reconciliation_result.unresolved)} unresolved. Report: {rr_path}",
                  flush=True)
            for u in reconciliation_result.unresolved:
                print(f"[RECONCILE] UNRESOLVED {u.feature_id} ({u.feature_type}): {u.issue}",
                      flush=True)
        except Exception as e:  # the reconciliation pass must never sink an otherwise good run
            log.warning("Reconciliation pass failed (non-fatal) for %s: %s", part, e)

    # ── Human-assist escalation (Task 1/2): AFTER the full automated ladder +
    # the reconciliation/correction loop, any item still unresolved becomes at
    # most `cap` narrow, answerable questions in <Part>_assist_queue.json. This
    # NEVER blocks — a pending question is a flagged assumption whose best-
    # available value still ships; it does not gate READY. Skipped when
    # --no-resolve (no resolution to escalate against).
    assist_queue = None
    if resolution is not None:
        try:
            from pipeline.human_assist import generate_assist_queue, overlay_dispositions

            assist_queue = generate_assist_queue(
                resolution=resolution, part=part, part_dir=part_dir, safe_name=safe,
                model=model, reconciliation_result=reconciliation_result,
                deferred_items=build_deferred,
                lessons_path=output_dir / "lessons_learned.jsonl",
            )
            if assist_queue.pending():
                print(f"[ASSIST] {len(assist_queue.pending())} question(s) need human input "
                      f"(part still shipped its best-available values). "
                      f"See {safe}_assist_queue.json", flush=True)
                for q in assist_queue.pending():
                    print(f"[ASSIST] {q.feature_id}: {q.question_text}", flush=True)
                # Overlay NEEDS_HUMAN_INPUT onto the disposition table (additive).
                overlay_dispositions(part_dir, safe, assist_queue)
        except Exception as e:  # the assist layer must never sink a run
            log.warning("Human-assist escalation failed (non-fatal) for %s: %s", part, e)

    # ── Post-build must-meet verification: measure the REAL SolidWorks STL
    # (trimesh) and grade every MM constraint PASS/FAIL with measured values.
    mm_verification = None
    if mm_constraints:
        built_stl = part_dir / f"{part_dir.name}.stl"
        if built_stl.is_file():
            from pipeline.constraint_verify import verify_constraints_stl
            from pipeline.macro_generator import _model_thickness

            print("[STAGE] Verifying must-meet constraints", flush=True)
            mm_verification = verify_constraints_stl(
                built_stl, mm_constraints, part_dir, part=part,
                expected_thickness_in=_model_thickness(model) or None,
                build_plan_path=pkg.build_plan_json,
                lessons_path=output_dir / "lessons_learned.jsonl",
            )
            for r in mm_verification.get("constraints", []):
                print(f"[MM] {r['id']} {r['status']}: required {r['required']}, "
                      f"measured {r['measured']}", flush=True)
            if mm_verification.get("error"):
                print(f"[MM] verification error: {mm_verification['error']}", flush=True)

    # ── Final checks: overview cross-check + human-requirements compliance ──
    # Both are graceful no-ops (with an explanatory note) when their input is
    # missing, and neither can crash the run. Only a SUCCESSFUL check with a
    # CRITICAL finding / unmet requirement gates the status.
    print("[STAGE] Final checks", flush=True)
    overview_items: list = []
    overview_note = "skipped: --skip-overview-check"
    if not skip_overview_check:
        from pipeline.overview_check import run_overview_check

        usage_o: dict = {}
        overview_items, overview_note = run_overview_check(
            overview_image, drawing_data,
            cache_dir=output_dir / ".extraction_cache", usage_out=usage_o,
        )
        if usage_o.get("calls"):
            try:  # the second vision pass is a paid call — put it in the ledger
                import os as _os

                from pipeline.extractor import DEFAULT_MODEL
                from pipeline.usage_log import record_run

                record_run(output_dir, safe,
                           _os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL, usage_o,
                           stage="final_overview_check")
            except Exception:
                pass
    log.info("%s overview check: %s", part, overview_note)

    reqs: list = []
    req_items: list = []
    req_note = "skipped: --skip-requirements-check"
    if not skip_requirements_check:
        from pipeline.requirements_check import (
            review_items as _req_review_items,
            run_requirements_check,
            write_requirements_json,
        )

        reqs, req_note = run_requirements_check(requirements_file, drawing_data)
        if reqs:
            write_requirements_json(part_dir, safe, reqs)
            req_items = _req_review_items(reqs)
    log.info("%s requirements check: %s", part, req_note)

    # Fold the checks into the human-facing verification report — every skip,
    # fallback, pre-validation flag, and MM PASS/FAIL is NOTED in the txt so
    # nothing about the produced model is silent.
    try:
        _append_final_checks_report(
            part_dir / f"{safe}_verification_report.txt",
            overview_items, overview_note, reqs, req_note,
            preval=preval, mm_verification=mm_verification,
            build_skipped=build_skipped, build_caveats=build_caveats,
        )
    except Exception as e:
        log.warning("Could not append final checks to the verification report: %s", e)

    # Rewrite the engineering review with the .sldprt build outcome AND the
    # final checks folded in, so a COM-skipped feature, an overview gap, or an
    # unmet requirement can never go unflagged (the macro package wrote the
    # pre-build version; this is the complete one). The same final item list is
    # written back into build_plan.json so the web UI's Engineering Flags tab
    # (which reads the build plan) shows the new sections too.
    try:
        from pipeline.engineering_review import SEVERITY_ORDER, build_review_items, write_review

        items = build_review_items(resolution=resolution, pkg=pkg,
                                   build_skipped=build_skipped, build_caveats=build_caveats)
        items.extend(overview_items)
        items.extend(req_items)
        if reconciliation_result is not None:
            for u in reconciliation_result.unresolved:
                items.append({
                    "severity": "CRITICAL",
                    "source": "reconciliation",
                    "id": u.feature_id,
                    "what": f"Stage 10.5 reconciliation: {u.feature_type} {u.feature_id} "
                            f"still unresolved after {reconciliation_result.loop_passes_used} pass(es)",
                    "decision": u.status,
                    "why": u.issue,
                    "affects": u.resolution_attempted or "no re-resolution attempt could make progress",
                })
        rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        items.sort(key=lambda it: rank.get(it.get("severity"), len(SEVERITY_ORDER)))
        write_review(part_dir, part_dir.name, items, resolution=resolution)
        _update_build_plan_review(pkg.build_plan_json, items, overview_note, reqs, req_note)
    except Exception as e:  # the review must never sink an otherwise good run
        log.warning("Could not write engineering review for %s: %s", part, e)

    # Gate: CRITICAL overview gap, unmet human requirement, a failed
    # pre-validation, or a failed post-build MM constraint -> NOT READY
    # (macros + model were still produced; the status is what changes).
    # A run with MM constraints is only SUCCESS when EVERY constraint passes.
    gate_reasons = []
    if any(i.get("severity") == "CRITICAL" for i in overview_items):
        gate_reasons.append("overview verification found CRITICAL gap(s)")
    n_unmet = sum(1 for r in reqs if r.get("status") == "unmet")
    if n_unmet:
        gate_reasons.append(f"{n_unmet} unmet human requirement(s)")
    if not preval_ok:
        gate_reasons.append(preval_detail or "CadQuery pre-validation failed")
    if reconciliation_result is not None and reconciliation_result.unresolved:
        gate_reasons.append(
            f"{len(reconciliation_result.unresolved)} reconciliation item(s) unresolved "
            f"after {reconciliation_result.loop_passes_used} pass(es) — see "
            f"{safe}{RECONCILIATION_REPORT_SUFFIX}")
    if mm_verification is not None and not mm_verification.get("ok"):
        gate_reasons.append("; ".join(
            mm_verification.get("failed_constraints",
                                ["must-meet constraint verification failed"]))[:250])
    status = "READY" if not gate_reasons else "NOT READY"
    if gate_reasons:
        detail = ("; ".join(gate_reasons) + (f"; {detail}" if detail else ""))[:300]

    # Iterative learning loop: capture every failure/flag from this run into the
    # repo's Learning Loop/ folder as a paste-ready brief for a later code-fix
    # pass. Never breaks the run.
    try:
        import os as _os

        from pipeline.extractor import DEFAULT_MODEL
        from pipeline.learning_loop import write_learning_log

        lp = write_learning_log(part_dir, part, status, gate_reasons, output_dir,
                                model=_os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL)
        if lp is not None:
            print(f"[LEARNING] Failure report written to {lp}", flush=True)
    except Exception as e:
        log.warning("Learning loop hook failed (non-fatal): %s", e)

    return BatchRow(source, part, status, **scores, n_macros=n_macros,
                    n_needs_review=len(pkg.needs_review), n_skipped=len(pkg.skipped), detail=detail)


def _append_final_checks_report(report_path: Path, overview_items: list,
                                overview_note: str, reqs: list, req_note: str,
                                preval: Optional[dict] = None,
                                mm_verification: Optional[dict] = None,
                                build_skipped: Optional[list] = None,
                                build_caveats: Optional[list] = None) -> None:
    """Append the Must-Meet, Overview Verification + Requirements sections to
    the part's verification report so the READY/NOT READY story — including
    every skipped feature and fallback — is in one place."""
    lines: list[str] = []

    # ── Must-meet constraint story (pre-validation + post-build) ──
    if preval or mm_verification:
        lines += ["", "", "MUST-MEET CONSTRAINT VERIFICATION", "=" * 33]
    if preval:
        if preval.get("skipped"):
            lines.append(f"Pre-validation (CadQuery): {preval['skipped']}")
        else:
            lines.append("Pre-validation (CadQuery, before the SolidWorks build): "
                         + ("PASS" if preval.get("ok") else "FLAGGED"))
            for r in preval.get("constraints", []):
                lines.append(f"  {r['id']} [{r['status']}] required {r['required']}, "
                             f"measured {r['measured']}")
                if r.get("status") == "FAIL":
                    lines.append(f"      -> {r.get('detail', '')}")
            if preval.get("error"):
                lines.append(f"  (pre-validation error, noted and skipped: {preval['error']})")
    if mm_verification:
        lines.append("Post-build verification (measured from the built STL): "
                     + ("ALL PASS" if mm_verification.get("ok") else "FAILURE(S)"))
        for r in mm_verification.get("constraints", []):
            lines.append(f"  {r['id']} [{r['status']}] required {r['required']}, "
                         f"measured {r['measured']}")
            if r.get("status") == "FAIL":
                lines.append(f"      -> {r.get('detail', '')}")
        if mm_verification.get("error"):
            lines.append(f"  (verification error, noted and skipped: {mm_verification['error']})")

    # ── Build skips / fallbacks: the model was still produced; each item here
    # is a feature that needs a human eye (never a silent drop). ──
    if build_skipped:
        lines += ["", "FEATURES SKIPPED DURING THE BUILD (model still produced)", "-" * 56]
        for fid, ftype, reason in build_skipped:
            lines.append(f"  - {fid} ({ftype}): {reason}")
    if build_caveats:
        lines += ["", "BUILD CAVEATS / FALLBACKS (applied, verify against the drawing)", "-" * 63]
        for w in build_caveats:
            lines.append(f"  - {w}")

    lines += ["", "", "OVERVIEW VERIFICATION", "=" * 21, f"({overview_note})"]
    if overview_items:
        for i, it in enumerate(overview_items, 1):
            lines.append(f"{i}. [{it['severity']}] {it['what']}")
    elif not overview_note.startswith("skipped"):
        lines.append("No gaps: every feature visible in the overview is accounted "
                     "for in the build.")
    lines += ["", "HUMAN-SPECIFIED REQUIREMENTS COMPLIANCE", "=" * 39, f"({req_note})"]
    if reqs:
        lines += ["Specs-first: these specifications were applied DURING extraction and",
                  "Stage 2.5 resolution (injected into the extraction prompt; spec values",
                  "took precedence over ambiguous readings), then verified here against",
                  "the final build — not just checked post-hoc."]
    for r in reqs:
        lines.append(f"{r['id']} [{r.get('status', '?').upper()}] {r['text']}")
        if r.get("note"):
            lines.append(f"      -> {r['note']}")
    n_crit = sum(1 for it in overview_items if it.get("severity") == "CRITICAL")
    n_unmet = sum(1 for r in reqs if r.get("status") == "unmet")
    if n_crit or n_unmet:
        lines += ["", f"RESULT: NOT READY — {n_crit} critical overview gap(s), "
                      f"{n_unmet} unmet requirement(s). Resolve or re-run with "
                      "--skip-overview-check / --skip-requirements-check to override."]
    if report_path.is_file():
        report_path.write_text(
            report_path.read_text(encoding="utf-8") + "\n".join(lines) + "\n",
            encoding="utf-8",
        )


def _update_build_plan_review(build_plan_path: Path, items: list,
                              overview_note: str, reqs: list, req_note: str) -> None:
    """Write the FINAL review items (incl. overview/requirement findings) back
    into build_plan.json — the web UI's Engineering Flags tab reads them there."""
    try:
        plan = json.loads(Path(build_plan_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    plan["engineering_review"] = items
    plan["overview_verification"] = {
        "note": overview_note,
        "n_findings": sum(1 for i in items if i.get("source") == "overview"),
    }
    plan["requirements_compliance"] = {
        "note": req_note,
        "requirements": reqs,
    }
    Path(build_plan_path).write_text(json.dumps(plan, indent=2), encoding="utf-8")


def run_batch(
    directory: Path,
    output_dir: Path,
    extract_fn: Optional[ExtractFn] = None,
    sw_app=None,
    template_path: Optional[str] = None,
    resolve: bool = True,
    strict_gate: bool = False,
    requirements_file: Optional[Path] = None,
    skip_overview_check: bool = False,
    skip_requirements_check: bool = False,
    overview_fn: Optional[Callable[[Path], Optional[dict]]] = None,
) -> tuple[list[BatchRow], Path]:
    """Process every input in ``directory`` and write ``batch_summary.csv``.

    ``extract_fn`` is required only if drawing files (not just ``*_extraction.json``)
    are present; it maps a drawing path to an extraction dict. ``overview_fn``
    (optional) maps a drawing path to its Stage 1.5 holistic overview analysis;
    it runs BEFORE extraction and its result feeds Stage 2.5 (tier 2). When
    ``sw_app`` is given, each READY part is also built into a real ``.sldprt``.
    ``resolve`` / ``strict_gate`` are forwarded to :func:`process_drawing_data`.
    """
    directory = Path(directory)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[BatchRow] = []

    for path, is_json in iter_inputs(directory):
        log.info("batch: processing %s", path.name)
        try:
            overview_analysis = None
            if is_json:
                drawing_data = json.loads(path.read_text(encoding="utf-8"))
            else:
                if extract_fn is None:
                    rows.append(BatchRow(path.name, path.stem, "ERROR", 0, 0, 0, 0, 0,
                                         0, 0, 0, "No extractor provided for a drawing file."))
                    continue
                # Stage 1.5 runs on the FULL sheet BEFORE per-view extraction;
                # a failure only loses the extra signal, never the run.
                if overview_fn is not None:
                    try:
                        overview_analysis = overview_fn(path)
                    except Exception as e:
                        log.warning("batch: overview analysis failed for %s "
                                    "(stage skipped): %s", path.name, e)
                drawing_data = extract_fn(path)
            rows.append(process_drawing_data(drawing_data, path.name, output_dir,
                                             sw_app=sw_app, template_path=template_path,
                                             resolve=resolve, strict_gate=strict_gate,
                                             overview_analysis=overview_analysis,
                                             requirements_file=requirements_file,
                                             skip_overview_check=skip_overview_check,
                                             skip_requirements_check=skip_requirements_check))
        except Exception as e:  # one bad drawing must not sink the whole batch
            log.warning("batch: %s failed: %s", path.name, e)
            rows.append(BatchRow(path.name, path.stem, "ERROR", 0, 0, 0, 0, 0,
                                 0, 0, 0, f"{type(e).__name__}: {e}"[:300]))

    csv_path = write_batch_csv(rows, output_dir)
    log.info("batch: wrote %s (%d rows)", csv_path, len(rows))
    return rows, csv_path


def write_batch_csv(rows: list[BatchRow], output_dir: Path, name: str = "batch_summary.csv") -> Path:
    """Write a list of BatchRow to ``<output_dir>/<name>`` and return the path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / name
    fields = list(asdict(BatchRow("", "", "", 0, 0, 0, 0, 0, 0, 0, 0, "")).keys())
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return csv_path
