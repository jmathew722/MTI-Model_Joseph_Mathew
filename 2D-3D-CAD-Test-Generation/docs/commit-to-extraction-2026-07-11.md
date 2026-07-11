# Commit-to-Extraction Mode — 2026-07-11

No human in the loop: the pipeline commits to the extraction, derives everything
derivable, and **builds every extracted feature** with flags for anything
inferred — never exclusion, never review-routing as a terminal state, never
placeholder `[0,0]` coordinates.

## Root-cause reads (evidenced in real artifacts)

**Bug 1 — extracted positions dropped by review-routing (158-C, F002).**
`158-C_extraction.json` carries `D002 = 1.56` (`applies_to: "slot_offset"`) and a
`slot_cuts[F002]` record with `anchor_offset: 1.56`, yet
`158-C_build_dispositions.json` records `position_xy: [0.0, 0.0]` /
`derivation_source: "position:needs_markup_review"`. Root cause:
`canonicalize_applies_to("slot_offset" | "hole_position_x" | "position")`
returns `""` — positional dimensions produce **no** canonical token, so the
resolver's position logic never consumes them and escalates to
`needs_markup_review` even though the location was fully extracted.

**Bug 2 — Y-axis/edge inversion (158-C notch on bottom instead of top).**
Image space is top-left origin (+Y down); the model frame is bottom-left origin
(+Y up). An edge-anchored notch "into the top edge" must anchor at
`y = part_height − depth`, not `y = 0`. The canonical `slot_cut.corner_array`
now owns this per `open_edge`; the audit adds a targeted opposite-ends test.

**M_121-B — both step cuts excluded.** F002 (step, x=4.38, height=4.5) and F003
(step, width=1.88) were `EXCLUDED_INCOMPLETE` for "missing length+width". A step
in a fully-dimensioned outer profile is determined by the profile's dimension
chain, not feature-local length+width callouts. F005 (2nd of "(2) HOLES" .422)
was excluded for a missing diameter its sibling F004 carries.

## What changed (`commit_mode`, default ON)

1. **Positional-dimension consumption (`_feature_positional_xy`).** Positional
   dims are detected by their RAW `applies_to` (contains `position`/`offset`/
   `slot_offset`/`_x`/`_y`), independent of the empty canonical token, plus the
   `slot_cuts` anchor. A feature with any positional evidence is resolved
   *before* any escalation runs.

2. **Bug-1 invariant (generation-time, `macro_generator`).** The generator
   REFUSES (`MacroGenerationError`) to emit a build for any feature whose
   disposition says the position is unresolved (`needs_markup_review`) while a
   positional dimension for that feature exists — that combination is a bug and
   crashes loudly in tests instead of shipping a wrong part.

3. **`_feature_xy` (build_sequencer)** is slot- and positional-dim-aware, so a
   disposition never records `[0,0]` for a feature whose location is known.

4. **Profile-delta derivation (`_derive_profile_delta`).** A step/notch
   `extrude_cut` missing length/width has its rectangle derived from the outer
   envelope minus its partial anchor dims, tagged `profile_delta`, flagged
   derived — and built.

5. **Sibling-diameter inheritance (`_sibling_diameter`).** A hole missing a
   diameter inherits the most common diameter among the part's other holes
   before any standard-size fallback.

6. **Commit-mode policy (`_completeness_gate`, `_resolve_feature`).**
   `EXCLUDED_INCOMPLETE` and `needs_markup_review` are no longer terminal for
   buildable geometry. The ladder runs to the end: extracted → chain →
   profile-delta → TYP → sibling/standard-size → and finally a declared-basis
   **conservative committed value** (applied and built, CRITICAL flag naming the
   value, the basis, and every empty rung). Placeholder `[0,0]` positions are
   banned; an undimensioned location commits to a conservative inside-parent
   placement, recorded with its basis. The only remaining hard failure is the
   existing one: no closed outer profile. The assist queue may still log a
   question but gates nothing.

Post-build rigor is unchanged: committed/derived features are measured by
`feature_verify.py` and the Phase-B correction loop remains their safety net.
`commit_mode=False` restores the old exclude/review behavior for comparison.
