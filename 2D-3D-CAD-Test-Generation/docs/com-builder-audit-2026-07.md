# COM builder cross-reference audit (2026-07-20)

Audit of `pipeline/solidworks_builder.py` (the direct-COM `.sldprt` builder that
runs under **both** `--engine com` and `--engine vba`) against the documented
VBA-generator error history (error classes E001–E011).

## Ground-truth note (read first)

The task brief referenced `docs/solidworks-macro-error-log.md` with 11 numbered
entries. **That markdown file does not exist in this repo** — the error history
survives only as (a) code comments citing `E0xx`, and (b) the enforceable rules in
`pipeline/macro_audit.py::BANNED_APIS` (E004 `GetModelBoundingBox`, E006
sketch-by-name). This audit therefore cross-references the builder against the
error *classes as described in the task brief and as encoded in the source*, not
against a nonexistent document. A fresh `docs/solidworks-macro-error-log.md` is
created as part of this pass (see Step 5) to give the history a home and to record
the promotion decision.

Findings legend: **PASS** = the builder already handles this class; **GAP** = the
builder is missing the fix and it is corrected in this pass (new entry Exxx);
**N/A** = not applicable to the builder.

---

## E001 — missing default part template → **GAP (fixed: E012)**

`create_new_part()` resolves the template only from (1) `SOLIDWORKS_TEMPLATE_PATH`
and (2) `GetUserPreferenceStringValue(swDefaultTemplatePart)`. If a machine has
neither the env var nor a configured default preference, the build dies with
"Part template not found" even when a stock `Part.prtdot` exists on disk. There is
**no filesystem-discovery fallback**. Fixed by adding `_discover_part_template()`
which globs the standard SOLIDWORKS `templates/` locations for `Part*.prtdot`.

## E002 — plane selection by hard-coded (localized) name → **GAP (fixed: E013)**

`_select_plane()` calls `SelectByID2("Front Plane", "PLANE", …)` by name only. On a
non-English SOLIDWORKS install the reference planes are localized ("Plano alzado",
etc.) and the name match fails. There is **no index-in-tree fallback**. Fixed by
adding `_select_plane_by_index()` — enumerate `RefPlane` features in tree order and
select the 1st/2nd/3rd (Front/Top/Right) when the name lookup returns false.

## E003 — mixed coordinate frames / notch orientation → **GAP (fixed: E014)**

`solidworks_builder.py` **never imports `pipeline/coordinate_normalize.py`**, while
`macro_generator.py` does. Concretely: a canonical **slot / open-edge notch** is
represented by a `SlotCut` record (geometry on `width`/`depth`/`corner_radius`,
positioned via `slot_cut.corner_array()` → `coordinate_normalize.resolve_notch_anchor`).
The COM builder has **no slot handling at all** — a slot-backed `extrude_cut`
feature reaches `build_extrude_cut`, which looks for two in-plane sides in
`related_dimensions`, finds none (the sizes live on the slot record), and either
raises or mis-places the cut. Result: under `--engine com` a top-edge notch is
missing or lands on the wrong edge — the exact 158-C class the coordinate
normalizer was built to prevent.

Fixed by `build_slot_cut()`: when `model.slot_cut_for_feature(feature.id)` returns
a record, the rectangle is built from `corner_array(slot, model)` (which routes the
`y = parent_height − depth` math through `coordinate_normalize`), cut through-all,
and its interior corners filleted (fragile/deferred-safe). `build_extrude_cut`
dispatches to it, so both engines now place notches identically.

## E004 — invented API (`GetModelBoundingBox`) → **PASS**

`grep GetModelBoundingBox pipeline/solidworks_builder.py` → 0 hits. The builder
reads the box from the solid body via `IBody2.GetBodyBox` (real API). To make this
*enforced* rather than incidental, Step 4 adds `pipeline/com_builder_audit.py`, a
static source auditor mirroring `macro_audit.py` that fails on any banned/invented
COM name in the builder's own source.

## E005 / E006 — sketch reselection anti-pattern → **PASS**

The builder follows the recorder pattern the fix prescribes: draw into the active
sketch → `InsertSketch(True)` to close → call the feature method
(`FeatureCut4`/`FeatureExtrusion3`/`FeatureRevolve2`) on the just-closed sketch
(`_circular_cut_at`, `build_extrude_boss`, `build_extrude_cut`, `build_revolve`). It
**never** re-selects a closed sketch by name (`SelectByID2(…, "SKETCH")`). Confirmed
by inspection; the Step-4 auditor also flags the E006 name-reselect pattern.

## E007 — redundant pattern manual work → **PASS**

`build_pattern()` calls `pipeline.macro_generator._pattern_covered_by(model, feature)`
— the **same** no-op detector the VBA generator uses — so a seed hole that already
placed all instances is a shared no-op, not a divergent reimplementation.

## E008 — (not described in the brief) → **N/A**

No E008 class was described; no corresponding builder concern identified.

## E009 — extraction / dimension-chain bug → **N/A**

Extraction/resolver stage, out of scope for the builder (brief says skip).

## E010 — `applies_to` label normalization → **PASS**

`get_dimensions_for_feature()` keys resolved dims by `dim.canonical_applies_to`,
the `pipeline.schema.Dimension` property backed by
`pipeline.schema.canonicalize_applies_to()` — the **same** canonicalizer the VBA
path uses. No separate matching path that could drift.

## E011 — resolver annotations / build status surfacing → **PASS (with note)**

`build_model()` records every feature outcome to `feature_results` (written as
`macro_result.json`) and appends caveats to `model.warnings`; `main.py`/`batch.py`
regenerate the engineering review **after** the COM build so a LOW/CRITICAL-tier
assumption is surfaced as a review item, never silently built. The build consumes
the resolver's `resolved_value`s through the same `model` object the macro package
uses (no second resolution path). Adequate; a per-feature assumption log line is a
possible future nicety but not a correctness gap.

---

## Summary

| Class | Verdict | Action |
| --- | --- | --- |
| E001 template discovery | GAP | fixed (E012) + test |
| E002 plane index fallback | GAP | fixed (E013) + test |
| E003 coordinate/slot handling | GAP | fixed (E014) + test |
| E004 invented API | PASS | enforced by new static audit |
| E005/E006 sketch reselect | PASS | enforced by new static audit |
| E007 redundant pattern | PASS | shared logic confirmed |
| E010 applies_to canon | PASS | shared canonicalizer confirmed |
| E011 flag surfacing | PASS | surfaced via review, not silent |

Three real gaps (E001/E002/E003) were found and fixed; the remainder were already
correct in the COM path. The centralized VARIANT marshalling (`com_marshal.py`),
expanded `test_com_builder.py`, and the static `com_builder_audit.py` make the
passing classes *enforced* going forward.
