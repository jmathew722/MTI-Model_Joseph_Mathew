# Tab-3 Visual Summary Tables — 2026-07-12

Two compact, scannable tables plus a part-header strip now sit **above** the
file-output dock on the Run Outputs sheet, so the two questions asked first —
"what did the pipeline find on the drawing?" and "what is it going to build, in
what order?" — are answerable without reading JSON or paragraph reports. Purely
a presentation layer over artifacts already on disk; no pipeline stage runs and
no new computation is added.

## What was added

**Server-side view-model builder — `pipeline/summary_view.py`.** One pure
function, `build_summary(output_dir)`, assembles the entire view-model from the
part's `output/` directory. All formatting rules live here, in one place, so the
browser never sees raw JSON numbers or meters:

* numbers are drawing-style — trailing zeros trimmed, leading zero dropped below
  magnitude 1 (`.105`, not `0.1050`); meters never surface;
* diameters carry the `⌀` convention, positions are `(x, y)` pairs;
* an absent value is the em dash `—`, never `null`/`""` leaking to the UI.

It degrades gracefully: a missing artifact renders its columns pending rather
than raising (notably `*_feature_verification.json`, which the pipeline does not
always produce — its verdict column simply reads pending). The artifact filename
PREFIX is discovered from disk (`A001581E/` holds `158-C_*` files), never derived
from the folder name. Dispositions prefer the standalone
`*_build_dispositions.json`, falling back to the copy embedded in
`build_plan.json`. The envelope is pulled from dimensions by `applies_to`
(accepting both `width` and `height` for the in-plane second axis), with the
base-solid step's `values_used` as a fallback.

**Endpoint — `GET /api/parts/{session}/{part}/summary`** (`webapp/app.py`). A
thin wrapper: locates the part's `output/` dir and returns `build_summary(...)`.
Never 500s on an unbuilt part (`ran: False`).

**UI — `webapp/index.html`.** A collapsible "Visual Summary" band with:

* a **header strip** — part, envelope (W × H × T), feature counts by type, flag
  counts by severity (reusing the `--sev-*` ladder), final READY status, and a
  `?N` affix when there are open assist questions;
* **Table 1 — Extracted Features & Dimensions**: ID · Type · Size (⌀/W×H×D) ·
  Position · Basis · Qty · Status; a collapsed "Notes & references" subsection
  holds non-geometric items (material/finish/tolerance, reference balloons);
* **Table 2 — Build Plan**, in build order: Step · Feature · Stage · Operation ·
  Key values · Placement · Result (disposition ⊕ verification verdict).

Each row expands (click) to a detail block — full dimension list with
tolerances, candidate readings + confidence when below 1.0, position-basis datum
chain, flags, macro filename, values used, per-check verification. Feature id is
the shared key: hovering a row cross-highlights its twin in the other table.
Column headers sort client-side (default = extraction order / build order,
restorable). At narrow widths the tables drop to their four essential columns
(the rest stay in the expansion) — never horizontal scroll. A `⎙ Print` button
expands every row and opens a clean printable view (first `@media print` block in
the app). A pending-question `?` affix jumps to the existing "Assist Needed"
section (no duplicate question UI). Styling reuses the existing design system
only — `.badge-c`/`.badge-sev`, `.files-table`/`.ovan`/`.twist` idioms, the
`--sev-*`/`--surface`/`--line`/`--mono` tokens; no new visual language, no
shadows, and the reserved `--explain*` periwinkle is untouched.

Integration is one hook: the summary repaints from `fillOutputs()` and clears in
`resetOutputs()`, so it follows part-select, run-switch, and rerun-complete
uniformly. The existing outputs/dock section is unchanged and the READY-banner
contract is untouched.

## Tests

`tests/test_summary_view.py` (29 tests) runs against frozen copies of REAL
golden-part artifacts under `tests/fixtures/summary/` (158-C, 127-C / A001271E,
M_121-B / A001641E — `A001211E` does not exist on disk). Covers the formatting
rules (trailing zeros, `⌀`, drawing-style leading zero, dashes), the merged
result logic, graceful degradation (empty dir, resolved-only, missing
verification), and the acceptance criteria: for 158-C a reader gets the notch's
`1.62 × 1.88` at `(1.56, 4.37)` basis Extracted, the six `⌀.218` holes with
placements, and each build step's stage/operation/result — all without opening
JSON; a flagged/excluded feature is red and a derived one amber; no
null/empty/"null" ever reaches a displayed cell.

Full suite: 663 passing (634 prior + 29 new), zero regressions.
