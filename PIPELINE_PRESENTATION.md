# MTI 2D → 3D SolidWorks Pipeline
### A walkthrough of the workflow, for MTI Engineering

> **In one sentence:** This system takes a folder of 2D engineering drawings, reads them with
> Claude Vision, resolves anything the drawing leaves ambiguous, and produces a real SolidWorks
> `.sldprt` part **plus** a portable set of VBA macros that rebuild that part on any SolidWorks
> machine — with every assumption flagged and the API cost logged.

---

## 1. The core idea

The pipeline is built around one engineering directive, stated throughout the code and docs:

> **A complete approximate model is the correct outcome. An incomplete model is the wrong outcome.**

So the pipeline **never refuses to build** because a dimension was unclear. Instead it resolves
each ambiguity to a defensible number, tags it with a confidence tier, and tells you exactly what
it assumed. The engineer's job shifts from *"model it from scratch"* to *"review the flagged
assumptions and confirm them against the drawing."*

---

## 2. The workflow at a glance

```
  drawings
     │
     ▼
 [1] image prep ──────────► clean PNG (any OS)
     │
     ▼
 [2] Claude Vision extract ► structured JSON: every dim, hole, view, tolerance, feature
     │                       (the only paid step — cached aggressively)
     ▼
 [2.5] resolve ───────────► every ambiguity → a number + confidence tier (the "chief-engineer pass")
     │
     ▼
 [3] verify ──────────────► dimensional closure, envelopes, readiness % (advisory by default)
     │
     ▼
 [4] build ───────────────► VBA macros + build_plan.json  ──► .sldprt (SolidWorks COM, when available)
     │
     ▼
   token ledger  ──►  copy all deliverables to ~/Downloads/SolidWorksModel_Parts
```

**Result of one run:** for every part — a `.sldprt`, a numbered macro package, a verification
report, a self-contained build plan, an assumption ledger, and a token-cost log.

---

## 3. The stages in detail

### Stage 1 — Image prep · `utils/image_prep.py` · *any OS*
Rasterizes a PDF page or normalizes an image into a clean PNG, downscales to a max long edge
(default **2576 px**, tunable via `MAX_IMAGE_LONG_EDGE`), and emits warnings (e.g. "image appears
nearly blank") that follow through to the final report.

### Stage 2 — Extraction · `pipeline/extractor.py` · *any OS* · **the only paid step**
- **Model:** `claude-sonnet-5` (override with env `EXTRACTION_MODEL`).
- **Method:** a *forced tool call* validated against a Pydantic schema, with one repair retry.
- **Multi-view:** all views of a part go to Claude in **one** call, labeled by view, so each
  feature's sketch plane is tied to the view it was read in — and a feature seen in two views is
  not double-counted.
- **Cost controls** (important for batches):
  - System prompt + tool schema (~4.6k tokens) and the image carry `cache_control` → later calls
    in a batch read the prefix from cache (~10% of cost).
  - An on-disk **extraction cache** (`<output>/.extraction_cache`) returns an identical
    image+model result with **zero** API calls. *This is why you keep `--output` stable — re-runs
    become free.*
  - A low-confidence re-query fires only when something specific was flagged to re-examine.

### Stage 2.5 — Resolution (the "chief-engineer pass") · `pipeline/resolver.py` · *any OS*
**The heart of the system.** Walks every value flagged unclear / under-dimensioned / unknown-position
and runs a deterministic decision tree, assigning a confidence tier:

| Step | Logic | Tier |
|---|---|---|
| 1. Arithmetic chain | the only reading that closes a dimension chain within tolerance | **HIGH** |
| 2. Geometric validity | eliminate readings that can't physically fit (wall thickness, cut ≤ solid) | **MEDIUM** |
| 3. Conservative geometry | among survivors, prefer the smallest / shallowest | **LOW** |
| 4. Last resort | derive from an adjacent dim, default depth → through-all, radius → general tol, or center on parent | **CRITICAL** |

> **Key guarantee:** numbers are *chosen from what was extracted* (candidate readings, chains,
> adjacent dimensions) — **never fabricated**. Every dimension ends with a numeric
> `resolved_value`; every feature is marked `build_status: build`; each assumption carries a
> basis, confidence, tier, and a plain-English `human_note`.

### Stage 3 — Verification · `pipeline/validator.py` · *any OS*
Checks dimensional closure, unit consistency, view consistency, and feature feasibility, and
computes a **drawing-completeness score** (geometry / dimension / consistency / feature-confidence
→ an overall *macro readiness %*).

- **Advisory by default** — issues are reported, but Stage 2.5 already resolved them, so the build
  proceeds with annotated assumptions.
- `--strict-gate` (or `--no-resolve`) restores the old behavior where a failing verification
  **blocks** the run.
- `MACRO_READINESS_THRESHOLD` (e.g. `0.95`) can hard-gate low-readiness drawings.

### Stage 4 — Build · `pipeline/macro_generator.py` (+ `solidworks_builder.py`)
Two engines:
- **`vba` (default, any OS)** — writes the portable macro package. Copy the folder to *any*
  SolidWorks 2024 machine (e.g. a VDI) and run the macros; **no Python needed there.**
- **`com` (Windows + SW 2024)** — drives SolidWorks directly to produce the `.sldprt`.

In the normal flow **both** happen when SolidWorks is present: macros are always written, and if
SolidWorks is reachable over COM the `.sldprt` is built too. Off Windows you still get macros +
reports and a printed reason the `.sldprt` was skipped.

**Macro discipline:** one macro per feature · named features · per-step PASS/FAIL logging ·
stop-on-first-failure · fillets/chamfers emitted last (interactive edge selection with drawing
values baked in).

**Prohibited features** — loft, sweep, boundary, shell, draft, surfacing, helical threads are
**never generated**; they are flagged in the build plan and skipped. Threads are cosmetic only.

---

## 4. Module map

| Stage | Module | Runs on |
|---|---|---|
| Image prep | `utils/image_prep.py` | any OS |
| Extraction | `pipeline/extractor.py` (`claude-sonnet-5`, forced tool call) | any OS |
| Schema | `pipeline/schema.py` (Pydantic v2: views, holes, relationships, ambiguity) | any OS |
| **Ambiguity resolution** | `pipeline/resolver.py` (resolved_value + tier per dim; never blocks) | any OS |
| Verification | `pipeline/validator.py` (closure, envelopes, readiness report) | any OS |
| **VBA macros** | `pipeline/macro_generator.py` | any OS |
| Macro audit | `pipeline/macro_audit.py` (static check *before* writing) | any OS |
| COM build | `pipeline/solidworks_builder.py` | Windows + SW 2024 |
| Model check | `pipeline/model_validator.py` | Windows + SW 2024 |
| Token ledger | `pipeline/usage_log.py` | any OS |
| Orchestrator | `main.py` | any OS |

---

## 5. How to run it

**One-time setup**
```powershell
cd 2D-3D-CAD-Test-Generation
python setup.py                      # checks Python, installs deps, creates .env
# then edit .env:  ANTHROPIC_API_KEY=sk-ant-...
```

**The command you use every time** (multi-view batch — each subfolder is a part)
```powershell
python main.py --views-folder ..\Test2 --output ..\Test2\output
```
One command: resolves → verifies → writes macros + `.sldprt` → logs tokens → copies everything to
`~/Downloads/SolidWorksModel_Parts` → prints a summary table → ends with `N/N READY`.

**Input layout** — the *front* view is the only required view; the rest are optional and detected
from filename keywords:
```
Test2/
└── PART-A/
    ├── PART-A_front_view.png    (required → base profile + extrude depth)
    ├── PART-A_side_view.png     (optional)
    └── PART-A_top_view.png      (optional)
```

**Flags worth knowing**

| Flag | Effect |
|---|---|
| `--views-folder DIR` | multi-view; each subfolder = a part (the normal batch mode) |
| `--drawing FILE` | a single drawing (PDF/PNG/JPG/TIFF) |
| `--batch DIR` | a flat folder of drawings / `*_extraction.json` |
| `--from-json FILE` | rebuild from a saved extraction — **no API cost** |
| `--output DIR` | keep stable to reuse the cache (free re-runs) |
| `--no-resolve` | skip Stage 2.5 (legacy blocking behavior) |
| `--strict-gate` | block on failing verification instead of building with assumptions |
| `--no-sldprt` | macros + reports only |
| `--no-export` | don't copy to Downloads |
| `--no-extract-cache` | force a fresh extraction (re-spends tokens) |

---

## 6. The outputs

Per part, under `output/<Part>/`:

```
<Part>.SLDPRT                    ← the 3D model (when SolidWorks is available)
<Part>_model_check.txt           ← mass/bbox validation + any skipped features
<Part>_extraction.json           ← RAW Claude extraction, verbatim
<Part>_resolved_extraction.json  ← Stage 2.5: resolved_value + flag tier per dim
<Part>_verification_report.txt   ← READY/BLOCKED + completeness score
<Part>_build_plan.json           ← self-contained build steps (the source of truth)
<Part>_audit_report.json         ← static self-validation of the macros
macros/                          ← 00_setup … ZZ_final_verify, RUN_ALL.vba, README.md
logs/                            ← build_log.txt appended by the macros at run time
```

Plus at the output root: `multiview_summary.csv` (triage), `token_usage_log.txt` (running cost),
and the internal `.extraction_cache/` (not exported).

### How to read the results — in this order

1. **`multiview_summary.csv` / the console table** — per part: status (READY / BLOCKED / ERROR),
   readiness %, macro count, # needing review, # skipped. Your triage view.
   > Real example from this repo:
   > `A040921E … BLOCKED 0.83 … Dimensional closure FAILED: D004 = D005 → 0.75 != 3` — the
   > drawing's own numbers don't add up, flagged honestly rather than silently built.

2. **`<Part>_build_plan.json` → `resolution_summary`** — the assumption ledger and the
   **self-contained source of truth**. Every step lists its dimensions in *both* drawing units and
   meters, its `flags[]`, and per-step `assumption_made` / `flag_tier`. A macro can be rebuilt from
   this file alone.
   > Real example (`115_C-RevB`): main body block → `POSITION ASSUMED`; corner holes →
   > `HOLE POSITIONS ASSUMED (centered on the plate envelope)`; ground-flat finish →
   > `skipped_prohibited` (shell); NPTF thread → `needs_review` (cosmetic).

3. **`<Part>_model_check.txt`** — if the auto-`.sldprt` skipped a feature (interactive fillet,
   degenerate hole), it's listed here with the reason. The **macros still build those features**
   even when the auto-build skipped them.

### Running the macro package (on the SolidWorks machine)
1. Copy the `<Part>/` folder over (with `macros/` and `logs/`).
2. SolidWorks → **Tools ▸ Macro ▸ New**, paste `00_setup.vba`, press **F5**.
3. Run each numbered macro **in order**, stopping on the first failure.
4. Finish with `ZZ_final_verify.vba` (rebuild, mass properties, bounding-box vs. drawing envelope).

Each macro logs PASS/FAIL with a bounding-box readback to `logs/build_log.txt`. `00_setup`
auto-discovers a Part template if none is configured (handy on fresh VDI installs).

---

## 7. Engineering safeguards

- **Units are traceable.** SolidWorks API is in meters. The COM path runs every value through
  `to_meters()` + `assert_meters()`. The VBA path writes every value as `<drawing value> *
  UNIT_FACTOR`, so the original drawing number stays visible in the code.
- **Assumption flags surface inside the macros, by tier:**
  HIGH → a `' NOTE` comment · MEDIUM → `MsgBox` info · LOW → `MsgBox` exclamation ·
  **CRITICAL → a banner + a confirmation dialog the operator must acknowledge** (Cancel logs and
  stops the macro). You cannot blow past a critical assumption silently.
- **A paid extraction is never lost.** The raw `_extraction.json` is written for every run, READY
  or BLOCKED. Patch it and rebuild with `--from-json` at **zero API cost**.
- **Macros are audited before they're written.** Banned/nonexistent SolidWorks APIs and structural
  defects fail generation outright — no macro that explodes inside SolidWorks ever ships.
- **The `.sldprt` build is non-strict.** A fragile feature (e.g. a fillet with no selectable edge)
  is skipped and recorded in `_model_check.txt` rather than failing the whole part.

---

## 8. Limitations — what an engineer must still verify

- **Positions are only as good as the callouts.** When a feature isn't dimensioned from an origin,
  Stage 2.5 *centers it* and flags `POSITION ASSUMED` (tier LOW). Verify placement before trusting it.
- **A CRITICAL value is a defensible default, not a confirmed reading.** Always review
  `resolution_summary` and any MEDIUM / LOW / CRITICAL flags.
- **Revolves and feature-level patterns** are emitted as TODO-marked skeletons (`needs_review`),
  not guessed API calls.
- **The COM `.sldprt` path and `ZZ_final_verify`** are exercised only on Windows + SolidWorks 2024.
- **No checkpoint resume** on the COM path (partial-save + auto-save only).

---

## 9. Takeaways for the team

- **One command, full deliverable.** Drawings in → `.sldprt` + portable macros + reports out, in
  one well-known place.
- **It never blocks; it annotates.** Ambiguity becomes a flagged assumption, not a dead end.
- **The engineer reviews, not re-models.** Read the summary → build-plan flags → model_check, in
  that order; confirm positions and anything CRITICAL against the original drawing before release.
- **Cost is controlled and visible.** Aggressive caching + a token ledger; re-runs from cache or
  `--from-json` are free.

---

*Source: `2D-3D-CAD-Test-Generation/`. Operator guide → `README.md`. Deep technical doc →
`2D-3D-CAD-Test-Generation/README.md`.*
