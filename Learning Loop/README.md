# Learning Loop

An **iterative learning loop** for the MTI 2D→3D pipeline. Every time a part is
run through the pipeline (from the web UI or the CLI), the pipeline writes one
plain-text failure report into this folder:

```
Learning Loop/<Part>__<YYYY-MM-DD_HHMMSS>.txt
```

Each report captures **every failure and flag from that run**, grouped:

1. **Gate / status** — READY vs NOT READY and the exact gate reasons.
2. **Must-meet constraint failures** — each `MM-xxx` with *measured vs required*.
3. **Cross-view conflicts** — the Stage 1.5 holistic overview analysis findings.
4. **Engineering flags — ALL severities** (CRITICAL → HIGH → MEDIUM → LOW) —
   every assumption, skipped feature, overview gap, and unmet requirement, each
   with its decision, why, and what it affects.
5. **Build / macro feature failures** — the exact feature that failed and why.
6. **FIXES FOR FABLE** — a paste-ready brief, including the *suspected code
   areas* per failure, to hand to Claude (Fable) to plan and apply code fixes.

`INDEX.md` (created on the first run) keeps one chronological line per run.

## How to use the loop

1. Run parts through the pipeline as normal — reports accumulate here.
2. When you want the pipeline to improve, open the most recent (or a range of)
   report(s) and **paste them to Fable** with a request like *"plan and fix
   these in the code."*
3. Fable proposes and applies a generalized fix (not a one-off), you re-run, and
   the next report shows whether the class of failure is gone — closing the loop.

The goal is a fix that **generalizes** — resolves the whole class of failure —
so the pipeline gets measurably better run over run, not patched per part.

> These reports are committed to the repo on purpose: they are the training
> signal for the loop. They reference (but never duplicate) the full artifacts
> in each run's output folder.
