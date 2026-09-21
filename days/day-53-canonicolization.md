# Day 53: Canonicalization — When Two Different Shapes Mean the Same Thing

## TL;DR
Day 52's projection step strips volatile *noise* from a result before hashing, but it assumes every result for a given operation shape looks the same to begin with. That assumption breaks when a retry lands on a different upstream replica, or a provider rolls out a v2 response format alongside v1 — two results that are semantically identical but structurally unrecognizable to each other. Today's fix adds a canonicalization layer ahead of projection: per-shape adapters that map known-valid response formats onto one canonical representation before anything gets hashed.

## The Problem
- Day 52's projection schemas operate on a fixed field layout — they know which fields to strip, but only because they assume the result already looks like the shape they were written for.
- Some upstreams don't guarantee a stable shape across calls: a request retried against a different replica can come back with reordered fields, renamed keys (`sat_id` vs `satellite_id`), or an entirely different nesting structure, while representing the exact same orbital fact.
- A provider-side rollout (v1 → v2 response format) can mean the *same* operation shape from Day 50's classification now returns two structurally different-but-equivalent payloads depending on which upstream version handled the call.
- Without a way to recognize these as equivalent, every such divergence looks identical to a genuine content change from the resume logic's point of view — it cascades re-execution through every dependent, even though nothing about the orbital data actually changed.
- This is a different failure than Day 52's: Day 52 was about *noise within one known shape*; this is about *multiple valid shapes for the same fact*, which projection has no vocabulary for.

## Architecture

### 1. Canonical form registry
Each operation shape can register one or more adapters — functions that map a recognized result format onto a single canonical representation. A discriminator (a stable field or structural fingerprint) identifies which adapter applies to a given raw result.

```python
CANONICAL_ADAPTERS = {
    "orbit-lookup:keyed:responded": [
        {
            "discriminator": lambda r: "satellite_id" in r,
            "to_canonical": lambda r: {
                "sat_id": r["satellite_id"],
                "position": r["orbital_position"],
            },
        },
        {
            "discriminator": lambda r: "sat_id" in r,
            "to_canonical": lambda r: {
                "sat_id": r["sat_id"],
                "position": r["position"],
            },
        },
    ]
}

def canonicalize(operation_shape: str, result: dict) -> dict:
    adapters = CANONICAL_ADAPTERS.get(operation_shape, [])
    for adapter in adapters:
        if adapter["discriminator"](result):
            return adapter["to_canonical"](result)
    return result  # no matching adapter -> pass through unchanged, conservative default
```

### 2. Pipeline ordering: canonicalize, then project
Canonicalization runs before Day 52's projection, not instead of it — the two solve different problems and compose cleanly. Canonicalize first collapses shape differences down to one structure; project then strips volatile fields from that canonical structure the same way it always has.

```python
def commit_result(item_id, operation_shape, raw_result):
    canonical = canonicalize(operation_shape, raw_result)
    projected = project(operation_shape, canonical)
    ledger.store(
        item_id,
        raw=raw_result,
        canonical=canonical,
        projected_hash=content_hash(projected),
        operation_shape=operation_shape,
    )
```

### 3. Adapter registration via cross-shape replay evidence
An adapter isn't hand-written and trusted on the spot. It's registered the same way Day 52 promotes a projection field: only after replay evidence shows that two structurally different results, observed for the same logical operation, produced identical downstream dependent behavior when each was fed through its respective adapter into the canonical form.

```python
def register_adapter_if_equivalent(operation_shape, candidate_adapter, replay_log, threshold=20):
    equivalent_outcomes = count_equivalent_dependent_behavior(
        replay_log, operation_shape, candidate_adapter
    )
    if equivalent_outcomes >= threshold:
        CANONICAL_ADAPTERS[operation_shape].append(candidate_adapter)
        audit_log.record(operation_shape, candidate_adapter, equivalent_outcomes)
```

"Equivalent" is checked the same narrow way Day 52 checks "harmless": not just that the canonical forms match structurally, but that every dependent actually exercised against results from both shapes behaved identically. A shape that merely renames fields but happens to also drop a field a dependent needs would fail this check, because the dependent's behavior would diverge.

### 4. Fallback when no adapter matches
A result whose shape doesn't match any registered discriminator passes through unchanged and gets hashed as-is, per Day 52's raw-hashing fallback. This means an unrecognized new shape is treated as a genuine change — correct but conservative, and it will cascade until an adapter for that shape earns its way into the registry.

```python
def is_resumable(item, ledger):
    for upstream_id, consumed_hash in item.dependencies.items():
        current_hash = ledger.get(upstream_id).projected_hash
        if current_hash != consumed_hash:
            return False
    return True
```

This function is unchanged from Day 52 — canonicalization, like projection, lives entirely upstream of the comparison logic, so the resume walk and cascade mechanics from Day 51 stay untouched.

## Failure Modes
- **False equivalence from a bad adapter.** An adapter that canonicalizes two shapes that are *not* actually equivalent silently merges them, suppressing cascades that should have fired — the same class of failure as Day 52's under-triggering risk, but harder to catch here because the two raw shapes look nothing alike, so a human skimming a diff is less likely to notice the adapter is wrong.
- **Discriminator collisions.** If two different response formats can both satisfy the same discriminator check (e.g. both happen to contain a `satellite_id` field for unrelated reasons), the wrong adapter gets applied and produces a canonical form that doesn't represent either shape correctly. Discriminators need to be specific enough to be mutually exclusive across all registered adapters for a given operation shape.
- **Adapter drift.** A provider evolving their v2 format further (v2.1) can silently break an existing adapter's assumptions without tripping the discriminator, producing a canonical form that's subtly wrong rather than falling back to the safe "no adapter matched" path.
- **Combinatorial growth.** Every new upstream variant needs its own adapter, and adapters need to be checked pairwise for discriminator collisions — this doesn't scale cleanly if an operation shape accumulates many valid formats over time.

## What's Next
Day 54: partial equivalence. Today's model treats two shapes as either fully equivalent or not comparable at all. But some shapes are equivalent for *most* dependents and diverge only in a field that just one specific dependent actually needs — full canonicalization would incorrectly treat them as identical for everyone, while full rejection would cascade unnecessarily for dependents that never touch the divergent field.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
