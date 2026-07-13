# Coordinate Normalization — One Canonical CAD Frame (2026-07-13)

## Root cause the layer prevents

Extraction correctly identifies a notch as opening from the **TOP** edge and
stores a top-edge-relative location such as `(1.56, 0)`. If any downstream stage
treats `y = 0` as an absolute lower-left coordinate, the notch lands on the
**bottom** edge. The orientation information (which edge the feature opens from)
is lost the moment a semantic edge-relative offset is used as a global
coordinate without going through the `parent_height - depth` conversion.

The fix is a single canonical convention and a single resolver, so that
conversion happens in exactly one place and both the UI table and the VBA
generator consume the same resolved object.

## Canonical convention

For the primary front view of a plate-like part:

```
Origin = lower-left corner of the finished parent profile
+X = right,  +Y = up,  +Z = extrusion/thickness
```

Lengths stay in **inches** through the normalized model; conversion to
**meters** (the SolidWorks API unit) happens exactly once, at the VBA boundary,
via `INCH_TO_M = 0.0254` / `to_meters()`. Inches and meters are never mixed in
one calculation, and no value is converted twice.

## Where raw anchors become global CAD coordinates

`pipeline/coordinate_normalize.py` — the ONE module. An explicit `Anchor` enum
(`TOP_EDGE`, `BOTTOM_EDGE`, `LEFT_EDGE`, `RIGHT_EDGE`, `LOWER_LEFT`,
`LOWER_RIGHT`, `UPPER_LEFT`, `UPPER_RIGHT`, `CENTER`, `DATUM_POINT`,
`DATUM_AXIS`, `FEATURE_RELATIVE`, `ABSOLUTE_GLOBAL`) plus two resolvers:

* `resolve_notch_anchor(anchor, offset_x, offset_y, width, depth, height,
  parent_width, parent_height) -> Bounds` — edge notches. **This is the single
  locus of the `H - depth` math.** TOP: `y_min = H - depth, y_max = H`. BOTTOM:
  `0 .. depth`. LEFT: `0 .. depth` in X. RIGHT: `W - depth .. W` in X.
* `resolve_point_anchor(anchor, offset_x, offset_y, parent_width,
  parent_height) -> Point` — holes, corners, center-relative points.

Plus `validate_bounds()` (in-parent + open-edge overshoot allowance) and
`assert_edge_orientation()` — the regression guard that refuses a notch resolved
to the wrong side.

`pipeline/slot_cut.corner_array()` now **delegates** its edge→global math to
`resolve_notch_anchor` (mapping `slot.open_edge` → the edge anchor via
`anchor_from_open_edge`), then applies the open-side overshoot and the
near-corners-first ordering the fillet step needs. Output is byte-faithful — the
golden macro set is unchanged. So the slot rectangle, the corner-fillet edge
selection, the build plan's `positions_xy`, the Tab-3 view-model, and the VBA
all trace back to the one resolver.

## Generation-time orientation guard

`macro_generator._assert_notch_orientation(model, pkg)` re-checks every built
`slot_rect_cut`'s corners against its semantic anchor through
`assert_edge_orientation`, using the REAL parent envelope (not the corners). A
`TOP_EDGE` notch resolved to `y = 0 .. depth` raises `MacroGenerationError`
(`NOTCH ORIENTATION …`) before the macro is finalized — the 158-C bug can never
ship again. It joins the existing generation guards (overshoot, label/payload,
echo check, no-dropped-positions, no-overlapping-holes).

## Viewer orientation — diagnosed, no change needed

The webapp Three.js viewer (`webapp/index.html`) loads the STL raw: it only
centers the geometry (`geometry.translate(-c.x,-c.y,-c.z)`) and frames the
camera. There is **no** `mesh.rotation.x = Math.PI`, no `scale.y = -1`, no
`geometry.scale(1,-1,1)`. So the viewer applies no orientation flip and needs no
compensating transform. Per the diagnostic rule: orientation correctness lives
entirely in the `.SLDPRT`/STL geometry (coordinate normalization), and a viewer
"fix" would be a double-correction — deliberately avoided. The UI visual
summary, build plan, SolidWorks model, exported STL, and browser preview all
read from the same resolved coordinates, so their top/bottom orientation agrees.

## 158-C proof

```
plate height H = 6.25 in,  notch depth = 1.88 in
global notch bottom  y_min = H - depth = 6.25 - 1.88 = 4.37 in
global notch top     y_max = H = 6.25 in   (open side overshoots to 6.30)

resolved F002:  x = 1.56 .. 3.18 in,  y = 4.37 .. 6.25 in
NEVER:          y = 0 .. 1.88 in
```

Generated `02_F002_slot_rect_cut.vba` CreateLine literals (meters): the closed
edge is `0.110998 m` (= 4.37 in) and the open edge `0.160020 m` (= 6.30 in) —
no `y = 0` / `0.047752 m` (1.88 in) bottom placement.

## Tests

`tests/test_coordinate_normalize.py` (23): all four edge notches, the point
anchors (upper/lower-left/right, center, absolute passthrough), inch→meter,
bounds validation (in-parent + overshoot + degenerate + non-finite), the
orientation guard (correct top passes; top-at-bottom and bottom-at-top
rejected), the `open_edge`→anchor mapping and resolver error handling, and the
end-to-end 158-C generator regression (F002 at y=4.37, and the guard refusing a
bottom-placed top notch). Full suite green; no golden regen needed (the slot
refactor is byte-faithful). Live SolidWorks COM build remains the standard final
check on a SolidWorks machine (not runnable in a headless environment).
