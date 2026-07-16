# Company Theme Analysis — MTI_ModelCodex (`agent/implement-draw2part-pipeline`)

Reference repo studied: `C:\Users\joeka\MTI-Model_New\MTI_ModelCodex`, branch
`agent/implement-draw2part-pipeline` (the "agent implementation branch"). This is the
company's desired visual identity. This document is the token inventory and the
mapping decisions used to restyle our UI (`2D-3D-CAD-Test-Generation/webapp/`). Written
before any of our code was touched.

## 1. Where the design lives

The reference keeps its entire design system in **two files** with no build step,
no framework, and **no CDN**:

- `ui/styles.css` — one hand-written stylesheet (~1,300 lines). All tokens are CSS
  custom properties in a single `:root {}` block at the top; everything else references
  them via `var()`.
- `ui/index.html` — semantic HTML: fixed `.topbar`, fixed `.tabs` nav, a `.workspace`
  main, and `.card`-based panels.

There is **no Tailwind, no SCSS, no theme JSON, and no font files**. Fonts are declared
as pure system-stack fallbacks (see §3). This matches our own no-CDN / vendored-assets
discipline exactly, so we adopt the same approach: one central token file, plain CSS
variables, system font stacks.

## 2. Color tokens (verbatim from `ui/styles.css :root`)

The palette is a **light "engineering paper" theme** with a **dark topbar** and
**dark viewer panels** — a warm off-white sheet, one strong blue accent, and muted
green/amber/red status colors.

| Token          | Value      | Role                                             |
|----------------|------------|--------------------------------------------------|
| `--ink`        | `#111318`  | Near-black. Primary text **and** the topbar fill |
| `--paper`      | `#fafaf7`  | Page background (warm off-white)                 |
| `--surface`    | `#ffffff`  | Cards, panels                                    |
| `--surface-alt`| `#f3f3ef`  | Table headers, quiet chips, inset fills          |
| `--line`       | `#d9d9d2`  | Hairline borders                                 |
| `--line-strong`| `#b8b9b4`  | Stronger borders, input outlines                 |
| `--muted`      | `#666a72`  | Secondary / label text                           |
| `--blue`       | `#1d5bdb`  | Primary accent — buttons, active tab, links, IDs |
| `--blue-dark`  | `#1548af`  | Primary button hover                             |
| `--blue-soft`  | `#e9effd`  | Selected fills, accent tints                     |
| `--green`      | `#177a4c`  | Pass / accept status                             |
| `--green-soft` | `#e9f5ef`  | Pass tint background                             |
| `--amber`      | `#b7791f`  | Flagged / hold status                            |
| `--amber-soft` | `#fbf3e4`  | Flagged tint background                          |
| `--red`        | `#c0392b`  | Fail / reject status                             |
| `--red-soft`   | `#fbecea`  | Fail tint background                             |

**Dark surfaces on the light app** (viewer chrome, used literally in the reference):
`--viewer-panel #171a20`, `--viewer-bar #1c1f25`, `--viewport #15181e`, viewer border
`#30343d`, viewer text `#f4f5f7` / `#a7abb5`, viewer control `#252930` on `#454a55`.
Overlay scrims over the drawing use `rgb(17 19 24 / ~82%)`. The topbar is `--ink` with a
`#2b2e35` bottom border and light text (`#fff` / `#969ba7` / `#aeb1b9`).

### Provider marks (informational, not part of the neutral palette)
`--provider claude #a65f3e`, `--provider openai #13785c` — small avatar chips only.

## 3. Typography

```
--sans: Inter, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
--mono: "JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace;
```

- **No web-font files or `@import`** — the reference relies on the font being present or
  the system fallback. We match this exactly (on Windows both fall back to Segoe UI /
  Consolas, i.e. identical rendering to the reference on the same machine). Keeps us
  CDN-free and reproducible.
- Sans is the workhorse; **mono is used everywhere a value is technical**: eyebrows,
  tab numbers, IDs, dimensions, costs, stage codes, timestamps.
- Scale: `h1 clamp(1.35rem, 2vw, 2rem)` with `-0.035em` tracking; `h2 1rem`; body `14px`
  / `1.45`. Micro-labels ("eyebrows") are `9px` mono, uppercase, `0.12em` tracking,
  colored `--muted`. Buttons are `11px`/`700`. Dense tables run `10px`, headers `8.5px`
  uppercase.

## 4. Shape, spacing, depth

- **Radius:** single `--radius: 8px` for cards; smaller ad-hoc radii (3–6px) on chips,
  inputs, buttons; `999px` for pills.
- **Spacing:** no numeric scale token — uses literal `px` on a loose 4/8/16/24 rhythm.
- **Depth: borders only, essentially no shadows** (one soft shadow on toasts). Elevation
  is communicated with `1px` hairlines and the light/dark surface contrast. This is
  identical in spirit to our existing "borders-only" depth strategy.
- **Layout metrics:** `--header-height: 64px`, `--tabs-height: 48px`, both fixed.

## 5. Layout & information hierarchy

- **Fixed dark topbar** (`.topbar`): left = brand (a `34px` bordered mono "brand-mark"
  glyph + stacked `DRAW2PART` / tiny mono subtitle); right = a context cluster
  (active-part `<select>` + a rounded `.status-pill`).
- **Fixed tab nav** (`.tabs`) directly under the topbar: numbered mono tab labels
  (`01 Input & Preview`), active tab = `--blue` text + a `2px` blue underline via
  `::after`. Quiet, no filled tab backgrounds.
- **`.workspace`** is a `min(1440px, 100%)` centered column padded clear of the two fixed
  bars.
- **Everything is a `.card`**: white surface, `1px --line` border, `8px` radius. Sections
  lead with an **"eyebrow"** (mono uppercase micro-label) above an `h1/h2`.
- **Dense data → `.data-table`**: sticky `--surface-alt` header row, `10px` rows,
  right-aligned mono numerics, blue clickable IDs, expandable `.row-detail` rows.
- **Status is a consistent vocabulary**: `.status-pill` (rounded, `currentColor` border),
  `.stage-dot` (filled dot, pulses when running), `.result-chip`, `.mini-status` — all
  keyed to the same neutral/pass/flagged/failed/running colors.
- Viewer panels invert to dark for contrast with linework and the STL.

## 6. Severity semantics

The reference expresses severity through the status colors (`.review-severity` uses
amber by default, red for `.critical`/`.error`). It does **not** ship a 4-level
CRITICAL/HIGH/MEDIUM/LOW ladder — our app does, and we must keep it. Per the brief we
keep the **red → orange → yellow → blue** ordering but re-shade to harmonize with the
company palette on a light background (readable, muted, professional):

| Level    | Token             | Shade      | Rationale                              |
|----------|-------------------|------------|----------------------------------------|
| CRITICAL | `--sev-critical`  | `#c0392b`  | = company `--red`                      |
| HIGH     | `--sev-high`      | `#c2410c`  | Burnt orange, distinct from amber      |
| MEDIUM   | `--sev-medium`    | `#a16207`  | Dark gold (yellow family), readable    |
| LOW      | `--sev-low`       | `#3f5bb0`  | Calm blue, below the accent in salience|

Each gets a `-soft` tint background token so the outlined "inspection-stamp" stamps read
on the light theme without hardcoded `rgba()`.

## 7. Mapping decisions for our restyle

Our webapp is already **fully token-driven**: `webapp/index.html` and
`webapp/photoapp/index.html` both bind every value to `webapp/static/design-tokens.css`.
That makes the restyle a **token re-point**, not a rewrite. Decisions:

1. **New single source of truth: `static/theme.css`.** It holds every company-derived
   token, exposed under **both** the reference's semantic names (`--paper`, `--surface`,
   `--blue`, …) **and** our existing variable names (`--bg`, `--bg-raised`, `--surface`,
   `--blueprint`, `--ok`, `--sev-*`, …) as aliases. Because our ~1,300 lines of existing
   CSS already reference the legacy names, re-pointing the aliases flips the whole app to
   the company light theme with zero markup churn.
2. **`design-tokens.css` keeps only the shared component recipes + base resets;** its old
   dark `:root` token block moves into `theme.css`. Both HTML docs load `theme.css`
   first, then `design-tokens.css`.
3. **Accent flips direction for a light theme:** our old "hover = brighter" becomes
   "hover = `--blue-dark`"; accent-as-text (`--blueprint-text`) becomes the readable
   `--blue` on white; `--on-blueprint` (text on the accent fill) becomes `#fff`.
4. **Viewers/console/canvas stay dark** via new `--viewer-*` / `--console-*` tokens
   (faithful to the reference's dark viewer panels and to both apps' "the drawing area is
   dark" convention). The handful of selectors that used `--well` for a genuine dark
   surface point at these; `--well` itself becomes a light inset color for chips/tracks.
5. **The header becomes the dark topbar** (`--topbar-*` tokens), keeping our drafting
   "title-block" data-cell motif but as light-on-dark cells — the closest analogue to the
   reference's dark topbar + part-selector + status-pill cluster. Our tab/sub-tab
   underline rail already matches the reference pattern and needs only the re-pointed
   accent.
6. **Overlay scrims** drawn over the (now light) drawing image stay dark via `--scrim` /
   `--scrim-strong` tokens so white marker/label text stays legible over any drawing.
7. **Fonts** adopt the reference stacks verbatim (Inter / JetBrains Mono with system
   fallbacks) — no files, no CDN.

Result: the company's colors, fonts, spacing, radius, and status vocabulary drive every
surface, while our tab-based workflow, drafting motifs, and the split-panel Three.js
viewer are preserved and merely re-chromed.
