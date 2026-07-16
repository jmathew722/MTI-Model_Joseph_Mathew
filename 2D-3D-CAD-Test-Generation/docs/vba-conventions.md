# SolidWorks 2024 VBA macro conventions (the spec Codex Sol writes against)

This is the **stable, authoritative contract** for the VBA macro set generated
from a validated build JSON. Codex (Stage B) must follow it exactly; the
deterministic fallback generator (`pipeline/macro_generator.py`) already does.
Extracted from the shipped generator + templates so both writers agree.

> Guiding principle: *a complete approximate model is always the correct outcome;
> an incomplete model is always the wrong outcome.* Never silently drop a
> build-plan step — build it, or emit a numbered `MANUAL` macro that names what a
> human must do.

## 1. File set, naming, order

One macro file per build step, zero-padded and ordered so lexical sort == build
order:

```
macros/
  00_setup.vba              # new part from template, set units, save-as <part>.sldprt
  01_<F001_id>.vba          # base solid (largest closed profile → FeatureExtrusion3)
  02_<F002_id>.vba          # additive bosses, profile cuts, holes …
  NN_<Fxxx>_SeedHoleCut.vba # circular-pattern trio (seed → axis → pattern)
  NN_<Fxxx>_reference_axis.vba
  NN_<Fxxx>_circular_pattern.vba
  NN_fillets_chamfers.vba   # chamfers THEN fillets, last geometry
  ZZ_final_verify.vba       # ForceRebuild3 + mass props + bbox vs envelope
  ZZZ_export_stl.vba        # export <part>.stl next to the .sldprt (sorts last)
  RUN_ALL.vba               # one macro that runs every step in order
  codex_manifest.json       # written by Stage B (files, coverage, assumptions)
```

Seven-stage build order (from the build sequencer): `0 reference · 1 base solid ·
2 additive bosses · 3 profile subtractions · 4 holes (plain → cbore/csk →
tapped) · 5 patterns · 6 chamfers-then-fillets · 7 non-geometric`.

## 2. Header / footer / units / logging

Every macro shares one skeleton:

```vba
Dim swApp As SldWorks.SldWorks
Dim swModel As SldWorks.ModelDoc2
Const UNIT_FACTOR As Double = 0.0254   ' inch → metre; MM=0.001, CM=0.01

Sub main()
    Set swApp = Application.SldWorks
    Set swModel = swApp.ActiveDoc
    If swModel Is Nothing Then
        MsgBox "No active document. Run 00_setup.vba first.", vbCritical
        Exit Sub
    End If
    ' ... step body ...
End Sub
```

- **Units:** the drawing works in its own units; every coordinate/length passed
  to the API is multiplied by `UNIT_FACTOR` (SolidWorks API is metres/radians).
- **Error handling:** append a JSON-line outcome per feature to
  `logs/macro_result.json` via a `LogResult "PASS"|"WARN"|"FAIL", "<step>",
  "<detail>"` helper. A FAIL must `Exit Sub` after logging (never continue on a
  broken feature). Wrap risky calls with a `Nothing`/status check.

## 3. Part / sketch / feature call patterns (verified signatures only)

Use only call shapes verified against the installed `sldworks.tlb`:

- **New part:** `swApp.NewDocument(templatePath, 0, 0, 0)`; `templatePath` from a
  robust `FindPartTemplate` (env `SOLIDWORKS_TEMPLATE_PATH` → default part
  template). Set document units, then `Extension.SaveAs(savePath, 0,
  swSaveAsOptions_Silent, Nothing, errs, warns)`.
- **Plane selection:** select a reference plane *robustly* — plane names vary by
  template/locale; try `"Front Plane"`, `"Front"`, then the first datum plane.
- **Sketch:** `swModel.SketchManager.InsertSketch True`; circles via
  `SketchManager.CreateCircleByRadius cx*UNIT_FACTOR, cy*UNIT_FACTOR, 0#,
  (dia/2#)*UNIT_FACTOR`; rectangles via `CreateCornerRectangle`.
- **Extrude:** `FeatureManager.FeatureExtrusion3(...)` (confirmed signature) for
  base/boss; blind depth `depth*UNIT_FACTOR`.
- **Cut:** sketch the profile/circle then `FeatureCut4`/`FeatureExtrusion` with a
  through-all or blind end condition; a plain hole is **always** a sketch-circle
  cut unless HoleWizard is explicitly enabled.
- **Holes (opt-in wizard):** the default is the proven sketch-circle cut. The
  legacy `HoleWizard5` path is OPT-IN (`MTI_ENABLE_HOLE_WIZARD=1`) with a verified
  27-arg signature and a fallback to the sketch-cut.
- **Circular pattern (reliability trio):** seed hole (`Fxxx_SeedHoleCut`) → named
  reference axis (`InsertAxis2` from the concentric bore's cylindrical face,
  `PatternAxisN`) → the pattern through a single `CreateCircularPatternSafe` VBA
  helper (version-pinned `FeatureCircularPattern5`, fallback `...4`; axis Mark=1,
  seed Mark=4; `Nothing` check + hard stop). `total_instances` INCLUDES the seed.
- **Chamfers then fillets** last, so edges still exist when referenced.

## 4. Prohibited features → MANUAL macros

loft / sweep / shell / wrap / dome / flex / draft-as-primary and any feature the
generator can't emit reliably become a numbered `NN_Fxxx_MANUAL_*.vba` that
`MsgBox`es the exact human step and `LogResult "WARN"`. Never skip silently.

## 5. Final verify + STL export (required, always present)

- `ZZ_final_verify.vba`: `ForceRebuild3(False)`; `Extension.GetMassProperties2`
  (fail if empty or `volume <= 0`); compare the model bounding box against the
  drawing envelope dims and `LogResult` the deltas.
- `ZZZ_export_stl.vba`: export `<part>.stl` next to the saved `.sldprt`
  (`SaveAs3`/`SaveAs` is extension-driven — set STL export options, then save to
  the `.stl` path). Sorts last so a full `RUN_ALL` ends with an STL.

## 6. Static audit (generation refuses on violation)

Every emitted macro is statically audited before it is written: banned APIs
(loft/sweep/shell/etc. as primary features) fail generation; canonical build-plan
fields may not be null (e.g. a `circular_pattern` step's `total_instances`). The
CadQuery pre-validation then builds the same geometry headlessly from
`build_plan.json` and must pass before any SolidWorks execution — and the **overall
shape check** confirms the built envelope, hole count and feature coverage match
the drawing's overall shape.

## 7. Manifest (Stage B output)

Codex writes `macros/codex_manifest.json`:
```json
{ "files": [ {"name": "01_F001.vba", "feature_ids": ["F001"], "purpose": "base solid"} ],
  "feature_coverage": { "F001": "BUILT", "F007": "MANUAL" },
  "assumptions": ["..."], "notes": ["..."], "engine": "codex", "model": "gpt-5.6-sol" }
```
Every build-plan step id must appear in `feature_coverage` as `BUILT`, `MANUAL`,
or `SKIPPED` (with a reason in `notes`) — this is what the overall-shape check's
feature-coverage gate verifies.
