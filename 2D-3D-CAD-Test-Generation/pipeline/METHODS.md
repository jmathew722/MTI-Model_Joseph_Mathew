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

## Slots (straight obround)

**Verified method: `slot2d` (CadQuery) / `create_sketch_slot` (SolidWorks).**
One primitive yields a closed, fully-defined obround (two arcs of radius = width/2
+ parallel flats) — no hand-drawn capsule profile. Build-plan steps carry
`profile: "slot"`; the CadQuery pre-validator emits
`center(cx, cy).slot2D(length, width, 0).cutThruAll()`. The SolidWorks path is
`ISketchManager::CreateSketchSlot` (straight-slot type, endpoints from the
resolved center/length/orientation in the origin frame), verified fully-defined
before cutting.

Evidence:
- Scratch slot experiment (`run_slot_experiment`), CadQuery, 2026-07-10:
  `slot2d` built a valid watertight obround and verified `OK` (winner). The
  alternative `capsule_profile` (two circles + rect) also verified `OK` but is
  more fragile (three operations, trim seams) — kept only as an experiment
  comparand.
- Phase A recognises a slot's obround boundary as the cut's own footprint (not a
  phantom hole), so a correctly-built slot verifies clean.

> Wiring note: slot *detection* (drawing callout → a `profile:"slot"` cut step)
> is not yet emitted by extraction/resolution for every slot; the construction +
> verification methods above are proven and ready for when it is. `A001551E`
> (per the acceptance set) is the intended first production slot.

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
