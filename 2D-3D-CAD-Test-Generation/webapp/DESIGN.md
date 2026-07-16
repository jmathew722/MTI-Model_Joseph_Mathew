# MTI Pipeline UI — Design System

**Single source of truth for design tokens: `webapp/static/theme.css`** (served
at `/static/theme.css`). It holds every color, font, spacing, radius and
severity token — under both the company's semantic names and this app's
historical names as aliases. **`webapp/static/design-tokens.css`** now holds
only the base resets and shared component recipes (buttons, badges, inputs,
tabs, cards…), each resolving its colors through `theme.css` tokens. Both files
are linked — `theme.css` first — by **both** documents (`index.html` and
`photoapp/index.html`), so Tab 1's cropper and the host app share the exact same
theme. **To retune the product, edit the values in `theme.css` — nothing else.**
See `docs/company-theme-analysis.md` for how the palette was derived.

## Theming — how to change the look

1. Open `webapp/static/theme.css`. Edit the values in the **PALETTE** block
   (`--ink`, `--paper`, `--surface`, `--blue`, `--green`, `--amber`, `--red`, …).
2. The app aliases (`--bg`, `--bg-raised`, `--blueprint`, `--ok`, `--sev-*`, …)
   point at those primitives, so every surface updates from that one edit.
3. Severity colors are the `--sev-critical|high|medium|low` (+ `-soft` tint)
   tokens — keep the red › orange › yellow › blue ordering.
4. Dark viewer/console surfaces are `--viewer-*` / `--console-*`; scrims over the
   drawing are `--scrim` / `--scrim-strong`; the dark topbar is `--topbar-*`.
5. Fonts are `--sans` / `--mono` (Inter / JetBrains Mono with system fallbacks —
   no font files, no CDN). Reload the page; no build step.

Rule: never hardcode a hex/rgba in a page style or component — add or reuse a
token in `theme.css` and reference it with `var()`.

---

## Direction — ENGINEERING PAPER (company identity)

Ported from the company reference repo (MTI_ModelCodex,
`agent/implement-draw2part-pipeline`). A light **engineering-paper** theme: a
warm off-white sheet (`--paper`), white cards, one strong **blue** accent, muted
green/amber/red status, a **dark topbar**, and **dark viewer panels** for
contrast with linework and the STL. Monospace numerals wherever a value is a
measurement; mono "eyebrow" micro-labels above every section.

Depth is **borders only** — no drop shadows. The app is dependency-free vanilla
HTML/CSS/JS with a strict no-CDN/vendored-assets rule, so patterns are shared CSS
classes, not components. The drafting motifs this app added on top of the
reference — the title-block topbar cells, sheet-numbered tabs, centerline
dividers, OP-numbered stage chips, inspection-stamp severities — are preserved
and re-chromed to the company palette.

> **Note:** the color/spacing tables below document the *previous* dark
> "Blueprint Room" direction and are kept for historical component reference.
> The live values now live in `theme.css` (company light theme); treat that file
> as authoritative wherever it disagrees with the tables here.

## Color

### Surfaces (Prussian navy — one hue, lightness steps only)

| Token | Value | Use |
|---|---|---|
| `--well` | `#0A0F16` | deepest: canvas wells, console, 3D viewport, list boxes |
| `--bg` | `#0D131C` | page background |
| `--bg-raised` | `#111927` | toolbars, tab rails, panel captions, status bars |
| `--surface` | `#16202F` | cards, chips, secondary buttons |
| `--surface-2` | `#1B2738` | hover states on surfaces |
| `--input-bg` | `#090E15` | inputs — inset *below* their surroundings |

### Ink (cool drafting white, 4 tiers)

`--ink #E7EDF5` (primary) · `--ink-2 #AEBACB` (secondary) · `--ink-3 #7D8A9E`
(labels/muted) · `--ink-4 #4E5A6C` (faint/disabled).

### Accents

- **Blueprint cyan — the one interactive accent (~10% of any screen).**
  `--blueprint #3FA9D4`, `--blueprint-bright #5FBFE4` (hover),
  `--blueprint-text #7ECFEE` (as text), `--blueprint-dim` (12% tint),
  `--on-blueprint #04191F` (text on cyan). Used for: primary buttons, active
  tab underline + sheet number, selection (part card, crop marquee), focus
  ring, links, the running stage chip.
- **Steel — secondary/informational.** `--steel #5C7690`, `--steel-text
  #9FB6CC`, `--steel-dim`. Used for: format badges, completed stage chips.
  Never for primary actions.
- **Status (inspection: accept / reject / hold):** `--ok #3EAF7C` /
  `--err #E5484D` / `--warn #E3A93C`, each with a `-text` variant.
- **Datum (functional):** `--datum #00C2FF` / `--datum-dim` — the locked (0,0)
  origin crosshair drawn on the drawing and its UI chips. A vivid azure that
  reads over white linework; functional like the severity ladder, NOT a second
  interactive accent (the one accent stays blueprint cyan).
- **Legacy aliases:** `--copper*`/`--teal*` map to blueprint/steel so the
  photoapp document needs no edits when the direction evolves.

### Severity ladder (functional, never decorative)

Severity renders as **inspection stamps**: outlined, letterspaced chips (color
border + 8–10% tint), identical everywhere (Engineering Flags, left rails,
requirement chips):

CRITICAL `#F0545C` · HIGH `#E3A13C` · MEDIUM `#D2C355` · LOW `#8DA4BE` — red →
amber → yellow → slate, distinguishable at a glance, and deliberately *not*
the brand cyan.

## Typography

- **Sans (UI text):** `--sans` — Segoe UI Variable/system stack. No webfonts:
  every asset is vendored, nothing loads from a CDN.
- **Mono (technical content):** `--mono` — Cascadia/Consolas stack. Dimensions,
  file names/paths, JSON, console, token counts, sheet/OP numbers, title-block
  values — always `tabular-nums` for dynamic numbers.

Scale (px): `--fs-caption 11` (uppercase micro-labels) · `--fs-small 12` ·
`--fs-body 13` (dense tool default) · `--fs-control 14` (buttons/tabs) ·
`--fs-h2 16` (app title) · `--fs-stat 20` (stat values, verdict stamp) ·
`--fs-display 30` (reserved for hero numbers). Weights 400/500/600/700;
hierarchy comes from weight + ink tier first, size second.

## Spacing & radius

4px grid only: `--sp-1..--sp-6` = 4/8/12/16/24/32. Radius scale (crisp,
technical): `--r-sm 2` (chips/stamps) · `--r-md 4` (buttons/inputs) ·
`--r-lg 6` (cards/panels).

## Depth & borders

**Borders only.** `--line rgba(185,212,240,.10)` standard · `--line-soft .06`
(row separators) · `--line-strong .20` (secondary button edges) · `--ring`
(cyan focus). The only box-shadows are the 1px cyan ring on the selected part
card and the soft glow on the API status dot. Elevation = surface lightness
step, nothing else.

## Component recipes (in design-tokens.css)

| Class | Recipe |
|---|---|
| `.btn` | 36px h · 0 16px pad · r-md · 14px/600 · cyan fill; `.secondary` (surface + strong border), `.ghost`, `.danger` (outline red), `.running`, `.on` (blueprint-tinted active state for tool toggles), sizes `.sm` 28px / `.xl` 46px; `:active` scale(.97) |
| `.ibtn` | 26px square quiet icon button |
| `.badge-c` | bordered chip, 11px/600, r-sm; tints: `.blueprint .steel .ok .err .warn` (aliases `.copper .teal`), `.mono` |
| `.badge-sev` | OUTLINED severity stamp, 10px/700 uppercase tracked: `.critical .high .medium .low` |
| `.input-c` | inset field: `--input-bg`, quiet border, cyan focus border |
| `.tab-c` | underline tab: transparent, 2px cyan underline + full-ink text when `.active`; `.sm` for sub-tabs |
| `.cap-c` | 11px/600 uppercase tracked label (panel captions) |
| `.progress-c` | 6px track (`--well`) + cyan fill |
| `.console-c` | `--well` ground, 12px mono, cool-gray text |
| `.card-c` | surface + line border + r-lg |
| `.seg-c` | segmented control: inset ground, 2px padding, cyan-filled active segment |

Page-specific styles in each document may **compose** these classes and bind
extra layout rules, but every color/size/spacing value must reference a token.

## Signature elements

- **Title-block header:** the app header is an engineering drawing's title
  block — brand left, hairline-bordered data cells right (ENGINE ·
  DELIVERABLES · API STATUS: micro-label over mono value), with a drafting-
  ruler tick strip along the bottom edge.
- **Sheet tabs:** the three top tabs are the SHEETS of a drawing set — a mono
  `SHEET n` plate that lights cyan on the active tab.
- **Centerline dividers:** the split-view drag divider carries the drafting
  centerline pattern (long dash · gap · short dash), the line type used for
  axes on real drawings.
- **Traveler stage strip (Tab 3):** stage chips are numbered `OP 01…` via CSS
  counters in mono, like operations on a shop routing sheet — done = steel + ✓,
  current = cyan-tinted.
- **Inspection stamps:** severity chips and the post-run verdict banner are
  outlined, letterspaced stamps (the READY / NOT READY verdict is a bordered
  mono stamp on a status-tinted strip).
- **Blueprint crop marquee (Tab 1):** the cropper's selection dash, corner
  handles, and px-dimension readout use the same cyan as every other selection.
- **Blueprint 3D viewport (Tab 2):** navy well background, cool key light +
  cyan rim light, machined-aluminum part material — the palette carried into
  the scene itself.

## Rules

1. No hex values in page styles — tokens only.
2. Cyan is scarce: one primary action per view; selection + focus; the active
   stage. Everything else is navy + ink.
3. Severity colors mean severity — never reuse them decoratively.
4. Any dynamic number gets `--mono` + `tabular-nums`.
5. No drop shadows (beyond the two sanctioned rings/glows), no decorative
   gradients, no new hues.
6. Emoji are banned in chrome (colored glyphs break the palette); use
   monochrome glyphs/text.
