# Day 52: Semantic Hashing — Stop Cascading on Noise

## TL;DR
Day 51's topological resume treats a batch item's committed result as an opaque blob: any byte-level change to that blob — even a re-serialized timestamp or an echoed request ID — triggers a full cascade of re-execution through every downstream dependent. Today's fix introduces a projection step before hashing, so dependency comparison runs on the *semantically meaningful* part of a result instead of its raw bytes. The schema that decides what gets stripped isn't hand-authored — it's earned through replay evidence, so the system never assumes a field is safe to ignore without proof.

## The Problem
- Content-hash versioning (Day 51) hashes the raw committed response to detect whether a dependent's upstream input has actually changed.
- Non-deterministic fields — `processed_at`, retry counters, request-echo IDs, sometimes even key ordering after a library upgrade — change on every re-execution even when the actual payload is functionally identical.
- Any hash mismatch, cosmetic or not, cascades re-execution forward through every dependent that consumed the item, per Day 51's design. There's no distinction between "the upstream fact changed" and "the upstream fact was re-serialized."
- At scale this means routine, harmless noise triggers the same expensive re-execution path as a genuine upstream change. Every retry, every re-checkpoint, every regrant after a loan renewal (Day 45) risks looking like drift even when nothing about the actual orbital data changed.
- This gets worse as batch sizes grow (Day 50) — one noisy field in a high-fanout item can cascade through dozens of dependents, each of which re-executes, re-commits, and produces its *own* fresh noise, compounding the problem downstream.
- The system currently has no way to distinguish "this matters" from "this doesn't," so it has to treat every mismatch as if it might matter — which is correct but expensive, and gets more expensive as the dependency graphs (Day 51) get deeper.

## Architecture

### 1. Projection schema per operation shape
Reuses Day 50's `(callee, has-item-keys, has-item-response)` operation shape as the key for a registered projection function — an allowlist of fields to strip before hashing. Allowlist, not denylist: the system only strips fields it has positive evidence are safe, rather than assuming everything is safe except a few known-bad fields.

```python
PROJECTION_SCHEMAS = {
    "orbit-lookup:keyed:responded": {
        "strip": ["processed_at", "request_id", "retry_count"]
    }
}

def project(operation_shape: str, result: dict) -> dict:
    schema = PROJECTION_SCHEMAS.get(operation_shape)
    if not schema:
        return result  # no schema registered -> hash raw, conservative default
    return {k: v for k, v in result.items() if k not in schema["strip"]}
```

Projection runs on a normalized copy of the result — nested dicts and lists are walked recursively so a volatile field buried inside a nested object (e.g. `metadata.processed_at`) is stripped the same way a top-level one is, rather than only handling flat schemas.

### 2. Dual hash storage
The effect ledger stores both the raw result (for audit/debugging) and the projected hash (for dependency comparison). Dependents key off the projected hash only — raw results stay around purely so a human debugging a cascade can see exactly what changed, byte for byte, without that detail leaking into the comparison logic.

```python
def commit_result(item_id, operation_shape, raw_result):
    projected = project(operation_shape, raw_result)
    ledger.store(
        item_id,
        raw=raw_result,
        projected_hash=content_hash(projected),
        operation_shape=operation_shape,
    )
```

Keeping the raw result also matters for schema promotion (step 3) — the replay comparison needs the full, unprojected response to determine whether a field's drift was actually harmless, not just whether it was stripped.

### 3. Schema promotion via replay evidence
A field only enters a projection schema's strip-list after Day 49's drift-check replay machinery has observed it drifting harmlessly across N independent replays of the same operation shape — never added speculatively, and never added by a developer eyeballing a diff and deciding a field "looks safe."

```python
def promote_field_if_safe(operation_shape, field, replay_log, threshold=20):
    harmless_drifts = count_harmless_drifts(replay_log, field)
    if harmless_drifts >= threshold:
        PROJECTION_SCHEMAS[operation_shape]["strip"].append(field)
        audit_log.record(operation_shape, field, harmless_drifts)
```

"Harmless" here is defined narrowly: the field's value differed across replays of the *same logical operation*, while every other field — and, critically, every downstream dependent's actual behavior when fed that result — stayed identical. A field that drifts but happens to never have been consumed by anything downstream yet doesn't count; the threshold only counts replays where a dependent was actually exercised against the result.

### 4. Cascade suppression at comparison time
With projected hashes in place, Day 51's topological resume comparison changes from "does the raw hash match" to "does the projected hash match" — the dependency-graph walk, the per-item resumability check, and the cascade-forward logic from Day 51 are otherwise untouched. This was a deliberate design constraint: the fix should live entirely in what gets hashed, not in how the graph is walked, so it doesn't reopen any of the correctness guarantees Day 51 already established.

```python
def is_resumable(item, ledger):
    for upstream_id, consumed_hash in item.dependencies.items():
        current_hash = ledger.get(upstream_id).projected_hash
        if current_hash != consumed_hash:
            return False
    return True
```

## Failure Modes
- **Under-triggering from a bad schema.** A projection schema that strips a field which *does* affect downstream semantics silently suppresses cascades that should have fired — strictly worse than the over-triggering it's meant to fix, because it fails silently instead of loudly. Mitigated by requiring replay-evidenced promotion (point 3) rather than hand-authored schemas, but the threshold is still a judgment call — too low and a coincidentally-harmless field gets promoted too early; too high and genuinely safe fields sit unprojected for longer than necessary.
- **No schema, no protection.** Operation shapes without a registered schema fall back to raw hashing — conservative by default, so the system never assumes safety it hasn't earned. This means new operation shapes get zero benefit from today's fix until they accumulate enough replay history, which could be a meaningful cold-start cost for fast-changing integrations.
- **Schema staleness.** An upstream API change can reintroduce meaning into a previously-volatile field without the system noticing, since promotion only runs forward — nothing currently re-validates an already-promoted field against fresh behavior. A field promoted six weeks ago under an old API version could start carrying real signal after a provider-side change, and the system would keep stripping it regardless.
- **Replay sample bias.** The replay log used for promotion reflects whatever traffic patterns happened to occur during the observation window — if a field only drifts meaningfully under a rare condition (e.g. a specific error-retry path) that wasn't exercised during promotion, it can get promoted on incomplete evidence.

## What's Next
Day 53: equivalent-but-differently-shaped results. Projection handles noise *within* one schema, but says nothing about two valid, differently-structured responses for the same operation — e.g., a retry landing on a different upstream replica that returns semantically identical data in a different shape entirely (different field names, different nesting, a v2 API response format alongside a v1 one). Projection schemas assume a fixed shape to strip fields from; they have no answer for "these two results mean the same thing but don't look alike at all."

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
