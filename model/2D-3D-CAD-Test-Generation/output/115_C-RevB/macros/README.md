# Running these macros on the SolidWorks machine

These macros build the part **in order**. No Python needed — just SolidWorks.

1. Copy this whole `115_C-RevB` folder (with `macros/` and `logs/`) to the machine.
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
- Check `115_C-RevB_build_plan.json` for the full step list, including anything
  skipped as prohibited (lofts/sweeps/shells are never generated).
