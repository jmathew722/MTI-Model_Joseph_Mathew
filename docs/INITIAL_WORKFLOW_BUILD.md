# Initial Build Workflow — MTI 2D→3D Pipeline

This document captures the full end-to-end pipeline **after** Stage 1.5 (Holistic
Overview Analysis) and Stage 2 (Vision Extraction): how every dimension is
resolved to a number, how an ordered build plan is frozen, and how each feature
is analyzed and built to produce a model. It is the "initial workflow build"
reference snapshot.

Mental model:

> **resolve every number → freeze an ordered plan → construct + verify the solid**

Guiding invariant throughout: *a complete approximate model is the correct
outcome; an incomplete model is always wrong.* The system resolves and flags —
it never blocks or silently drops a feature.

---

## Phase A — Turn a messy extraction into unambiguous numbers

Starting point: raw `_extraction.json` — a list of `Feature` objects (each with a
`type`, `sketch_plane`, `parent_feature`, dimensions), hole callouts, and a
top-level `build_order` of feature IDs. Several dimensions are still ambiguous.
Four stages clean this up, in strict priority order.

### Stage 3 — Vector hole positions
`pipeline/hole_resolution.py` + `pipeline/vector_extract/`

Re-reads the original vector file for exact hole positions (DXF/DWG entities via
ezdxf, vector-PDF Bézier circles via PyMuPDF, HoughCircles raster fallback).

**Precedence rule: vector geometry owns position; the vision callout owns
semantics (diameter/thread/depth).** On disagreement, both are kept and the hole
is flagged CRITICAL. Each hole carries `position_source` and
`position_confidence`.

### Stage 2.6 — Spec reconciliation
`pipeline/must_meet.py`

The operator's must-meet text is parsed into structured `MM-001…` constraints.
These are **priority tier 0** — they override vision-extracted values on any
conflict, and every override is logged to `lessons_learned.jsonl` (never silently
dropped). Missing geometry (e.g. a bolt-circle radius) is *derived* from the
extraction, never invented.

### Stage 2.5 — The resolver (core design decision)
`pipeline/resolver.py`

Where "never block on ambiguity" lives. Every ambiguous dimension is forced to a
single numeric `resolved_value`, **chosen only from extracted candidates — never
fabricated** (`_candidates()`). Decision tree, in order:

1. **Spec-driven** (Step 0) — an operator spec value that clarifies the reading
   wins (`assumption_basis="spec_driven"`).
2. **Arithmetic chain** — value forced by a dimension chain that must sum.
3. **Geometric validity** — `_passes_geometry()` rejects candidates that make the
   part invalid (hole bigger than the plate, negative depth, …).
4. **Conservative geometry** — pick the reading that removes least material / stays
   inside the envelope.
5. **Last-resort default** — a small tolerance-based value, flagged CRITICAL.

Every choice is tagged HIGH/MEDIUM/LOW/CRITICAL with a human note. Output:
`_resolved_extraction.json` — the single source of truth downstream. Stage 1.5
overview relationships feed in here as **tier 2** (cross-view correspondence,
e.g. through-vs-blind).

### Stage 6 — Validator
`pipeline/validator.py` — arithmetic/envelope sanity check. Advisory by default;
`--strict-gate` makes it blocking.

---

## Phase B — Freeze an ordered, self-contained build plan

`pipeline/macro_generator.py :: generate_macro_package()` walks
`model.build_order` and emits both numbered VBA macros **and** `_build_plan.json`
(the ordered, fully-numeric recipe).

```
00_setup                 → new part, set units, save-as
<in build_order>:
   feature 1 (must be the base EXTRUDE_BOSS / REVOLVE)
   feature 2 …
   fillets & chamfers    → DEFERRED to the very end
ZZZ_export_stl
RUN_ALL.vba              → runs every numbered sub in sequence
```

Ordering rules baked in:

- **Base first.** The first feature creates the solid body (rectangular/circular
  extruded boss, or a revolve). The base's lower-left corner sits at the sketch
  origin (+X right, +Y up), so hole positions dimensioned from part edges become
  sketch coordinates directly.
- **Cuts/holes after the base**, in `build_order`.
- **Cosmetic edge features (fillet/chamfer) last** — fragile; deferred so they
  don't break references mid-build.
- **Prohibited/unsupported types are never silently dropped.** SHELL and anything
  outside the supported set become a numbered `NN_Fxxx_MANUAL_*.vba` step with a
  MsgBox + extracted values, recorded for the engineering review.
- **Circular hole patterns get a special 3-macro trio** (when a concentric bore
  exists to derive the axis): seed hole → named reference axis (`InsertAxis2` from
  the bore's cylindrical face) → the pattern via the version-pinned
  `CreateCircularPatternSafe` helper. `total_instances` includes the seed.

Each `_build_plan.json` step is **self-contained**: dimensions in drawing units
and meters, `sketch_plane`, `parent_feature_id`, `depth_type`
(through_all/blind), `positions_xy` (+ meters), `position_source`, and flags.
Generation *refuses* if any canonical field is null.

Every macro is **statically audited** before it is written
(`pipeline/macro_audit.py`) — banned APIs fail generation.

---

## Phase C — Build the solid, then verify it

### Stage 8 — CadQuery pre-validation
`pipeline/cq_prevalidate.py`

Before touching SolidWorks, the *same* geometry is built headlessly from
`build_plan.json` (circular patterns via `.polarArray()` + `cutThruAll`). Checks
watertightness, volume, and hole counts against MM constraints. **A failed check
aborts the SolidWorks build** and names the exact constraint (`MM-001 FAILED: …`).
No-op if cadquery isn't installed.

### Stage 9 — The COM build
`pipeline/solidworks_builder.py :: build_model()` (Windows-only)

Loops `model.build_order` and dispatches each feature by type
(`_FEATURE_DISPATCH`):

| Feature type | Builder | What it does |
|---|---|---|
| `extrude_boss` | `build_extrude_boss` | select plane → sketch rect/circle → verify fully defined → extrude |
| `extrude_cut` | `build_extrude_cut` | sketch on face → cut (blind/through, side-aware) |
| `hole` | `build_hole` → maybe `build_circular_pattern_holes` | draw circle(s) at positions → cut; pattern builds seed+axis+pattern |
| `revolve` | `build_revolve` | half-profile polygon about an axis |
| `mirror` | `build_mirror` | mirror `parent_feature` across a plane (fragile) |
| `fillet` / `chamfer` | `build_fillet` / `build_chamfer` | edge treatment (fragile) |
| `pattern` | dispatched | linear/circular replication |

Robustness behaviors in the loop:

- **Sketches verified fully-defined before extruding** (`_verify_sketch_fully_defined`)
  — tries to add relations; refuses if over-defined.
- **Each feature renamed deterministically right after creation**
  (`<featureId>_<type>`) so a failure surfaces as the exact feature.
- **Rebuild errors checked after every feature.**
- **Graded failure handling:** fillet/chamfer/mirror are "fragile" → demoted to a
  warning and skipped, build continues. Non-fragile failures in strict mode save a
  `PARTIAL_<featureId>` model and abort with the exact feature name. Every outcome
  (PASS/FAIL/no-op) is written to `macro_result.json`.
- A completed run **with no solid body is still a failure**.

Then STL export + mass/bbox check (`pipeline/model_validator.py`).

### Stage 10 — Post-build constraint verification
`pipeline/constraint_verify.py`

The *built STL* is measured with trimesh (cross-section circle fitting;
through-all = hole near both faces). Every MM constraint is graded PASS/FAIL with
measured-vs-required into `constraint_verification.json`. **A part with MM
constraints is only READY when all pass**; failures append to
`lessons_learned.jsonl` with the responsible VBA snippet.

### Stage 11 — READY gates (status only; outputs still produced)

- `pipeline/overview_check.py` — re-examines the part's overview drawing alone and
  diffs it against the build; a missing visible feature = CRITICAL.
- `pipeline/requirements_check.py` — grades operator must-meet notes
  met/partial/unmet; any unmet line gates READY.

### Stage 12 — Engineering review
`pipeline/engineering_review.py` — the single severity-ranked human report,
regenerated *after* the COM build so skipped/manual features are included. Read
this first.

### Stage 13 — Learning loop
`pipeline/learning_loop.py` — writes one plain-text failure report per run to
`Learning Loop/` with a paste-ready "FIXES FOR FABLE" brief, so the pipeline
improves run-over-run.

---

## One-line summary

resolve every number (tier 0 spec → tier 1 extraction → tier 2 overview) →
freeze an ordered plan (base first, cuts next, edges last, prohibited→manual) →
pre-check in CadQuery → build in SolidWorks feature-by-feature with fully-defined
sketches and graded failure → measure the STL and grade every constraint → gate
READY but always deliver a model.
