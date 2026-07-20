# Change Plan — Reconciliation Loop + Audit Fixes

Ordered by severity: Critical → High → Medium → Low. Each item references its
`AUDIT_REPORT.md` finding.

## Critical

### 1. Add Stage 5 — Reconciliation Pass (new `pipeline/reconciliation.py`)
- **Defect:** no stage re-checks the pipeline's own output against the original
  `_extraction.json` checklist before the part is reported done (Audit: "Reconciliation").
- **Fix:** new module `reconcile_part()` that (a) rebuilds the checklist from the
  raw extraction (every feature id + every hole-callout instance count), (b) diffs
  it against `<Part>_build_dispositions.json` (already-exhaustive per-feature state
  from `build_sequencer.py`) plus a per-hole instance-count check against
  `build_plan.json`'s `positions_xy`, (c) for any gap, re-runs `resolve_extraction`
  on the SAME raw extraction (no paid re-extraction) and re-sequences the build —
  if the gap closes, splices only the affected step(s)/macro file(s) into the
  existing package (nothing else is renumbered/touched); if it doesn't, tries again
  up to a cap of 3 passes; (d) writes `<Part>_reconciliation_report.json` in the
  exact schema requested, with `final_status: READY | READY_WITH_OPEN_ITEMS`.
- **Why it matters:** this is the mechanism that makes "a complete approximate
  model is always the correct outcome" actually *checked*, not just aspired to.
- **Risk if unfixed:** a feature could silently fail to make it from extraction to
  the final model and nothing would ever re-verify that against the source.

### 2. Wire Stage 5 into `pipeline/batch.py::process_drawing_data`
- **Defect:** no call site exists for the new stage.
- **Fix:** call `reconcile_part()` right after the `.sldprt`/build-dispositions are
  known (after `build_sldprt_for_part`, before the final engineering-review
  assembly), fold any unresolved reconciliation items into `gate_reasons` (adds
  `"N reconciliation item(s) unresolved after M pass(es)"` — this flips the
  EXISTING binary `BatchRow.status` to `NOT READY`, so exit codes (`main.py`'s
  `0 if n_ready == len(rows) else 8`) and the webapp's READY/NOT-READY banner
  regex continue to work with **zero changes** and zero regression risk). The
  richer `READY_WITH_OPEN_ITEMS` distinction lives inside
  `_reconciliation_report.json`'s own `final_status` field, exactly where the task
  spec's JSON schema puts it — not as a new top-level pipeline status enum, which
  would silently break the webapp's `/\bNOT READY\b/` / `\d+\/\d+\s+READY` regex
  matching (verified by reading `webapp/index.html:1541-1556`).
- **Why:** keeps the existing, working READY/NOT READY/exit-code/webapp-banner
  contract intact while still surfacing the nuance the task asks for.
- **Risk if unfixed:** the new stage runs but nothing downstream (CLI exit code,
  webapp banner) ever reflects an unresolved reconciliation finding.

## High

### 3. Fix silent fillet/chamfer drop — `pipeline/macro_generator.py:1123-1220` (`_macro_fillet_chamfer`)
- **Defect:** when both the feature-level and model-wide radius/distance fallback
  are exhausted, the feature is `continue`'d with only a VBA comment — never
  recorded in `pkg.skipped`/`build_plan.json` (Audit finding #1, High).
- **Fix:** change `_macro_fillet_chamfer`'s return type to also yield a
  `skipped: list[tuple[feature_id, reason]]`; the caller (`generate_macro_package`)
  appends a `BuildStep(..., "skipped_prohibited", ...)` for each and includes it in
  `pkg.skipped` — so it surfaces in `build_plan.json`, the engineering review, AND
  is visible to the new Stage 5 checklist walk.
- **Why:** closes the one confirmed, reachable silent-drop path in the whole
  audit; also removes a blind spot Stage 5 would otherwise need to special-case.
- **Risk if unfixed:** a fillet/chamfer with a genuinely unrecoverable
  radius/distance disappears from every JSON artifact a human or Stage 5 would
  check, surviving only as a VBA source comment.

### 4. Harden the `feature is None` drop — `pipeline/macro_generator.py:2072-2073`
- **Defect:** relies entirely on an unstated upstream guarantee ("validator
  already flagged it") with no local defense (Audit finding #2, Medium — bumped to
  High here because it's a one-line, zero-risk fix that removes a latent trap).
- **Fix:** log a warning and append a `BuildStep` with status `"skipped_prohibited"`
  / reason `"build_order referenced unknown feature id"` instead of a bare `continue`.
- **Why:** defense-in-depth — currently unreachable, but "currently unreachable"
  is not the same guarantee as "structurally impossible," and the fix is free.
- **Risk if unfixed:** a future change to `build_sequencer.py` (or a hand-edited
  `build_plan.json`) that puts a bad id into `build_order` would silently drop a
  feature with zero trace.

### 5. Fix CLAUDE.md region-markup drift
- **Defect:** CLAUDE.md documents an entire markup subsystem that does not exist
  in the code (Audit finding, High-severity documentation drift).
- **Fix:** remove/rewrite the stale paragraph to describe the actual current Sheet-1
  flow (upload the full overview sheet; no human crop/markup preprocessing), per
  the on-page copy in `webapp/index.html:547-549`.
- **Why:** "this file is the project's source of truth and must never drift from
  the real behavior again" — explicit task requirement.
- **Risk if unfixed:** every future session (including this one, had it not been
  caught) wastes time or makes wrong assumptions building on a feature that isn't
  there.

## Medium

### 6. Reconciliation report token-ledger discipline
- **Defect:** none yet — preventative. The task requires that targeted
  re-resolution never force a fresh paid re-extraction, and any scoped re-query
  must be logged.
- **Fix:** `reconcile_part()` only ever calls `resolve_extraction()` (pure Python,
  already-extracted data, no API call) for its re-resolution attempts; it never
  calls the extractor. This is asserted in the module docstring and covered by a
  test that patches `pipeline.extractor.extract_drawing_data*` to fail loudly if
  invoked during reconciliation.
- **Why:** preserves the `--from-json` zero-cost path and the extraction cache.
- **Risk if unfixed:** N/A (preventative; enforced by design + test).

## Low

### 7. Test coverage for the reconciliation module
- **Fix:** `tests/test_reconciliation.py` — checklist-vs-disposition diffing,
  hole-instance-count mismatch detection, the capped-loop termination + report
  schema, and the "no paid re-extraction" guarantee.
- **Why:** the reconciliation module is new, non-trivial control flow (a bounded
  loop with a splice-back side effect) that needs direct unit coverage independent
  of a live SolidWorks run.
