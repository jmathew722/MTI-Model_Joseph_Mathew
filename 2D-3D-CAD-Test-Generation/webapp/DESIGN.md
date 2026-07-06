# MTI Pipeline UI — Design System

Single source of truth: **`webapp/static/design-tokens.css`** (served at
`/static/design-tokens.css`, linked by **both** documents — `index.html` and
`photoapp/index.html` — so Tab 1's cropper and the host app share the exact
same tokens and component recipes). This file documents the decisions; the CSS
file implements them. If you change a value, change it in the tokens file, not
in a page style.

---

## Direction

Professional engineering software for a customer-facing demo — the reference
points are Fusion 360's orange-on-dark and Altium's dark workbench, not SaaS
dashboards. The feel is **machined**: graphite surfaces, one copper accent used
sparingly (like anodized tooling against dark metal), a muted slate-teal for
informational elements, monospace for every technical value (dimensions, paths,
token counts, stage numbers), and **borders-only depth** — no drop shadows
anywhere. Dark is the only theme (the demo environment); a light variant was
deliberately skipped to keep one surface system exact.

Component metrics (button height/padding/radius, badge shape, underline tabs,
card borders, slim progress track) are translated from **shadcn new-york-v4**
(`button`, `badge`, `tabs`, `card`, `table`, `progress`) pulled from the shadcn
registry. The app is dependency-free vanilla HTML/CSS/JS with a strict
no-CDN/vendored-assets rule, so the patterns are implemented as shared CSS
classes rather than React components.

## Color

### Surfaces (graphite — one hue, lightness steps only)

| Token | Value | Use |
|---|---|---|
| `--well` | `#101216` | deepest: canvas wells, console, 3D viewport, list boxes |
| `--bg` | `#14161A` | page background |
| `--bg-raised` | `#1A1D22` | toolbars, tab rails, panel captions, status bars |
| `--surface` | `#22262C` | cards, chips, secondary buttons |
| `--surface-2` | `#282D34` | hover states on surfaces |
| `--input-bg` | `#121418` | inputs — inset *below* their surroundings |

### Ink (warm off-white, 4 tiers)

`--ink #E8E6E1` (primary) · `--ink-2 #B9B5AD` (secondary) · `--ink-3 #8A867E`
(labels/muted) · `--ink-4 #5F5B54` (faint/disabled).

### Accents

- **Copper — the one interactive accent (~10% of any screen).**
  `--copper #C9762A`, `--copper-bright #D98B3F` (hover), `--copper-text
  #E8A45C` (copper as text), `--copper-dim` (14% tint for fills), `--on-copper
  #1A0F04` (text on copper). Used for: primary buttons, active tab underline,
  selection (part card, crop marquee), focus ring, the READY banner, links.
- **Slate-teal — secondary/informational.** `--teal #4A7A78`, `--teal-text
  #7FB0AD`, `--teal-dim`. Used for: format badges, running/in-progress text,
  completed stage chips, the 3D rim light. Never for primary actions.
- **Status:** `--ok #58A66C` / `--err #E5484D` / `--warn #E2A33C`, each with a
  `-text` variant for use on dark.

### Severity ladder (functional, never decorative)

One set, used identically everywhere a severity appears (Engineering Flags,
left rails, requirement chips):

| Tier | Fill | Text on fill |
|---|---|---|
| CRITICAL | `--sev-critical #E5484D` | `#2A0507` |
| HIGH | `--sev-high #E29A3C` | `#2A1703` |
| MEDIUM | `--sev-medium #D4C14A` | `#262104` |
| LOW | `--sev-low #8FA0B2` | `#10151B` |

Red → amber → yellow → slate: distinguishable at a glance on the graphite
surfaces, and deliberately *not* the brand copper (HIGH's amber is yellower and
appears only as a filled chip + left rail, never on controls).

## Typography

- **Sans (UI text):** `--sans` — system stack (Segoe UI on the demo machine).
  No webfonts: every asset is vendored, nothing loads from a CDN.
- **Mono (technical content):** `--mono` — Cascadia/Consolas stack. Dimensions,
  file names/paths, JSON, console, token counts, stage numbers, coordinates —
  always with `font-variant-numeric: tabular-nums` for dynamic numbers.

Scale (px): `--fs-caption 11` (uppercase micro-labels) · `--fs-small 12` ·
`--fs-body 13` (dense tool default) · `--fs-control 14` (buttons/tabs) ·
`--fs-h2 16` (app title) · `--fs-stat 20` (stat values, READY banner) ·
`--fs-display 28` (reserved for hero numbers). Weights 400/500/600/700;
hierarchy comes from weight + ink tier first, size second.

## Spacing & radius

4px grid only: `--sp-1..--sp-6` = 4/8/12/16/24/32. Radius scale: `--r-sm 4`
(chips/badges) · `--r-md 6` (buttons/inputs) · `--r-lg 8` (cards/panels).

## Depth & borders

**Borders only.** `--line rgba(232,230,225,.08)` standard · `--line-soft .05`
(row separators) · `--line-strong .16` (secondary button edges) · `--ring`
(copper focus). The only box-shadow in the app is the 1px copper ring on the
selected part card. Elevation = surface lightness step, nothing else.

## Component recipes (in design-tokens.css)

| Class | Recipe |
|---|---|
| `.btn` | 36px h · 0 16px pad · r-md · 14px/500 · copper fill; `.secondary` (surface + strong border), `.ghost`, `.danger` (outline red), `.running`, sizes `.sm` 28px / `.xl` 44px; `:active` scale(.97) |
| `.ibtn` | 26px square quiet icon button |
| `.badge-c` | bordered chip, 11px/600, r-sm; tints: `.copper .teal .ok .err .warn`, `.mono` |
| `.badge-sev` | filled severity chip, 10px/700 uppercase: `.critical .high .medium .low` |
| `.input-c` | inset field: `--input-bg`, quiet border, copper focus border |
| `.tab-c` | underline tab: transparent, 2px copper underline + full-ink text when `.active`; `.sm` for sub-tabs |
| `.cap-c` | 11px/600 uppercase tracked label (panel captions) |
| `.progress-c` | 6px track (`--well`) + copper fill |
| `.console-c` | `--well` ground, 12px mono, warm-gray text |
| `.card-c` | surface + line border + r-lg |
| `.seg-c` | segmented control: inset ground, 2px padding, copper-filled active segment |

Page-specific styles in each document may **compose** these classes and bind
extra layout rules, but every color/size/spacing value must reference a token.

## Signature elements

- **Routing-sheet stage strip (Tab 3):** stage chips are auto-numbered
  `01–08` via CSS counters in mono, like operations on a shop traveler —
  done = teal, current = copper-tinted.
- **READY banner (Tab 3):** after a run, a copper-railed banner with a mono
  `N/N READY` at `--fs-stat` is the loudest element on the screen (amber
  variant when gated NOT READY, red when the run failed).
- **Copper crop marquee (Tab 1):** the cropper's selection dash, corner
  handles, and px-dimension readout are the same copper as every other
  selection in the app.
- **Machined 3D viewport (Tab 2):** graphite well background, warm key light +
  slate-teal rim light, warm-aluminum part material — the palette carried into
  the scene itself.

## Rules

1. No hex values in page styles — tokens only.
2. Copper is scarce: one primary action per view; selection + focus; the READY
   banner. Everything else is graphite + ink.
3. Severity colors mean severity — never reuse them decoratively.
4. Any dynamic number gets `--mono` + `tabular-nums`.
5. No drop shadows, no gradients, no new hues.
6. Emoji are banned in chrome (colored glyphs break the palette); use
   monochrome glyphs (▶ ✕ ⟳ ⬇ ✓ △).
