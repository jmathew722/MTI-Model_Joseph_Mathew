# MTI Pipeline UI — Design System

Single source of truth: **`webapp/static/design-tokens.css`** (served at
`/static/design-tokens.css`, linked by **both** documents — `index.html` and
`photoapp/index.html` — so Tab 1's cropper and the host app share the exact
same tokens and component recipes). This file documents the decisions; the CSS
file implements them. If you change a value, change it in the tokens file, not
in a page style.

---

## Direction — PAPER ROOM

The product converts 2D engineering drawings into SolidWorks parts, so the
interface is built from the drawing's own world: **the drawing office**, now
lit from a window instead of a desk lamp. Warm paper-white surfaces, ink-black
text, ONE blueprint-blue accent (the line color of a working print), hairline
rules, inspection-stamp semantics, and monospace numerals wherever a value is
a measurement. Viewport wells (drawing canvas, 3D viewer, console) stay a deep
graphite — a light table's glass stays dark so the print on it reads. The
reference points are a drafting table in daylight and a CAD viewport — not
SaaS dashboards.

Depth is **borders only** — no drop shadows. The shell is light; the
well/viewer/console surfaces are the one deliberately dark exception (they
hold rendered drawings and 3D geometry, which read best against dark glass —
this mirrors how the shell's own dark title-block header sits atop the light
page). The app is dependency-free vanilla HTML/CSS/JS with a strict
no-CDN/vendored-assets rule, so patterns are shared CSS classes, not
components.

## Color

### Surfaces (paper white → graphite wells)

| Token | Value | Use |
|---|---|---|
| `--well` | `#14171C` | deepest: canvas wells, console, 3D viewport, list boxes — stays dark |
| `--bg` | `#FAFAF7` | page background (paper) |
| `--bg-raised` | `#FFFFFF` | toolbars, tab rails, panel captions, status bars |
| `--surface` | `#FFFFFF` | cards, chips, secondary buttons |
| `--surface-2` | `#F3F3EF` | hover states on surfaces |
| `--input-bg` | `#FCFCFA` | inputs — subtly recessed |

### Ink (warm near-black, 4 tiers)

`--ink #111318` (primary) · `--ink-2 #45484F` (secondary) · `--ink-3 #666A72`
(labels/muted) · `--ink-4 #9A9DA4` (faint/disabled).

### Accents

- **Blueprint blue — the one interactive accent (~10% of any screen).**
  `--blueprint #1D5BDB`, `--blueprint-bright #3F74E5` (hover),
  `--blueprint-text #1D5BDB` (as text — legible on both paper and the dark
  wells, so text and fill share one value), `--blueprint-dim` (10% tint),
  `--on-blueprint #FFFFFF` (text on a blue fill). Used for: primary buttons,
  active tab underline + sheet number, selection (part card, crop marquee),
  focus ring, links, the running stage chip.
- **Steel — secondary/informational.** `--steel #5C6B80`, `--steel-text
  #4A5568`, `--steel-dim`. Used for: format badges, completed stage chips.
  Never for primary actions.
- **Status (inspection: accept / reject / hold):** `--ok #177A4C` /
  `--err #C0392B` / `--warn #B7791F`, each with a `-text` variant (equal to
  the base value — the base hue itself is dark enough to read as text on
  paper, unlike the old dark-theme tints).
- **Datum (functional):** `--datum #0891B2` / `--datum-dim` — the locked (0,0)
  origin crosshair drawn on the drawing and its UI chips. A saturated
  teal-cyan that reads over white linework; functional like the severity
  ladder, NOT a second interactive accent (the one accent stays blueprint
  blue).
- **Legacy aliases:** `--copper*`/`--teal*` map to blueprint/steel so the
  photoapp document needs no edits when the direction evolves.

### Severity ladder (functional, never decorative)

Severity renders as **inspection stamps**: outlined, letterspaced chips (color
border + 8–12% tint), identical everywhere (Engineering Flags, left rails,
requirement chips):

CRITICAL `#C0392B` · HIGH `#B7791F` · MEDIUM `#8A7A1F` · LOW `#5C6B80` — red →
amber → olive → steel, distinguishable at a glance, and deliberately *not*
the brand blue. Critical/High intentionally share hue with `--err`/`--warn` —
one small functional palette, not two.

## Typography

- **Sans (UI text):** `--sans` — Inter, falling back to the Segoe UI/system
  stack. No webfonts are fetched: Inter is used where already installed,
  system UI fonts otherwise — nothing loads from a CDN.
- **Mono (technical content):** `--mono` — JetBrains Mono, falling back to
  Consolas/system mono. Dimensions, file names/paths, JSON, console, token
  counts, sheet/OP numbers, title-block values — always `tabular-nums` for
  dynamic numbers.

Scale (px): `--fs-caption 11` (uppercase micro-labels) · `--fs-small 12` ·
`--fs-body 13` (dense tool default) · `--fs-control 14` (buttons/tabs) ·
`--fs-h2 16` (app title) · `--fs-stat 20` (stat values, verdict stamp) ·
`--fs-display 30` (reserved for hero numbers). Weights 400/500/600/700;
hierarchy comes from weight + ink tier first, size second.

## Spacing & radius

4px grid only: `--sp-1..--sp-6` = 4/8/12/16/24/32. Radius scale (crisp,
technical): `--r-sm 3` (chips/stamps) · `--r-md 5` (buttons/inputs) ·
`--r-lg 8` (cards/panels).

## Depth & borders

**Borders only.** `--line rgba(17,19,24,.10)` standard · `--line-soft .06`
(row separators) · `--line-strong .20` (secondary button edges) · `--ring`
(blue focus). The only box-shadows are the 1px blue ring on the selected part
card and the soft glow on the API status dot. Elevation = surface lightness
step, nothing else.

## Component recipes (in design-tokens.css)

| Class | Recipe |
|---|---|
| `.btn` | 36px h · 0 16px pad · r-md · 14px/600 · blue fill; `.secondary` (surface + strong border), `.ghost`, `.danger` (outline red), `.running`, `.on` (blueprint-tinted active state for tool toggles), sizes `.sm` 28px / `.xl` 46px; `:active` scale(.97) |
| `.ibtn` | 26px square quiet icon button |
| `.badge-c` | bordered chip, 11px/600, r-sm; tints: `.blueprint .steel .ok .err .warn` (aliases `.copper .teal`), `.mono` |
| `.badge-sev` | OUTLINED severity stamp, 10px/700 uppercase tracked: `.critical .high .medium .low` |
| `.input-c` | inset field: `--input-bg`, quiet border, blue focus border |
| `.tab-c` | underline tab: transparent, 2px blue underline + full-ink text when `.active`; `.sm` for sub-tabs |
| `.cap-c` | 11px/600 uppercase tracked label (panel captions) |
| `.progress-c` | 6px track (`--well`) + blue fill |
| `.console-c` | `--well` ground, 12px mono, cool-gray text |
| `.card-c` | surface + line border + r-lg |
| `.seg-c` | segmented control: inset ground, 2px padding, blue-filled active segment |

Page-specific styles in each document may **compose** these classes and bind
extra layout rules, but every color/size/spacing value must reference a token.

## Signature elements

- **Title-block header:** the app header is an engineering drawing's title
  block, printed in dark ink so it reads as a distinct band atop the light
  page — brand left, hairline-bordered data cells right (ENGINE ·
  DELIVERABLES · API STATUS: micro-label over mono value), with a drafting-
  ruler tick strip along the bottom edge.
- **Sheet tabs:** the three top tabs are the SHEETS of a drawing set — a mono
  `SHEET n` plate that lights blue on the active tab.
- **Centerline dividers:** the split-view drag divider carries the drafting
  centerline pattern (long dash · gap · short dash), the line type used for
  axes on real drawings.
- **Traveler stage strip (Tab 3):** stage chips are numbered `OP 01…` via CSS
  counters in mono, like operations on a shop routing sheet — done = steel + ✓,
  current = blue-tinted.
- **Inspection stamps:** severity chips and the post-run verdict banner are
  outlined, letterspaced stamps (the READY / NOT READY verdict is a bordered
  mono stamp on a status-tinted strip).
- **Blueprint crop marquee (Tab 1):** the cropper's selection dash, corner
  handles, and px-dimension readout use the same blue as every other
  selection.
- **Dark 3D viewport (Tab 2):** graphite well background, cool key light +
  blue rim light, machined-aluminum part material — the one dark surface in
  an otherwise light page, matching the well/console treatment everywhere
  else.

## Rules

1. No hex values in page styles — tokens only.
2. Blue is scarce: one primary action per view; selection + focus; the active
   stage. Everything else is paper + ink.
3. Severity colors mean severity — never reuse them decoratively.
4. Any dynamic number gets `--mono` + `tabular-nums`.
5. No drop shadows (beyond the two sanctioned rings/glows), no decorative
   gradients, no new hues.
6. Emoji are banned in chrome (colored glyphs break the palette); use
   monochrome glyphs/text.
