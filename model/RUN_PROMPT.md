# Reusable Run Prompt — full pipeline on a test folder

Paste the prompt below into Claude Code (or any agent in this repo) at the start
of a test run. Replace `<<FOLDER>>` with the test folder (a folder whose
subfolders are parts, each holding front/side view images — like `Test2`). It
reproduces the exact end-to-end flow: run → resolve → build `.sldprt` → log tokens
→ export to Downloads → zip → verify → report.

> Tip: to make this a slash command, save the prompt block as
> `.claude/commands/run-test.md` and invoke it as `/run-test <<FOLDER>>`.

---

## Prompt (copy everything in the block)

```
Run the MTI 2D→3D SolidWorks pipeline end-to-end on the test folder: <<FOLDER>>

Do exactly this, in order, and report faithfully (state failures plainly):

1. Preflight: from `2D-3D-CAD-Test-Generation`, confirm `.env` has an
   ANTHROPIC_API_KEY and that the folder <<FOLDER>> exists with one subfolder per
   part (each containing front/side view images). Do NOT print the API key.

2. Run the pipeline (this auto-resolves via Stage 2.5, verifies, generates macros,
   builds a .sldprt per part if SolidWorks is available, logs tokens, and copies
   deliverables to ~/Downloads/SolidWorksModel_Parts):

       cd 2D-3D-CAD-Test-Generation
       python main.py --views-folder <<FOLDER>> --output <<FOLDER>>/output

   Run it in the background if SolidWorks launches (it can take minutes); wait for
   it to finish. Keep the default flags — do NOT pass --no-resolve, --strict-gate,
   --no-sldprt, or --no-export. Extractions should be cache hits ($0) on a re-run;
   that's expected.

3. Verify completeness:
   - Confirm the run printed "N/N READY".
   - Confirm NO generated macro contains a dead "GENERATION ISSUE" no-op
     (grep <<FOLDER>>/output/*/macros/*.vba).
   - For each part, note from <part>_model_check.txt any features the .sldprt
     build skipped, with the reason.

4. Collect the Stage 2.5 flags: from each <part>_build_plan.json read
   resolution_summary plus every MEDIUM/LOW/CRITICAL flag (these are the
   engineering assumptions a human must verify).

5. Tidy + package: delete SolidWorks junk (`~$*.SLDPRT`, `AUTOSAVE_*.SLDPRT`) from
   <<FOLDER>>/output and ~/Downloads/SolidWorksModel_Parts, then zip the
   deliverable:

       Compress-Archive -Path "$env:USERPROFILE\Downloads\SolidWorksModel_Parts\*" -DestinationPath "$env:USERPROFILE\Downloads\SolidWorksModel_Parts.zip" -Force

6. Report a concise summary:
   - A table: part | status | readiness % | macros | features built in .sldprt /
     total | features skipped (with reasons).
   - Total API cost this run + to date (from <<FOLDER>>/output/token_usage_log.txt).
   - A bulleted list of every CRITICAL/LOW/MEDIUM assumption flag, grouped by part,
     so I know exactly what to verify in SolidWorks.
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

If you just want the deterministic commands:

```powershell
cd 2D-3D-CAD-Test-Generation
python main.py --views-folder ..\<<FOLDER>> --output ..\<<FOLDER>>\output
# optional bundle:
Compress-Archive -Path "$env:USERPROFILE\Downloads\SolidWorksModel_Parts\*" `
  -DestinationPath "$env:USERPROFILE\Downloads\SolidWorksModel_Parts.zip" -Force
```

Re-runs are free (extraction cache) as long as `--output` stays the same and the
images don't change. To force a fresh, paid extraction add `--no-extract-cache`.
