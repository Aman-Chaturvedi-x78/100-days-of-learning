# Day 54: Dependent-Scoped Read-Sets — Stop Comparing the Whole Result

## TL;DR
Day 53's canonicalization treats a result as equivalent or not for *every* dependent at once — one hash, shared by all consumers. But some shape differences are equivalent for most dependents and diverge only in a field that just one specific dependent actually reads. Full canonicalization overreaches and can hide a difference that matters to that one dependent; full rejection cascades everyone else for no reason. Today's fix: give each dependent its own read-set — the subset of fields it actually consumes — and hash only that subset when deciding whether that specific dependent needs to re-run. The resume check moves from a single item-level verdict to a per-(item, dependent) verdict.

## The Problem
- Day 53's projected/canonical hash is computed once per committed result and shared by every dependent that consumes it — there's exactly one hash per item, regardless of how many downstream consumers exist or what each of them actually needs from the result.
- A field that diverges between two otherwise-equivalent shapes either gets proven safe for everyone via canonicalization (dangerous if even one dependent actually reads it — that dependent silently gets a stale value it thinks is current) or it forces a cascade for everyone (wasteful, since most dependents in a high-fanout graph, per Day 50's batch model, likely never touch that specific field).
- Resume logic today operates at the item level — `is_resumable(item)` returns one boolean. There's no notion of "resumable for dependent A but not dependent B," even though partial equivalence, by definition, requires exactly that distinction.
- This gets more expensive to ignore as fan-out grows: the more dependents a single upstream item has, the more likely it is that at least one of them cares about a field the others don't, and the more collateral cascade cost is paid by dependents who were never going to be affected.

## Architecture

### 1. Read-set declaration
Each dependent registers the field paths of an upstream result it actually accesses, scoped to the operation shape it depends on. This is a static, hand-maintained registry to start — the read-set is declared once per (operation shape, dependent) pair rather than derived.

```python
READ_SETS = {
    ("orbit-lookup:keyed:responded", "collision-predictor"): ["sat_id", "position"],
    ("orbit-lookup:keyed:responded", "reentry-estimator"): ["sat_id", "position", "velocity"],
}
```

Nested field paths use dotted notation (`"metadata.orbit_class"`) so a dependent reading only a sub-field of a nested object doesn't need the whole object included in its declared set.

### 2. Per-dependent scoped hashing
Instead of one hash per item, compute a hash scoped to each dependent's declared read-set from the canonical result. This runs after Day 53's canonicalization and Day 52's projection — scoping is a third, final narrowing step in the pipeline, not a replacement for either.

```python
def scoped_hash(operation_shape, dependent_id, canonical_result):
    fields = READ_SETS.get((operation_shape, dependent_id))
    if not fields:
        return content_hash(canonical_result)  # no declared read-set -> full hash, conservative
    scoped = {k: extract_path(canonical_result, k) for k in fields}
    return content_hash(scoped)
```

### 3. Resume check keyed by (item, dependent)
`is_resumable` moves from item-level to (item, dependent)-level — a dependent only re-runs if the fields *it* reads changed, independent of whether other dependents of the same upstream item need to re-run too.

```python
def is_resumable_for(item, dependent_id, ledger):
    for upstream_id, consumed_hash in item.dependencies.items():
        shape = ledger.get(upstream_id).operation_shape
        canonical = ledger.get(upstream_id).canonical
        if scoped_hash(shape, dependent_id, canonical) != consumed_hash:
            return False
    return True
```

### 4. Commit-time hash recording per dependent
When a dependent consumes an upstream result, it now records the scoped hash it saw — not the item-wide canonical hash — so the comparison at resume time is apples-to-apples against what that dependent actually keyed its execution on.

```python
def record_dependency(item_id, dependent_id, upstream_id, ledger):
    shape = ledger.get(upstream_id).operation_shape
    canonical = ledger.get(upstream_id).canonical
    consumed_hash = scoped_hash(shape, dependent_id, canonical)
    ledger.record_dependency(item_id, dependent_id, upstream_id, consumed_hash)
```

## Failure Modes
- **Stale or wrong declarations.** A hand-declared read-set is only as accurate as the developer's memory of what their own code touches — under-declaring a field silently produces a missed cascade for exactly the field that mattered, which is the same class of silent-failure risk flagged since Day 52's projection schemas and Day 53's canonicalization adapters.
- **No enforcement.** Nothing today verifies a declared read-set against what the dependent's code actually does at runtime, so drift between declaration and reality goes unnoticed until a bad cascade — or worse, a missed one — surfaces it in production.
- **Fan-out cost.** Computing a separate scoped hash per registered dependent adds work proportional to fan-out, on top of Day 53's canonicalization step — for an item with many dependents, this is now O(dependents) hash computations per commit instead of one.
- **Declaration maintenance burden.** Every new dependent added to the graph needs its read-set declared before it benefits from scoping at all; until then it silently falls back to the full canonical hash, which is safe but means the declaration step is easy to forget and easy to skip under time pressure.

## What's Next
Day 55: hand-declared read-sets are exactly the kind of unverified assumption this series has been trying to eliminate since Day 52's replay-evidence requirement for projection schemas. The next step is deriving read-sets automatically from what a dependent actually accesses at runtime, instead of trusting what a developer remembers to declare.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
