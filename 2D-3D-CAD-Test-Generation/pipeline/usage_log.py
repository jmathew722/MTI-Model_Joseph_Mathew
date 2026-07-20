"""Token-usage and API cost ledger (Claude or, on MTI_Codex, GPT-5.6).

Appends one row per extraction run to a human-readable ``token_usage_log.txt``
(and a machine-readable ``token_usage_log.jsonl`` used to recompute the running
total) at the output root, so the cost of every API call is tracked over time.
The row's ``model`` field names whichever model actually ran that stage, so a
mixed history (e.g. re-running the same output dir under both providers) still
prices each row correctly rather than assuming one provider for the whole file.

Prices are USD per MILLION tokens.

Anthropic (claude-api skill, cached 2026-07-03). Prompt-caching multipliers:
cache WRITE (5-min TTL) = 1.25x input, cache READ = 0.10x input.
claude-sonnet-5 is listed at $3/$15 (an introductory $2/$10 applies through
2026-08-31 — the ledger uses the list price, so logged costs are an upper bound
during the intro window). Update PRICING if Anthropic changes published prices.

OpenAI GPT-5.6 (pricing checked live 2026-07-20 against developers.openai.com/
api/docs/pricing — verify again if it drifts). Automatic prompt caching gives
the same 90% discount convention as Anthropic's cache read (0.10x input); there
is no separate cache-WRITE charge exposed in the API, so ``cache_write`` is set
equal to ``input`` (a cache write costs the same as an ordinary input token) and
:mod:`pipeline.ai_provider` always reports 0 cache-write tokens for OpenAI, so
that column is inert unless the pricing model changes.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# USD per 1,000,000 tokens.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    # MTI_Codex (OpenAI provider). "gpt-5.6" is the alias for the Sol (frontier)
    # tier — used for every stage on this branch (vision + reasoning).
    "gpt-5.6": {"input": 5.00, "output": 30.00, "cache_write": 5.00, "cache_read": 0.50},
    "gpt-5.6-sol": {"input": 5.00, "output": 30.00, "cache_write": 5.00, "cache_read": 0.50},
    "gpt-5.6-terra": {"input": 2.50, "output": 15.00, "cache_write": 2.50, "cache_read": 0.25},
    "gpt-5.6-luna": {"input": 1.00, "output": 6.00, "cache_write": 1.00, "cache_read": 0.10},
}
# Used when the model isn't in PRICING (keeps the ledger working, flags unknown).
_FALLBACK = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}

LEDGER_TXT = "token_usage_log.txt"
LEDGER_JSONL = "token_usage_log.jsonl"


def estimate_cost(usage: dict[str, int], model: str) -> float:
    """USD cost of one run's token usage. Cache hits (no tokens) cost $0."""
    p = PRICING.get(model, _FALLBACK)
    return (
        usage.get("input_tokens", 0) / 1e6 * p["input"]
        + usage.get("output_tokens", 0) / 1e6 * p["output"]
        + usage.get("cache_creation_input_tokens", 0) / 1e6 * p["cache_write"]
        + usage.get("cache_read_input_tokens", 0) / 1e6 * p["cache_read"]
    )


def record_run(output_dir: Path | str, part: str, model: str, usage: dict[str, int],
               stage: str = "extraction") -> Path:
    """Append one run to the ledger and rewrite the human-readable total.

    ``stage`` tags the cost center for the row (e.g. ``extraction``,
    ``stage_1_5_overview_analysis``, ``spec_reconciliation``, ``overview_check``)
    so distinct pipeline stages show as distinct line items in the ledger.

    Returns the path to the human-readable ``token_usage_log.txt``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cost = estimate_cost(usage, model)
    cache_hit = usage.get("cache_hits", 0) > 0 and not usage.get("input_tokens", 0)

    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "part": part or "?",
        "stage": stage or "extraction",
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
        "api_calls": usage.get("calls", 0),
        "cache_hit": cache_hit,
        "cost_usd": round(cost, 4),
        "model_priced": model in PRICING,
    }

    # Append the machine-readable row, then recompute totals from all rows.
    jsonl_path = output_dir / LEDGER_JSONL
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")

    rows = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    total_cost = sum(r.get("cost_usd", 0.0) for r in rows)
    total_in = sum(r.get("input_tokens", 0) for r in rows)
    total_out = sum(r.get("output_tokens", 0) for r in rows)
    total_cread = sum(r.get("cache_read_tokens", 0) for r in rows)

    lines = [
        "API TOKEN & COST LEDGER",
        "========================",
        "Prices USD per 1M tokens (sonnet-5 $3/$15, sonnet-4-6 $3/$15, opus-4-8 $5/$25, "
        "gpt-5.6 $5/$30, gpt-5.6-terra $2.50/$15, gpt-5.6-luna $1/$6; see PRICING in "
        "this module for cache-write/cache-read multipliers per model).",
        "Each row's MODEL column names whichever model/provider actually ran that stage.",
        "Only extraction runs hit the API; --from-json and cache hits cost $0.",
        "",
        f"{'TIMESTAMP':<20}  {'PART':<16}  {'STAGE':<28}  {'MODEL':<18}  "
        f"{'IN':>9}  {'OUT':>8}  {'CACHE_RD':>9}  {'CALLS':>5}  {'COST_USD':>9}",
        "-" * 140,
    ]
    for r in rows:
        note = "  (cache hit)" if r.get("cache_hit") else ""
        note += "  [unpriced model]" if not r.get("model_priced", True) else ""
        lines.append(
            f"{r.get('timestamp',''):<20}  {str(r.get('part',''))[:16]:<16}  "
            f"{str(r.get('stage', 'extraction'))[:28]:<28}  "
            f"{str(r.get('model',''))[:18]:<18}  {r.get('input_tokens',0):>9,}  "
            f"{r.get('output_tokens',0):>8,}  {r.get('cache_read_tokens',0):>9,}  "
            f"{r.get('api_calls',0):>5}  {r.get('cost_usd',0.0):>9.4f}{note}"
        )
    lines += [
        "-" * 140,
        f"{'TOTAL':<20}  {len(rows)} run(s){'':<8}  {'':<28}  {'':<18}  "
        f"{total_in:>9,}  {total_out:>8,}  {total_cread:>9,}  {'':>5}  {total_cost:>9.4f}",
        "",
        f"TOTAL API COST TO DATE: ${total_cost:.4f}",
    ]
    txt_path = output_dir / LEDGER_TXT
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path
