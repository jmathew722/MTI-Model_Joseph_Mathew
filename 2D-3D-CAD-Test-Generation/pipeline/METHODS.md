# Construction Method Library (Phase D)

Living record of the **verified** construction recipe for each feature class —
which SolidWorks / CadQuery method actually produces geometry that passes Phase A
per-feature verification (`pipeline/feature_verify.py`). This is the human,
evidence-backed half; `pipeline/methods_config.py` is the machine-readable
dispatch config the pipeline reads (defaults mirror the winners below; override
via `methods.json` or `MTI_METHOD_<CLASS>`). New findings are produced by
`pipeline/construction_experiment.py` — a scratch base+feature is built with each
candidate method and Phase-A-verified; the winner is recorded here and can be
promoted into `methods.json`.

A recipe is only listed as **verified** with concrete evidence: the part (or
scratch experiment), the date, and the measured result.

---

## Holes (drilled / cbore / csk / tapped)

**Verified method: `sketch_circle_cut`** — sketch a circle at each resolved
(x, y) on the target face and `FeatureCut4` through-all/blind; a counterbore adds
a second concentric blind cut; a tapped hole drills the tap-drill diameter and
leaves the thread cosmetic (real helical threads are prohibited). CadQuery mirror:
`faces(">Z").workplane().pushPoints([...]).hole(d)` / `.cboreHole()` / `.cskHole()`.

Evidence:
- **A001341E (157-C)** live SolidWorks 2024 build, 2026-07-10: all four Ø0.218
  THRU holes verified `OK` — position within ~0.004", diameter 0.2174 vs 0.218,
  through=true (both faces). `feature_verify` against the built `.STL`.
- Scratch hole experiment (`run_hole_experiment`), CadQuery, 2026-07-10:
  `sketch_circle_cut` built and verified `OK` (winner).

**`hole_wizard5` — available, OPT-IN, NOT default.** Real Hole Wizard features
carry tap/cbore/csk semantics and avoid the open-sketch failure class, but on
this machine's SolidWorks 2024 `IFeatureManager::HoleWizard5` returned `None`
(no geometry) even on a clean part with a valid face + point sketch — the
Value-slot/standard mapping is version/locale specific (see
`docs/build-order-redesign-2026-07-10.md`). Enable with `MTI_ENABLE_HOLE_WIZARD=1`
to iterate; promote to default here once it passes Phase A across the golden set.

---

## Slots / U-notches (CANONICAL: rectangle + corner fillets)

**Verified method: the two-step slot decomposition (`pipeline/slot_cut.py`).**
A U-shaped cutout / open notch / keyway / slot is NEVER built as a single
arc-bearing sketch. It decomposes into exactly two ordered, adjacent steps:

  A. **`slot_rect_cut`** — a rectangular through-cut from the single corner
     array (`corner_array`), at the dimensioned position. Mandatory
     (`must_complete`): 4 lines + a cut is near-unfailable and carries the
     slot's position + size truth.
  B. **`slot_corner_fillet`** — constant-radius fillets on the rectangle's
     INTERIOR corners (2 for an open notch, 4 for a closed slot). Deferred-safe:
     a fillet failure never destroys the already-correct rectangle. Each arc
     centre sits exactly `(r, r)` inset from the sharp corner along both walls
     (a filleted corner, by construction) — proven by
     `slot_cut.arc_centers` / `rounded_profile_from_corners` and asserted in
     `tests/test_slot_cut.py`.

The corner array is the ONE source of truth both steps derive from, so the
fillet can never target a location other than the rectangle that was cut. All
three builders now build this SAME shape (2026-07-21): the VBA macro
(`_macro_slot_rect` + `_macro_slot_fillet`), the SolidWorks COM path
(`build_slot`), and the CadQuery pre-validator (which cuts the exact rounded
profile from `rounded_profile_from_corners` in one shot). Edge selection for the
corner fillets is restricted to VERTICAL through-thickness edges (an orientation
filter), so a stray horizontal edge near the corner is never mis-selected.

**`slot2D` / native obround is used ONLY for a true obround** — a `closed_slot`
whose `2·corner_radius == width` (full-radius ends), reclassified as
`slot_kind == "obround"` by `validate_slot`. For any slot with a corner radius
SMALLER than half the width (the common U-notch / keyway), the obround is the
WRONG shape (it would round the entire end), so the rectangle+fillet
decomposition is used. This supersedes the earlier "straight obround is the
verified method" note — that guidance was correct only for the obround special
case.

Evidence:
- `pipeline/slot_cut.py` corner-array + `(r, r)` inset math, unit-tested in
  `tests/test_slot_cut.py` (corner placement, near-edge/centerline offset,
  `2R ≤ width` / `R ≤ depth` gates, and the arc-centre inset).
- 16247 (two left-edge U-notches, `.531 R TYP`) builds both notches as
  rectangle+corner-fillet across all three paths; the second notch, extracted as
  a linear pattern of the first, is expanded into an explicit second slot
  (`expand_slot_patterns`) because a U-notch decomposition cannot be reliably
  feature-patterned.
- Phase A recognises a slot's boundary as the cut's own footprint (not a phantom
  hole), so a correctly-built slot verifies clean.

---

## Profile cuts / notches / steps (correct location)

**Verified method: `sketch_rect_cut` with ORIGIN-ANCHORED coordinates.** Each cut
sketch is dimensioned to the part origin (lower-left corner), never chained to a
prior sketch entity, so a single upstream position correction never cascades into
neighbouring features. The location guarantee is layered: intersection pre-check
before the cut call (`_assert_cut_intersects_body`), CadQuery pre-validation of
the cut's local geometry, and now Phase A post-build cross-section confirmation
(material-absence probe) with `MISPLACED` feeding the correction loop.

Evidence:
- **A001341E (157-C)** live build, 2026-07-10: the Ø3.062 semicircular top-edge
  cutout verified `OK` — material fraction inside the probe = 0.0 (material
  correctly removed), plate material present adjacent. `feature_verify`.

---

## How the loop uses this library

`reconciliation.geometric_correction_loop` (Phase B) builds → measures (Phase A)
→ corrects → rebuilds, capped at 3 iterations. When a feature class fails twice
on the same part, run `construction_experiment` for that class instead of a third
blind retry, record the winner here + in `methods.json`, and the next build
dispatches the proven method. Every loop iteration appends a
`geometric_loop_iteration` entry to `lessons_learned.jsonl` — the raw material
for future entries in this file.
