"""Token-usage and Claude API cost ledger.

Appends one row per extraction run to a human-readable ``token_usage_log.txt``
(and a machine-readable ``token_usage_log.jsonl`` used to recompute the running
total) at the output root, so the cost of every API call is tracked over time.

Prices are USD per MILLION tokens, from the Anthropic pricing reference
(claude-api skill, cached 2026-06-04). Prompt-caching multipliers:
cache WRITE (5-min TTL) = 1.25x input, cache READ = 0.10x input.
Update PRICING if Anthropic changes published prices.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# USD per 1,000,000 tokens.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
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


def record_run(output_dir: Path | str, part: str, model: str, usage: dict[str, int]) -> Path:
    """Append one run to the ledger and rewrite the human-readable total.

    Returns the path to the human-readable ``token_usage_log.txt``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cost = estimate_cost(usage, model)
    cache_hit = usage.get("cache_hits", 0) > 0 and not usage.get("input_tokens", 0)

    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "part": part or "?",
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
        "CLAUDE API TOKEN & COST LEDGER",
        "==============================",
        "Prices USD per 1M tokens (sonnet-4-6 $3/$15, opus-4-8 $5/$25; "
        "cache write 1.25x input, cache read 0.10x input).",
        "Only extraction runs hit the API; --from-json and cache hits cost $0.",
        "",
        f"{'TIMESTAMP':<20}  {'PART':<16}  {'MODEL':<18}  "
        f"{'IN':>9}  {'OUT':>8}  {'CACHE_RD':>9}  {'CALLS':>5}  {'COST_USD':>9}",
        "-" * 110,
    ]
    for r in rows:
        note = "  (cache hit)" if r.get("cache_hit") else ""
        note += "  [unpriced model]" if not r.get("model_priced", True) else ""
        lines.append(
            f"{r.get('timestamp',''):<20}  {str(r.get('part',''))[:16]:<16}  "
            f"{str(r.get('model',''))[:18]:<18}  {r.get('input_tokens',0):>9,}  "
            f"{r.get('output_tokens',0):>8,}  {r.get('cache_read_tokens',0):>9,}  "
            f"{r.get('api_calls',0):>5}  {r.get('cost_usd',0.0):>9.4f}{note}"
        )
    lines += [
        "-" * 110,
        f"{'TOTAL':<20}  {len(rows)} run(s){'':<8}  {'':<18}  "
        f"{total_in:>9,}  {total_out:>8,}  {total_cread:>9,}  {'':>5}  {total_cost:>9.4f}",
        "",
        f"TOTAL API COST TO DATE: ${total_cost:.4f}",
    ]
    txt_path = output_dir / LEDGER_TXT
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path
