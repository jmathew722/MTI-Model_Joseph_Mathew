# Dimensioning & Coordinate Architecture — Research Notes (2026-07-17)

Phase 1 of the dimensioning-architecture overhaul. Everything the implementation
adopts is cited here with *what the practice is*, *why it exists*, and *exactly
how we adopt it*. The defect class being eliminated: the pipeline flattens every
position into absolute (x, y) floats and discards WHAT each dimension is
measured FROM, so corrections move features wrong. The fix is an explicit
per-feature **PositionAnchor** record and a single **position solver** that
derives absolute coordinates from anchors — the drawing's own dimensioning
scheme survives to the macro.

---

## 1.1 Dimensioning scheme taxonomy (the core theory)

Standard machine-drawing practice (ASME Y14.5 and every drafting text) uses a
small set of schemes. Which scheme a drawing uses **per feature** determines
what that feature's position is *anchored to*; preserving the anchor is what
makes a model robust to value corrections.

### Chain (linear / consecutive / point-to-point)
* **What**: each feature dimensioned from the *previous* feature
  (`|--2.75--|--2.75--|--2.75--|`).
* **Why it exists**: the designer cares about the *pitch* between features
  (e.g. mating parts, repeated tooling moves), not their distance to an edge.
* **Tolerance behavior**: tolerances **STACK** — position error accumulates
  down the chain. Feature N's worst-case position error is the sum of N
  tolerances. This is the defining property: correcting one chain link MUST
  move every feature downstream of it, and must NOT move anything upstream.
* **How we adopt**: `scheme: "chain"`, `anchor_ref: "<previous feature id>"`.
  The solver accumulates chains **in drawing order**, reusing the resolver's
  existing arithmetic-chain structures (Stage 2.5 already discovers
  `D007 = D008 + D009` closures — the build side now consumes the same
  chains rather than re-deriving positions ad hoc).

### Baseline (datum-line dimensioning)
* **What**: every feature dimensioned from ONE common reference edge/feature;
  the dimension lines stack visually but each is independent.
* **Why**: eliminates tolerance accumulation — each feature's position error
  is bounded by its own tolerance only.
* **Tolerance behavior**: does NOT stack. Correcting one dimension moves
  exactly one feature.
* **How we adopt**: `scheme: "baseline"`, `anchor_ref: "part_edge_left"` (or
  whichever edge the drawing measures from). This is today's implicit
  convention (lower-left-corner drawing frame) made explicit and per-feature.

### Ordinate (arrowless / running dimensioning)
* **What**: X=0 / Y=0 datum edges are marked (often with the ASME
  **dimension-origin symbol**, a small circle at the origin end of a dimension
  line); every feature is labeled with its perpendicular distance from the zero
  lines. The machining/CMM-friendly form of baseline.
* **Why**: uncluttered drawings for hole-rich plates; maps 1:1 to CNC
  coordinates.
* **Tolerance behavior**: identical to baseline (no stacking).
* **How we adopt**: `scheme: "ordinate"` with `anchor_ref` = the detected zero
  edge. Extraction gains a prompt instruction to report the dimension ORIGIN
  (zero edges / origin symbol); Stage 2.5 selects the canonical frame from it.

### Coordinate (Cartesian table)
* **What**: explicit (X, Y[, Z]) from a stated common origin, often a hole
  table.
* **How we adopt**: `scheme: "coordinate"`, `anchor_ref: "origin"`. **This is
  the degenerate case the current pipeline already implements** — a bare
  position from the drawing-frame origin. Legacy behavior maps onto it
  unchanged, which is what keeps the golden outputs byte-identical.

### GD&T datum reference frame (ASME Y14.5)
* **What**: features positioned by **true position** (basic dimensions +
  position tolerance zones) relative to datum features `A|B|C`. Datums are
  *physical features* — a face, a bore, a datum HOLE — not abstract axes.
  The DRF constrains the six degrees of freedom in order (primary/secondary/
  tertiary).
* **Why**: functional tolerancing — the tolerance zone is round (⌀), datum
  precedence mirrors how the part is fixtured/inspected.
* **How we adopt**: `scheme: "datum_frame"`, `anchor_ref: "DRF_A|B|C"`,
  `semantics: "true_position"`. Extraction already reads `datum_ref` on
  GD&T frames; the reference-geometry workstream already builds
  `REF_DATUM_A/B/C`. The anchor record now ties a feature's position to that
  frame explicitly.
* **Datum-hole pattern** (fixture/plate drawings): two precision holes carry
  the datum letters and *everything* is positioned from their centers, not
  from part edges. We adopt: extraction marks `is_datum_hole: true` on a hole
  callout when it carries a datum letter or when position dims chain to hole
  centers; the solver grounds anchors at `DATUM_HOLE_<n>` (hole center), and
  Stage 2.5 prefers a datum-hole frame over the default corner convention.

### Polar / bolt-circle (BSC)
* **What**: radius (bolt-circle ⌀/2, often marked **BSC** = basic) + angle
  from a center datum.
* **Why**: round parts are dimensioned from their own center; angular
  equal-spacing notes ("8 HOLES EQ. SP.") replace per-hole angles.
* **Tolerance behavior**: radial and angular errors are independent; the
  center datum is the single ground.
* **How we adopt**: `scheme: "polar_bsc"`, `anchor_ref` = the center feature
  (`F00x_center`), `axis: "radial"` + `axis: "angular"` anchor pair. This is
  the existing circular-pattern route's `bolt_circle_radius_in` +
  `seed_angle_deg` made into first-class anchors. The 164-C flywheel failure
  (all hole groups collapsing to one estimated center because "no numeric
  positions" existed) is exactly the case where a polar anchor pair carries
  the position that a bare (x, y) could not.

### Key implications implemented against
1. **Anchor survival**: a position is only as correct as its anchor; the
   anchor must survive from extraction through resolution into the build plan
   and the macro (as sketch-dimension annotations + derivation trace).
2. **Chain accumulation is directional**: solve in topological order; a chain
   correction propagates downstream only.
3. **Datum holes beat edges**: when a drawing grounds in hole centers, edge
   distances are DERIVED, not authoritative — the frame selection must record
   which ground was chosen.

---

## 1.2 Mined repositories and sources

`third_party/` is gitignored (mining checkout, never a runtime dependency).
Restrictively-licensed or heavyweight sources were **summarized read-only**
from their public pages rather than cloned; nothing below is vendored into the
pipeline. Adoption is *ideas and math*, re-implemented in our own code.

| Source | License | What it does | What we take | What we ignore |
|---|---|---|---|---|
| [adityaintwala/Image2CAD](https://github.com/adityaintwala/Image2CAD) | Apache-2.0 | Raster CAD drawing → DXF: line/circle detection → **arrowhead detection → dimension-line identification → Tesseract text → feature correlation** (associating dimensional text with the entity it measures) | The *association* concept: a dimension is (text, dimension line, two arrowheads, two extension lines) and its ANCHOR is the entity each extension line lands on. Our `PositionAnchor.anchor_ref` + the vector cross-check (§1.3) implement the same association at the vector level; the extraction prompt now asks the vision model to name what each positional dimension is measured FROM. | Their raster detection stack (OpenCV 3.x-era Hough pipelines) — we already have vision extraction + ezdxf/PyMuPDF vector paths and a HoughCircles fallback. |
| Photo2CAD ([arXiv:2101.04248](https://arxiv.org/abs/2101.04248), Harish & Prasad OpenCV notebooks) | code MIT (notebooks) | Photos of orthographic views → boundary detection → **bounding box → point locations relative to the box** → SCAD solids | The *bounding-box-relative oracle*: measure every detected point relative to the outline's bounding box and compare against the dimension text. Adopted as the vector cross-check contract in `position_solver` research (§1.3): "1.56 from left edge" is verified as a measured perpendicular distance from the fitted left edge line, not only trusted as OCR. | The photo rectification and SCAD emission; hidden-line handling (they punt on it, we already do cross-view correspondence in Stage 1.5). |
| [PrincetonLIPS/SketchGraphs](https://github.com/PrincetonLIPS/SketchGraphs) ([arXiv:2007.08506](https://arxiv.org/abs/2007.08506)) | MIT (code), CC-BY-NC (data) | 15M real CAD sketches as **geometric constraint graphs**: primitives are nodes, designer-imposed constraints (coincident, distance, radius…) are edges; single-primitive dimensions are self-loop edges; sub-primitives (endpoints) are their own nodes | The representation shape: our build plan moves toward "entities + dimensional constraints as edges". `PositionAnchor` IS a constraint edge (feature → anchor entity, labeled with dimension ids, axis, value, semantics), and `position_solver` is a deliberately tiny constraint propagator: topological order + accumulation (full iterative constraint solving is out of scope — drawings give us a DAG, not a cyclic system). | The GNN models, the Onshape data pipeline, generative modeling. |
| GitHub topic `engineering-drawings` — SolidWorks MCP server | MIT (typical) | COM automation of SolidWorks from agents | COM call/error-handling patterns already mined in the prior codestack pass — notably: never trust a bare `Nothing` return, name features immediately after creation. No new adoption; we keep our own builder. | The MCP layer itself. |
| GitHub topic `engineering-drawings` — PaddleOCR drawing-OCR workflows | Apache-2.0 | OCR presets for engineering drawings (binarization, deskew, DPI floors) | Confirms our existing `adaptive_render` DPI-escalation approach (Workstream 2); noted that dimension text OCR needs ≥300 DPI and high-contrast binarization — already satisfied. No code taken. | The Paddle runtime (heavy dependency; our extraction is vision-model based). |
| FreeCAD TechDraw auto-dimensioning (STEP → dimensioned PDF repos) | LGPL (FreeCAD ecosystem) | RULE-BASED auto-dimensioning: which features get dimensioned from what (edges → baseline; holes → center-to-edge or hole table; circles → diameter callout) | The **reverse-oracle idea**: the rules that *generate* dimensions are the rules for *interpreting* them. Encoded in the solver's anchor-derivation defaults: a hole position dim with an edge-named `applies_to` (`hole_position_x` from left) is baseline; qty>1 uniform pitch is chain; a bolt-circle dim is polar. LGPL means: idea only, no code copied. | FreeCAD integration. |
| [xarial/codestack](https://github.com/xarial/codestack) (already in `third_party/` when present) | MIT | SolidWorks API examples | Reference-geometry & named-selection idioms (already adopted in Workstream 3: `REF_DATUM_*` naming contract). Sketch-dimension emission idioms (`AddDimension2`) reviewed and **deliberately not adopted as executable code** — the repo's standing decision (Stage-7 hardening, 2026-07-12) is that unverified `AddDimension2` smart-dimensioning stays opt-in-future until verified live; anchors are therefore emitted as structured, audited annotations + derivation traces, not live dimension calls. | Anything add-in/C++-specific. |
| ASME Y14.5 practice guides (GD&T Basics, CMM Quarterly) | n/a (practice) | Datum reference frames, basic dimensions, true position, the dimension-origin symbol, BSC | The recognition rules in §1.1: datum letters on faces/holes ⇒ `datum_frame`; boxed (basic) dims ⇒ true-position semantics (no ± tolerance of their own); origin symbol ⇒ ordinate zero edge; BSC ⇒ polar. | Full tolerance-zone math (stack-up analysis is out of scope; we record scheme + anchors, we do not compute tolerance zones). |

---

## 1.3 Edge detection / vector cross-check (verifying "1.56 from left edge")

We already extract vector geometry (ezdxf entities, PyMuPDF Bézier circles)
in the three-source consensus for hole positions. The anchor architecture
extends the same machinery to **anchor verification**: an OCR'd/vision-read
positional value can be *measured* on the drawing when vectors exist.

### Edge line fitting
* Collect the outline segments (already found by the vector extractor's
  outline matcher). For a candidate edge (e.g. "left edge"): take all outline
  segments whose direction is within ~5° of vertical and whose x-extent is in
  the leftmost band; fit the edge line by **total least squares** (PCA on the
  segment endpoint cloud): the line passes through the centroid with direction
  = the first principal component. TLS, not ordinary LS, because drawing edges
  can be near-vertical where y-on-x regression degenerates.

### Perpendicular foot distance
* The measured value of "feature F is d from edge E" is the **perpendicular
  distance from F's center to E's fitted line**:
  `dist = |(p - c) · n̂|` where `c` is a point on the line and `n̂` the unit
  normal. Compare against the resolved dimension value; disagreement beyond
  tolerance (we use the vector extractor's existing 0.005-in class tolerance,
  scaled by sheet scale) does NOT overwrite — it becomes a **candidate
  reading** for the Stage 2.5 resolver, exactly like conflicting tile
  readings (`possible_values`). Never silently clamped, never fabricated.

### Circle-center fitting for datum holes
* Datum-hole centers from vectors: DXF `CIRCLE` entities give centers
  exactly; PDF Bézier circles use the existing 4-arc control-point fit; raster
  fallback uses HoughCircles (already flagged non-vector-exact). For a
  redundant fit (many points on the circle), algebraic least-squares
  (Kåsa fit): minimize `Σ(x²+y²+Dx+Ey+F)²` — linear, exact enough at drawing
  resolution; refine with one Gauss-Newton step on geometric distance if
  residuals are high.
* Datum-hole **pair frame**: origin = hole-1 center; +x = unit vector to
  hole-2 center; +y = its left normal. Every `DATUM_HOLE_*`-anchored value is
  then measurable as a dot product in that frame.

### Adoption summary
`position_solver.py` exposes the pure math (`point_to_line_distance`,
`fit_edge_line`, `datum_pair_frame`) so the vector cross-check is unit-testable
against synthetic DXF geometry (Phase 3.5's 1e-6 test) without any drawing on
disk. Wiring the live vector measurement into `hole_resolution.py`'s consensus
follows the existing precedence rule: **vector geometry owns position, the
vision callout owns semantics; disagreement keeps both and flags CRITICAL.**

---

## What-we-adopted-from-where (one-screen table)

| Adopted thing | From | Into |
|---|---|---|
| Dimension↔entity *association* as the anchor identity | Image2CAD feature correlation | `PositionAnchor.anchor_ref` + extraction prompt |
| Bounding-box-relative measurement oracle | Photo2CAD | §1.3 vector cross-check contract |
| Constraint-graph build-plan shape (entities + dimension edges) | SketchGraphs | `PositionAnchor` list per feature; solver = tiny DAG propagator |
| Rules-that-generate-are-rules-that-interpret | FreeCAD TechDraw auto-dimensioning | solver's scheme-derivation defaults |
| Scheme taxonomy, tolerance stacking, datum precedence, origin symbol, BSC | ASME Y14.5 practice | §1.1 → schema `scheme`/`semantics` enums; chain accumulation order |
| Edge TLS fit / perpendicular foot / Kåsa circle fit | standard CV/metrology practice | `position_solver` pure-math helpers |
| Named reference geometry as selection handles | codestack (prior pass) | anchors ground at `REF_DATUM_*`/`REF_PT_*` names (existing contract) |
| Sketch dimensioning emitted as audited ANNOTATIONS, not live `AddDimension2` | repo standing decision (Stage-7 hardening) | macro anchor annotations + `macro_audit` check |

Sources: [Image2CAD](https://github.com/adityaintwala/Image2CAD) ·
[SketchGraphs paper](https://arxiv.org/abs/2007.08506) ·
[SketchGraphs code](https://github.com/PrincetonLIPS/SketchGraphs) ·
[Photo2CAD](https://arxiv.org/abs/2101.04248)
