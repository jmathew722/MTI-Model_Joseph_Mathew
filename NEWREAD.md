# NEWREAD — Web UI Design Overhaul (graphite / copper / slate-teal)

Everything that changed in the MTI 2D→3D pipeline web UI design pass, in one
place: the design system, what each tab got, how it was verified, and where
the source of truth lives. Functionality is untouched — crop, orient, save,
run, cancel, and every results panel behave exactly as before; this was a
visual and structural pass for a customer-facing demo.

---

## The design system (source of truth)

| Artifact | Role |
|---|---|
| **`2D-3D-CAD-Test-Generation/webapp/static/design-tokens.css`** | The single stylesheet of tokens + shared component classes, served at `/static/design-tokens.css` and linked by **both** documents — the host app (`index.html`) and the Tab 1 cropper (`photoapp/index.html`) — so every tab literally pulls from one file. |
| **`2D-3D-CAD-Test-Generation/webapp/DESIGN.md`** | The written contract: palette rationale, type scale, spacing grid, component recipes with measurements, signature elements, and the rules (no hex in page styles, copper is scarce, severity colors are functional, tabular-nums on dynamic numbers, no shadows/gradients, no emoji in chrome). |

### Palette (not a blue SaaS theme)

- **Graphite surfaces**, one hue, lightness steps only: `#101216` wells
  (canvas / console / 3D viewport) → `#14161A` page → `#1A1D22` toolbars/rails
  → `#22262C` cards → `#282D34` hover. Inputs sit *below* their surroundings
  (`#121418`) so "type here" reads without heavy borders.
- **Copper** `#C9762A` / `#D98B3F` — the one interactive accent (Fusion
  360-style orange-on-dark): primary buttons, active-tab underline, selection,
  focus ring, the READY banner, links. Roughly 10% of any screen.
- **Slate-teal** `#4A7A78` — secondary/informational: format badges, running
  states, completed stage chips, the 3D rim light.
- **Warm off-white ink** `#E8E6E1` in four tiers (`#B9B5AD`, `#8A867E`,
  `#5F5B54`) — hierarchy comes from weight + ink tier before size.
- **Severity ladder** (identical everywhere severities appear):
  CRITICAL `#E5484D` · HIGH `#E29A3C` · MEDIUM `#D4C14A` · LOW `#8FA0B2`,
  each with a dark on-fill text color for contrast. Deliberately distinct from
  the brand copper so a HIGH flag never reads as a button.
- **Depth = borders only** (`rgba(232,230,225,.08)` and softer/stronger
  steps). No drop shadows anywhere; elevation is a surface-lightness step.
- Dark is the only theme — a light variant was skipped deliberately to keep
  one exact surface system for the demo.

### Typography & spacing

- System sans for UI (Segoe UI on the demo machine — no webfonts: the repo's
  no-CDN/vendored-assets rule holds, nothing loads externally).
- Mono (Cascadia/Consolas stack) for ALL technical content: dimensions, file
  names, paths, JSON, console, token counts, stage numbers, px readouts —
  always `tabular-nums` when the value changes.
- Real scale: 11 / 12 / 13 / 14 / 16 / 20 / 28 px, weights 400–700.
- Strict 4px spacing grid: 4 / 8 / 12 / 16 / 24 / 32.
- Radius scale: 4 (chips) / 6 (controls) / 8 (cards).

### Component patterns (from the shadcn registry)

Button, badge, tabs, card, table, and progress metrics were pulled from the
shadcn MCP registry (new-york-v4) and translated into vanilla-CSS classes in
the tokens file — `.btn` (+ `secondary/ghost/danger/running`, `sm/xl`),
`.badge-c` (+ tints), `.badge-sev`, `.input-c`, `.tab-c`, `.cap-c`,
`.progress-c`, `.console-c`, `.card-c`, `.seg-c` — because the app is
build-less vanilla HTML/CSS/JS and installing React components would have been
a functional rewrite, not a restyle.

---

## What each tab got

### Tab 1 · Drawing Crop (`webapp/photoapp/index.html`)
- Toolbar and sidebar rebuilt on the shared tokens: copper primary actions
  (Open Drawing, Queue View, Download ZIP when armed), quiet secondary/ghost
  buttons, uppercase micro-labels, mono only for technical values (file name,
  dimensions, queued view lists, status bar coordinates).
- Canvas overlay recolored to the system: copper crop marquee, corner handles
  and px-dimension readout; faint copper grid + crosshair on the graphite
  well — the same copper as every other selection in the app.
- Sidebar sits on the same ground as the canvas, separated by a quiet border
  (no "sidebar world / content world" split).

### Tab 2 · Part Setup & 3D Model (`webapp/index.html`)
- **Orientation is now a segmented control** — F / B / T / Bo / L / R / ISO
  buttons on each image card (copper-filled active segment, click again to
  unassign, tooltips carry the full names) — replacing the raw `<select>`;
  same state, same validation, same save format.
- **"Must-meet specifications"** is a first-class labeled field (uppercase
  label + grading hint + inset textarea), not an afterthought.
- Format badge and sync indicator are proper status chips from the shared
  badge component (teal = format, green = "matches Part Setup source").
- Three-panel split keeps its resizable behavior; each panel has a consistent
  uppercase caption; viewers sit on the darkest well surface.
- **STL viewer scene matches the palette**: graphite background, warm key
  light + slate-teal rim light, warm-aluminum part material, warm-neutral
  grid — no more default blue-gray Three.js scene.

### Tab 3 · Pipeline & Results (`webapp/index.html`)
- **READY banner** — new, and the most prominent element after a run: a
  copper-railed strip with mono `✓ N/N READY` at stat size, parsed from the
  pipeline summary (amber variant when a gating flag made it NOT READY, red
  when the run failed, hidden while running). Wired into both the part-run and
  demo-run completion paths.
- **Stage strip as a routing sheet**: stage chips auto-number themselves
  `01–08` in mono via CSS counters — done = teal, current = copper.
- Console/build log on the deepest well surface in 12px mono, in both the
  collapsible inline strip and the Console sub-tab.
- Sub-tabs share the exact underline-tab component with the top-level tabs.
- **Engineering Flags** severity badges + left rails bind to the `--sev-*`
  tokens; requirement chips (met/partial/unmet/not-applicable) restyled to the
  same language; Overview-Verification / Human-Requirements group headers use
  the copper rail treatment.
- **Token/Cost**: mono tabular values, uppercase labels, the this-part total
  copper-emphasized as the lead card, session total beside it.
- Emoji removed from all chrome (colored glyphs broke the palette); replaced
  with monochrome glyphs (▶ ✕ ⟳ ⬇ ✓ △).

### Server (`webapp/app.py`)
- One additive line: a `/static` mount so both documents can load the shared
  tokens file. No endpoint or pipeline behavior changed.

---

## Verification

- Served the app and screenshot-verified with headless Edge: Tab 1 (cropper +
  sidebar), Tab 2 (three-panel layout, segmented orientation cards, labeled
  spec field), Tab 3 (run controls, sub-tabs), plus injected-state captures of
  the Engineering Flags ladder (CRITICAL/HIGH/MEDIUM/LOW), the Token/Cost
  cards, and the `✓ 1/1 READY` banner.
- `python -m pytest tests/ -q` → **310 passed**.
- All endpoints (/, /photoapp/, /static/design-tokens.css, /bridge.js,
  /api/status) verified 200 on a live server.
- No old-palette hex values remain in either document (grep-verified).

## Assumptions made (worked autonomously, per instructions)

1. **shadcn as patterns, not packages** — the app has no React/build
   toolchain and a hard vendored-assets rule, so shadcn components were pulled
   from the registry and translated into shared vanilla-CSS recipes rather
   than installed; installing them would have been a feature change.
2. **Dark-only** — the light variant was skipped so the demo has one exact,
   tuned surface system (DESIGN.md notes this).
3. **HIGH severity is amber** and kept visually distinct from brand copper
   (filled chip + rail only, never on controls).
4. **System font stack** instead of a webfont, forced by the no-CDN rule.
5. **Segmented orientation control adds one interaction nicety**: clicking the
   active segment unassigns (the dropdown had an explicit "— orientation —"
   blank; the segmented control needed an equivalent).
6. The cropper's "embedded verbatim" note in the README now has one
   exception: its stylesheet (and canvas colors) — its logic, IDs, bridge
   protocol, and behavior are byte-identical.
