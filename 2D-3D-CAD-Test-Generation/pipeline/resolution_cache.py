"""Resolution determinism cache (2026-07-12, "extraction is truth" — Task 2).

``resolve_extraction`` is already a pure, deterministic function of its inputs
(no randomness, no dict/hash-order dependence, no wall-clock reads — verified by
this module's own tests). This module makes that guarantee OBSERVABLE and
PROVABLE at the pipeline's entry points:

  * ``cache_key`` hashes the raw extraction dict + the resolver's version
    string, so the key changes if — and only if — the extraction changed or the
    resolver's logic did.
  * ``resolve_with_cache`` always calls the real resolver fresh (it is cheap and
    makes no paid API call — there is nothing to save by skipping it), then
    compares the result's hash against whatever was cached under the same key.
    Same key + different output would mean the resolver stopped being pure —
    that is loud-logged as a determinism violation, never silently accepted.
    A different key (extraction or resolver version changed) is an expected,
    ATTRIBUTED cause: the cache is updated and, when the resolver version is
    what changed, a diff of every resolved value that moved is logged.

No cache directory given -> pure passthrough (no disk I/O); this makes the
wrapper safe to drop into any call site without new required plumbing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from utils.logger import get_logger

log = get_logger()

# Bump this whenever resolver.py's DECISION LOGIC changes (not for comments/
# refactors that can't change a resolved value). A changed version invalidates
# every cache entry and is the only thing besides the extraction itself that is
# allowed to change what a re-run resolves to.
RESOLVER_VERSION = "2026-07-12.1"

CACHE_DIRNAME = ".resolution_cache"


def cache_key(raw_extraction: dict, resolver_version: str = RESOLVER_VERSION) -> str:
    """Stable hash of (extraction content, resolver version). ``sort_keys=True``
    makes the JSON serialization independent of dict insertion order."""
    blob = json.dumps(raw_extraction, sort_keys=True, default=str)
    h = hashlib.sha256()
    h.update(blob.encode("utf-8"))
    h.update(b"|")
    h.update(resolver_version.encode("utf-8"))
    return h.hexdigest()


def _resolved_hash(resolved_extraction: dict) -> str:
    return hashlib.sha256(
        json.dumps(resolved_extraction, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _diff_resolved_values(old: dict, new: dict) -> list[dict]:
    """Which dimension/feature resolved values moved between two resolved
    extractions with the SAME extraction content (i.e., only the resolver
    version differed) — the attributed-cause log the prompt requires."""
    diffs: list[dict] = []
    old_dims = {d.get("id"): d for d in (old.get("dimensions") or [])}
    new_dims = {d.get("id"): d for d in (new.get("dimensions") or [])}
    for did, nd in new_dims.items():
        od = old_dims.get(did)
        if od is None:
            continue
        if od.get("value") != nd.get("value") or od.get("assumption_basis") != nd.get("assumption_basis"):
            diffs.append({"id": did, "old_value": od.get("value"), "new_value": nd.get("value"),
                          "old_basis": od.get("assumption_basis"), "new_basis": nd.get("assumption_basis")})
    return diffs


def resolve_with_cache(
    raw_extraction: dict,
    cache_dir: Optional[Path | str] = None,
    *,
    resolve_fn: Optional[Callable[..., Any]] = None,
    resolver_version: str = RESOLVER_VERSION,
    **resolve_kwargs: Any,
):
    """Resolve ``raw_extraction`` and cross-check the result against the cache.

    Always calls the real resolver (fresh, deterministic, free) and returns its
    ``ResolutionResult`` unchanged. When ``cache_dir`` is given:
      * same key + same output hash  -> cache hit, confirmed byte-identical.
      * same key + DIFFERENT output hash -> a determinism violation: logged as
        an ERROR (this should never happen; it means the resolver stopped being
        pure) but the fresh result is still returned/used — never silently
        substitute a stale cached value for the truth.
      * different key -> a new/changed extraction or resolver version. If only
        the resolver version changed (extraction content identical), the moved
        values are diffed and logged as the attributed cause. The cache is
        updated to the new key either way.
    """
    if resolve_fn is None:
        from pipeline.resolver import resolve_extraction as resolve_fn  # noqa: PLC0415

    result = resolve_fn(raw_extraction, **resolve_kwargs)

    if cache_dir is None:
        return result

    cache_dir = Path(cache_dir)
    key = cache_key(raw_extraction, resolver_version)
    new_hash = _resolved_hash(result.resolved_extraction)
    entry_path = cache_dir / f"{key}.json"

    if entry_path.is_file():
        try:
            cached = json.loads(entry_path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        if cached is not None:
            if cached.get("resolved_hash") == new_hash:
                log.debug("resolution cache HIT (%s): byte-identical to the cached result.", key[:12])
            else:
                log.error(
                    "resolution cache MISMATCH (%s): the SAME extraction + resolver version "
                    "produced a DIFFERENT resolved output — the resolver is no longer "
                    "deterministic. Investigate before trusting this run's resolved values.",
                    key[:12])
        return result

    # New key — find the most recent OTHER cache entry (if any) to attribute a
    # resolver-version-only change (extraction identical, only logic changed).
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        siblings = sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        siblings = []
    for sib in siblings:
        try:
            prev = json.loads(sib.read_text(encoding="utf-8"))
        except Exception:
            continue
        if prev.get("extraction_hash") == cache_key(raw_extraction, "") and \
                prev.get("resolver_version") != resolver_version:
            diffs = _diff_resolved_values(prev.get("resolved_extraction", {}),
                                          result.resolved_extraction)
            if diffs:
                log.info("resolver version changed (%s -> %s); %d resolved value(s) moved: %s",
                         prev.get("resolver_version"), resolver_version, len(diffs), diffs)
        break

    entry_path.write_text(json.dumps({
        "key": key, "resolver_version": resolver_version,
        "extraction_hash": cache_key(raw_extraction, ""),
        "resolved_hash": new_hash,
        "resolved_extraction": result.resolved_extraction,
    }, indent=2, default=str), encoding="utf-8")
    return result
