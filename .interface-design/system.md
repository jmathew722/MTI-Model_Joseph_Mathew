# Interface Design System — MTI 2D→3D Pipeline UI

Full spec lives in `2D-3D-CAD-Test-Generation/webapp/DESIGN.md`; tokens in
`webapp/static/design-tokens.css` (shared by index.html + photoapp). This file
is the session-to-session summary.

## Direction

**BLUEPRINT ROOM** — the digital drawing office. Deep Prussian-navy surfaces
(cyanotype at night), cool drafting-white ink, ONE blueprint-cyan accent,
inspection-stamp semantics, mono numerals for every measurement. Dark only.

## Core decisions

- **Depth:** borders only (hairline cool rgba); elevation = surface lightness
  step. Sanctioned exceptions: 1px cyan ring on selected part card, soft glow
  on API status dot.
- **Spacing:** 4px grid (`--sp-1..6` = 4/8/12/16/24/32); dense workbench
  (12–16px paddings).
- **Radius:** crisp 2/4/6 (`--r-sm/md/lg`).
- **Type:** system Segoe stack + Cascadia mono; scale 11/12/13/14/16/20/30;
  hierarchy from weight + ink tier first, size second; `tabular-nums` on all
  dynamic numbers.
- **Surfaces:** `#0A0F16 well · #0D131C bg · #111927 raised · #16202F surface ·
  #1B2738 hover · #090E15 input(inset)`. Ink `#E7EDF5/#AEBACB/#7D8A9E/#4E5A6C`.
- **Accent:** blueprint cyan `#3FA9D4` (bright `#5FBFE4`, text `#7ECFEE`,
  dim 12%); secondary steel `#5C7690`. Legacy aliases `--copper*`→blueprint,
  `--teal*`→steel keep photoapp untouched.
- **Severity:** OUTLINED inspection stamps (color border + 8–10% tint), never
  filled: CRITICAL `#F0545C` · HIGH `#E3A13C` · MEDIUM `#D2C355` · LOW `#8DA4BE`.

## Signature elements (point to these, keep them)

1. Title-block header: brand left + hairline-bordered data cells (micro-label
   over mono value) + drafting-ruler tick strip on the bottom edge.
2. Sheet tabs: mono `SHEET n` plate, cyan-lit when active.
3. Centerline drag divider: long-dash/short-dash `--centerline-v` pattern.
4. Traveler stage chips: CSS-counter `OP 01 ·` prefix, done = steel + ✓.
5. Verdict stamp: post-run READY/NOT READY as a bordered mono uppercase stamp
   on a status-tinted strip.

## Component metrics

`.btn` 36h/16px pad/r-4/14px-600 cyan · `.xl` 46h/700 · `.sm` 28h ·
`.ibtn` 26sq · `.badge-c` bordered 11/600 chip · `.badge-sev` outlined stamp
10/700 tracked · `.input-c` inset + cyan focus border · `.tab-c` 2px cyan
underline · `.seg-c` inset segmented, cyan active.

## Gotchas

- `#src-img`/`#ov-img` MUST keep their hidden state as an INLINE style
  attribute (`style="display:none"`), never in the stylesheet — the JS reveals
  them with `style.display=''`.
- Three.js scene colors are hardcoded in index.html (bg 0x0a0f16, cyan rim
  0x3fa9d4, grid 0x223140/0x141d29, aluminum 0xa9b8c6) — keep in sync with
  tokens.
- photoapp canvas literals: fill `#0A0F16`, marquee `#5FBFE4`.
- No CDN/webfonts ever (vendored-assets guarantee); pure-ASCII .ps1 files.
