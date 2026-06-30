---
description: Run the MTI 2D→3D pipeline end-to-end on a test folder and report results
---

Run the MTI 2D→3D SolidWorks pipeline end-to-end on the test folder: $ARGUMENTS

If $ARGUMENTS is empty, ask me which folder (e.g. `Test2`) before proceeding.

Do exactly this, in order, and report faithfully (state failures plainly):

1. Preflight: from `2D-3D-CAD-Test-Generation`, confirm `.env` has an
   ANTHROPIC_API_KEY (do NOT print it) and that the folder exists with one
   subfolder per part (each holding front/side view images).

2. Run the pipeline (auto-resolves via Stage 2.5, verifies, generates macros,
   builds a `.sldprt` per part when SolidWorks is available, logs tokens, and
   copies deliverables to `~/Downloads/SolidWorksModel_Parts`). Run it in the
   background if SolidWorks launches; wait for it to finish. Keep DEFAULT flags
   (do NOT pass --no-resolve / --strict-gate / --no-sldprt / --no-export):

       cd 2D-3D-CAD-Test-Generation
       python main.py --views-folder ../$ARGUMENTS --output ../$ARGUMENTS/output

3. Verify completeness:
   - Confirm "N/N READY" was printed.
   - Confirm NO macro contains a dead "GENERATION ISSUE" no-op
     (grep ../$ARGUMENTS/output/*/macros/*.vba).
   - For each part, note from `<part>_model_check.txt` any features the `.sldprt`
     skipped, with the reason.

4. Collect Stage 2.5 flags: from each `<part>_build_plan.json` read
   `resolution_summary` and every MEDIUM/LOW/CRITICAL flag.

5. Tidy + package: delete `~$*.SLDPRT` and `AUTOSAVE_*.SLDPRT` from the output and
   from `~/Downloads/SolidWorksModel_Parts`, then zip the deliverable:

       Compress-Archive -Path "$env:USERPROFILE\Downloads\SolidWorksModel_Parts\*" -DestinationPath "$env:USERPROFILE\Downloads\SolidWorksModel_Parts.zip" -Force

6. Report: a per-part table (status | readiness % | macros | features built/total |
   skipped + reasons), total API cost this run and to date (from
   `token_usage_log.txt`), a bulleted list of every CRITICAL/LOW/MEDIUM assumption
   to verify (grouped by part), and where the deliverables + zip landed.

Rules: never block on ambiguity (the pipeline resolves and builds with annotated
assumptions — surface them, don't stop); if a feature can't build in the `.sldprt`,
say which/why and that its macro still builds it; never claim a model is complete
when `model_check` lists skips; do not commit or push unless I explicitly ask.
