# Reusable Run Prompt — 2D → 3D SolidWorks pipeline

Two ways to run the updated pipeline: the **Web UI** (interactive, one part at a
time, with a live console and a 3D STL viewer) or the **CLI / agent prompt**
(scriptable, whole-folder batch). Both drive the same `main.py`.

---

## A. Web UI (interactive)

```bash
cd 2D-3D-CAD-Test-Generation/webapp
./run.sh                 # http://127.0.0.1:8092  (creates venv, installs deps)
```

Then, in the browser:

1. **Tab 1 — Image Preprocessing:** open a PDF/image in the embedded DrawingCrop
   tool, draw bounding boxes, and **Queue View** for each (name them
   front/top/side/left/bottom so the pipeline classifies them). DWG is not
   supported — use PDF or an image.
2. Click **➕ Add current part**. Load the next drawing and add it too, to build a
   **part list**.
3. **Select one part** (card highlights), then click **▶ Run Pipeline**. It runs
   scoped to that single part: `main.py --views-folder <part> --output <part>/output`.
   Watch the live console; the button shows **Running…** then a success/failure
   message.
4. Read the outputs in the tabs: **Extraction / Resolved / Build Plan /
   Verification / Model Check / VBA Macros / Console**, and inspect the model in
   **Tab 2 — Drawing vs 3D Model** (source drawing + interactive STL viewer).

Notes:
- Live extraction needs `ANTHROPIC_API_KEY` in `2D-3D-CAD-Test-Generation/.env`.
  Without a key, use **▶ Run demo** to populate the tabs from a saved extraction.
- On macOS/Linux (no SolidWorks) the `.sldprt`, `.stl`, and Model Check are not
  produced; run the generated `ZZZ_export_stl.vba` (or `RUN_ALL.vba`) on a
  SolidWorks machine to get the model and STL. The 3D viewer loads the STL
  automatically once it exists.

---

## B. CLI / agent prompt (batch a test folder)

Paste the block below into Claude Code (or any agent in this repo). Replace
`<<FOLDER>>` with the test folder (subfolders are parts, each holding front/side
view images — like `Test2`). It reproduces the end-to-end flow: run → resolve →
build `.sldprt` → export `.stl` → log tokens → export to Downloads → zip → verify
→ report.

> Tip: save the prompt block as `.claude/commands/run-test.md` and invoke it as
> `/run-test <<FOLDER>>`.

```
Run the MTI 2D→3D SolidWorks pipeline end-to-end on the test folder: <<FOLDER>>

Do exactly this, in order, and report faithfully (state failures plainly):

1. Preflight: from `2D-3D-CAD-Test-Generation`, confirm `.env` has an
   ANTHROPIC_API_KEY and that the folder <<FOLDER>> exists with one subfolder per
   part (each containing front/side view images). Do NOT print the API key.

2. Run the pipeline (auto-resolves via Stage 2.5, verifies, generates macros,
   builds a .sldprt AND exports a .stl per part when SolidWorks is available, logs
   tokens, and copies deliverables to ~/Downloads/SolidWorksModel_Parts):

       cd 2D-3D-CAD-Test-Generation
       python main.py --views-folder <<FOLDER>> --output <<FOLDER>>/output

   Run it in the background if SolidWorks launches (it can take minutes); wait for
   it to finish. Keep the default flags — do NOT pass --no-resolve, --strict-gate,
   --no-sldprt, or --no-export. Extractions should be cache hits ($0) on a re-run.

   To process exactly ONE part (mirrors the Web UI's per-part Run), point
   --views-folder at that single part's subfolder:
       python main.py --views-folder <<FOLDER>>/<PART> --output <<FOLDER>>/<PART>/output

3. Verify completeness:
   - Confirm the run printed "N/N READY".
   - Confirm NO generated macro contains a dead "GENERATION ISSUE" no-op
     (grep <<FOLDER>>/output/*/macros/*.vba).
   - Confirm each part folder has a <part>.sldprt AND a <part>.stl (SolidWorks
     runs only). For macro-only runs, confirm macros/ZZZ_export_stl.vba exists.
   - For each part, note from <part>_model_check.txt any features the .sldprt
     build skipped, with the reason.

4. Collect the Stage 2.5 flags: from each <part>_build_plan.json read
   resolution_summary plus every MEDIUM/LOW/CRITICAL flag (the engineering
   assumptions a human must verify).

5. Tidy + package: delete SolidWorks junk (`~$*.SLDPRT`, `AUTOSAVE_*.SLDPRT`) from
   <<FOLDER>>/output and ~/Downloads/SolidWorksModel_Parts, then zip the
   deliverable:

       Compress-Archive -Path "$env:USERPROFILE\Downloads\SolidWorksModel_Parts\*" -DestinationPath "$env:USERPROFILE\Downloads\SolidWorksModel_Parts.zip" -Force

6. Report a concise summary:
   - A table: part | status | readiness % | macros | features built in .sldprt /
     total | features skipped (with reasons) | .stl exported (y/n).
   - Total API cost this run + to date (from <<FOLDER>>/output/token_usage_log.txt).
   - A bulleted list of every CRITICAL/LOW/MEDIUM assumption flag, grouped by part.
   - Where the deliverables landed (folder + zip path).

Rules:
- Never block on ambiguity — the pipeline resolves and builds with annotated
  assumptions; surface them, don't stop.
- If a feature can't build in the .sldprt, say which, why, and that its macro
  still builds it. Do not claim a model is complete when model_check lists skips.
- Do not commit or push unless I explicitly ask.
```

---

## Pure-CLI version (no agent)

```powershell
cd 2D-3D-CAD-Test-Generation
python main.py --views-folder ..\<<FOLDER>> --output ..\<<FOLDER>>\output
# optional bundle:
Compress-Archive -Path "$env:USERPROFILE\Downloads\SolidWorksModel_Parts\*" `
  -DestinationPath "$env:USERPROFILE\Downloads\SolidWorksModel_Parts.zip" -Force
```

Re-runs are free (extraction cache) as long as `--output` stays the same and the
images don't change. To force a fresh, paid extraction add `--no-extract-cache`.
On a SolidWorks-enabled machine each part produces both `<part>.sldprt` and
`<part>.stl`.
