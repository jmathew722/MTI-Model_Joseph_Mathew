# Research notes — Upgrade 3-Pack (2026-07-11)

## External references surveyed

### xarial/codestack (MIT) — cloned to `third_party/codestack` (gitignored)
The open-source library behind codestack.net. Mined for SolidWorks-API idioms;
patterns copied into our generator templates with attribution, code NOT called
at runtime (our macros stay dependency-free single files). Useful patterns:
- `solidworks-api/document/sketch/*` — `SketchUseEdge2/3` (Convert Entities)
  usage + error handling (raise on `False` return). Adopted into the
  reference-geometry sketch templates (Workstream 3 Convert-Entities discipline).
- Select-by-type feature-tree traversal (`FirstFeature`/`GetNextFeature`,
  `GetTypeName2`) — already used in our macros (`SelectRefPlane` helper); codestack
  confirms the idiom.
- Error-handling idiom: check the boolean return of every API call and raise a
  descriptive error — matches our existing `LogResult`/`WriteMacroResult` pattern.

### drawing→CAD reference-geometry reconstruction repos
Surveyed read-only. No MIT/BSD/Apache repo does 2D-drawing → SolidWorks
reference-geometry reconstruction in a directly reusable way (the space is
dominated by point-cloud/mesh reverse-engineering and closed-source CAD tools).
Nothing qualified for direct integration — not forcing one. The datum-skeleton
approach here is built from our own extraction's `datum_ref`/`relationships`
data + the codestack API idioms.

## SW API signatures pinned
`docs/sw_api_reference/reference_geometry_api.md` records the exact SolidWorks
2024 signatures for `InsertRefPlane`, `InsertAxis2`, `SketchUseEdge3`, and
`SelectByID2` that the generated macros use and that `macro_audit.py` whitelists.

## Decisions
- `third_party/` is gitignored — codestack is a mining checkout, not a vendored
  dependency (2445 files would bloat the repo; we copy only the specific idioms).
- Reference-geometry naming contract: `REF_DATUM_*`, `REF_SYM_*`, `REF_AXIS_*`,
  `REF_PT_*` (documented in CLAUDE.md).
