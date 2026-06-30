# Running these macros on the SolidWorks machine

These macros build the part **in order**. No Python needed — just SolidWorks.

## Fastest: one-click `RUN_ALL.vba`

For a single-run build, paste **`RUN_ALL.vba`** into a new macro (Alt+F11) and
press **F5 once**. It runs every step in build order with the same per-step
PASS/FAIL logging to `../logs/build_log.txt`; a failing step stops the run and
reports which step failed. Fillets/chamfers (if any) still need the interactive
edge-selection step afterwards — see step 6 below. If anything fails, fall back to
the numbered macros to isolate the step.

## Step-by-step (numbered macros)

1. Copy this whole `0014630E` folder (with `macros/` and `logs/`) to the machine.
2. Open SolidWorks 2024.
3. Tools > Macro > New… (give it any temp name) — the VBA editor opens.
4. Paste the contents of `00_setup.vba`, press **F5** (Run). It creates the part,
   sets units, and saves it next to this folder.
5. Repeat for each numbered macro **in order** (01_, 02_, …).
   - Each macro logs PASS/FAIL to `../logs/build_log.txt` and stops on failure.
   - **Stop on the first failure** — do not run later macros on a broken state.
6. `NN_fillets_chamfers.vba` (if present) is interactive: select the edge(s) in
   the graphics area first, then run the macro; it applies the exact radius /
   chamfer values from the drawing.
7. Finish with `ZZ_final_verify.vba` — rebuild, mass properties, bounding-box
   check against the drawing envelope, save.

Notes
- Macros marked `TODO: VERIFY API CALL` describe a step to do manually
  (cosmetic threads, countersinks, revolves) — values are in the comments.
- If a feature's position was not readable from the drawing, the macro says
  `POSITION ASSUMED` — verify against the drawing before trusting the model.
- Check `0014630E_build_plan.json` for the full step list, including anything
  skipped as prohibited (lofts/sweeps/shells are never generated).
