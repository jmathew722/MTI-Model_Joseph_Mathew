# Human-assist escalation layer — 2026-07-11

Stops silent re-looping on stuck features. The closed-loop system (resolver
ladder → reconciliation → Phase B correction loop → Phase D method experiments)
exhausts every *automated* path. Some blockers, though, are missing FACTS
(occluded dimension, cropped title block, designer intent) — no re-reasoning
manufactures them, and each retry is a slow COM round-trip. This layer is the
exit ramp: escalate late, escalate narrow, never block, batch, learn.

## Task 1 — trigger + question generation (`pipeline/human_assist.py`)

An item becomes a question ONLY after the full ladder fails
(`escalation_eligible`): resolver plausibility → TYP/derivation → the Phase B
loop → Phase D method experiments (chronic construction kinds only). Then it
emits a `Question` object: one-sentence `question_text`, pre-populated
`candidates` (value + basis), `region_crop`, `automated_attempts` (audit), and a
`default_if_unanswered` that is **always populated**. Capped (default 3) and
prioritized by leverage — a base/envelope dimension outranks one slot's radius;
higher fan-out and CRITICAL tier rank higher. Queue → `<Part>_assist_queue.json`
+ `lessons_learned.jsonl`.

## Task 2 — non-blocking integration (`reconciliation`/`batch`/`main`)

A pending question is a new *flagged* disposition, `NEEDS_HUMAN_INPUT`, overlaid
**additively** on `build_dispositions.json` (geometric `state` unchanged, so
existing consumers are unaffected). The part still ships its complete
approximate model and its usual READY status — questions do NOT gate READY. On
answer, the value feeds the resolver as its **highest-priority** candidate
(`assumption_basis="human_provided"`, `tier_human`, above spec — a person on the
actual sheet outranks every automated tier) and the affected feature
re-resolves + re-splices via the exact reconciliation splice-back. **No paid
re-extraction** anywhere (`rerun_with_answers` calls only `resolve_extraction`).

## Task 3/4 — batched review surface + API (`webapp`)

`GET /api/parts/{session}/assist` returns the pending/answered queue across the
session; `POST` accepts a batch of `{question_id, answer}`, records them,
re-resolves each affected part, and returns updated dispositions — no reload.
The Sheet-2 "Assist Needed" section (matches the Blueprint-Room design system)
is a review queue, not a per-question modal: grouped by part, candidate radios +
free-text override, the default-if-skipped clearly marked, a count badge, and an
empty state so it's easy to ignore when nothing is urgent. Verified live: a
posted answer re-resolved and recovered the excluded feature (`recovered:
["F002"]`, 0 pending), with no extraction call.

## Task 5 — pattern learning (`pipeline/learned_patterns.py`)

When the same value-free `signature` (kind + what it dimensions + candidate
count) is answered the same relative way (e.g. "prefer the larger candidate")
across **≥2 distinct parts**, it generalizes to `LEARNED_PATTERNS.md` +
`learned_patterns.json` — an auditable, reversible priority/confidence bias.
Never auto-applies a numeric value across drawings (a wrong generalization can
only mis-rank a question, never fabricate a value).

## Invariants preserved

Single-point unit conversion; candidates-only resolution (a human answer is a
candidate, never fabricated); unconditional dispositions; complete-approximate-
model; READY/NOT-READY contract + webapp banner regex (the new state lives in
report JSONs and an additive disposition overlay — no new top-level status
enum); no paid re-extraction in any loop.

Tests: `tests/test_human_assist.py` (15) — ladder ordering (a question is never
generated before all applicable stages), cap + severity prioritization,
default-always-populated, queue round-trip, human answer outranks spec in the
resolver, additive disposition overlay, pattern generalization only after ≥2
consistent parts. Full suite: 496 passing, zero regressions.
