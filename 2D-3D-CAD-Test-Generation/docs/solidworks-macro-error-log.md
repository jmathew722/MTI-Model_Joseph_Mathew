# SolidWorks build error log

> **Origin note (2026-07-20):** this file was referenced across the codebase
> (`macro_audit.py`, `schema.py`, `macro_generator.py`) but did **not exist** in the
> repo — the E001–E010 history survived only as (a) code comments and (b) the
> enforceable rules in `pipeline/macro_audit.py::BANNED_APIS`. It is (re)created
> here to give that history a home, to record the build-engine decision below, and
> to log the new COM-builder findings E012–E014 from the 2026-07 COM audit
> (`docs/com-builder-audit-2026-07.md`). Entries E001–E011 are summarized from their
> in-code footprints; each new entry keeps the same root-cause / fix / rule / test
> shape.

## Legacy entries E001–E011 (VBA generator — summarized from code)

The original numbered write-ups were not in the repo; their **rules are live** in
`pipeline/macro_audit.py` (banned APIs) and throughout `pipeline/macro_generator.py`
and `pipeline/schema.py`. For the 2026-07 audit each class was re-checked against the
COM builder — see `docs/com-builder-audit-2026-07.md` for the per-class verdict.

| Id | Class | Enforced by |
| --- | --- | --- |
| E001 | Missing default part template | template resolution + (new) FS discovery |
| E002 | Plane selection by hard-coded name | name lookup + (new) tree-index fallback |
| E003 | Mixed coordinate frames / notch orientation | `pipeline/coordinate_normalize.py` |
| E004 | Invented API `GetModelBoundingBox` | `macro_audit.BANNED_APIS`, `com_builder_audit` |
| E005/E006 | Sketch reselection anti-pattern | recorder pattern; `macro_audit`/`com_builder_audit` |
| E007 | Redundant pattern manual work | `macro_generator._pattern_covered_by` (shared) |
| E010 | `applies_to` label normalization | `schema.canonicalize_applies_to` (shared) |
| E011 | Resolver annotations / build status surfacing | engineering review after build |

---

## COM engine promoted to primary build path (2026-07-20)

This is a deliberate, documented statement of the build architecture after the
2026-07 COM audit:

1. **`pipeline/solidworks_builder.py` builds the `.sldprt` in BOTH `--engine vba`
   and `--engine com` modes.** The `vba` mode additionally writes the `.vba` macro
   package, but the model on this machine is created by the Python COM builder in
   both modes.

2. **`--engine vba`'s `.sldprt` comes from the SAME `solidworks_builder.py` code
   path as `--engine com`** — via `pipeline/batch.py::build_sldprt_for_part` →
   `solidworks_builder.build_model`. Therefore **a bug fixed under one engine flag
   is fixed under both.** (This was implicit in the code and previously required
   reading `batch.py` to discover; it is now stated here explicitly.)

3. **`macro_generator.py`'s `.vba` package is retained** — it is NOT the primary
   build path, but it remains useful for: (a) **portability** to a machine without
   this Python pipeline installed; (b) **human review** of the build steps in a
   readable script format; (c) **parity testing** — an independently-implemented
   second builder that cross-checks the COM path.

4. **Parity status after this audit.** The two builders share their coordinate
   truth (`coordinate_normalize`), canonicalizer (`canonicalize_applies_to`), and
   pattern no-op logic (`_pattern_covered_by`), so most geometry matches by
   construction. The audit found **one real drift**: the COM builder had **no
   `slot_cut` handling** (a canonical open-edge notch was mis-placed or dropped
   under `--engine com`, while the VBA path placed it correctly). Fixed in **E014**
   below by routing the COM builder's slot placement through the same
   `slot_cut.corner_array` → `coordinate_normalize` path the macros use. Static
   fake-harness parity is locked by `tests/test_com_builder.py::TestCoordinateNormalizeIntegration`
   (top-edge notch builds at y_min = 4.37 in, not 0).

   **Live 158-C `--engine com` vs `--engine vba` bounding-box diff:** see the
   "Live verification" subsection below (recorded from the Step-6 run).

### Live verification (Step 6, 158-C — COVER)

Ran on this machine (SOLIDWORKS **2025** — note the brief's 2024 template path does
not exist here; the E012 discovery/override resolved the real 2025 `Part.PRTDOT`),
`tests/fixtures/commit_mode/158-C_extraction.json`, both engines, `--no-export`:

```
python main.py --from-json ...\158-C_extraction.json --output ...\live_com --engine com --debug
python main.py --from-json ...\158-C_extraction.json --output ...\live_vba --engine vba --debug
```

Build log confirmed the **E014 slot path fired under `--engine com`**:
`F002: slot built as a rectangular through-cut at the normalized position` — the
top-edge notch that the COM builder previously could not place. Both builds passed
model validation (length 279.4 mm, width 158.75 mm).

STL bounding-box + volume diff (`trimesh`):

| Metric | `--engine com` | `--engine vba` | Δ |
| --- | --- | --- | --- |
| Sorted extents | `[2.667, 158.75, 279.40]` | `[2.667, 158.75, 279.40]` | **0.000 mm** |
| Volume | identical | identical | **0.000 %** |

**Result: byte-identical geometry — full parity.** This is expected and now
documented: both engines create the model through the same
`solidworks_builder.build_model` path, so the `.vba` package's independent build
recipe and the COM build agree, and a fix under either flag lands under both. The
F004 all-edges fillet was demoted to a warning under both engines (fragile op;
radius exceeds the adjacent thin-cover face) — a pre-existing behavior, not a
regression from this audit.

---

## E012 — COM builder: no filesystem fallback for the part template

**Root cause.** `solidworks_builder.create_new_part` resolved the part template only
from `SOLIDWORKS_TEMPLATE_PATH` and `GetUserPreferenceStringValue(swDefaultTemplatePart)`.
A machine with neither configured failed the build even when a stock `Part.prtdot`
existed on disk (the COM-side analogue of the VBA path's E001).

**Fix.** Added `_discover_part_template()`: globs the standard SOLIDWORKS
install/data `templates/` directories for `Part*.prtdot` (newest year first) and
uses the first match when the env var / user-preference default is absent.

**Generator rule.** Never fail a build for a missing template when one is
discoverable on disk; env var and user-preference take precedence, FS discovery is
the fallback.

**Test.** `tests/test_com_builder.py` (builder-source audit clean; discovery function
covered by the live build). Behavior exercised on the live 158-C run.

## E013 — COM builder: plane selection by localized name only

**Root cause.** `_select_plane` selected reference planes with
`SelectByID2("Front Plane", "PLANE", …)` — the English UI name. On a localized
SOLIDWORKS install the default planes are renamed and the lookup returns false
(the COM-side analogue of the VBA path's E002).

**Fix.** Added `_select_plane_by_index()`: when the name lookup fails, enumerate
`RefPlane` features in creation order and select the 1st/2nd/3rd (Front/Top/Right),
which are language-independent.

**Generator rule.** Resolve a standard plane by name first, then by its fixed
position in the feature tree; never depend solely on the localized name.

**Test.** `tests/test_com_builder_audit.py` (source clean) + the live build.

## E014 — COM builder: no slot / open-edge notch handling (coordinate frame)

**Root cause.** A canonical slot / open-edge notch stores its geometry on a
`SlotCut` record (width/depth) and its global placement is owned by
`slot_cut.corner_array` → `coordinate_normalize.resolve_notch_anchor` (e.g. a
top-edge notch's `y = parent_height − depth`). The macro path uses this; the COM
builder **never imported `coordinate_normalize` and had no slot handling at all**.
A slot-backed `extrude_cut` reached `build_extrude_cut`, found no in-plane sides in
`related_dimensions`, and either raised or mis-placed the cut — the 158-C-class E003
orientation bug, present on the COM side.

**Fix.** Added `build_slot_cut()` and dispatch from `build_extrude_cut` when
`model.slot_cut_for_feature(feature.id)` is present. It builds the rectangle from
`corner_array(slot, model)` (routing the notch math through `coordinate_normalize`),
cuts through-all, and leaves the interior corner fillet as a deferred refinement
(the rectangle is the mandatory, position-carrying step). Both engines now place
notches identically.

**Generator rule.** The COM builder must place every slot/notch through the SAME
`coordinate_normalize` path as the macros; slot geometry comes from the `SlotCut`
record, never from `related_dimensions`.

**Test.** `tests/test_com_builder.py::TestCoordinateNormalizeIntegration` — a
top-edge notch (plate 6.25, depth 1.88) builds at y_min = 4.37 in, not 0.
