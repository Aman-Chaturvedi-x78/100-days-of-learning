# Day 51: Intra-Batch Item Dependencies — When Item 3 Needs Item 1's Result

**TL;DR:** Day 50 gave us per-item resumability inside a batch, but it assumed items were independent. Real batches aren't — item 3 might consume item 1's output. Today: a dependency graph over batch items, result versioning so a dependent can tell if the upstream result it needs is still the one it originally consumed, and cascading re-execution when it isn't.

## The Problem

- Day 50's per-item resume checks *status* only — "did item 1 complete?" — not *identity* — "is the committed result for item 1 the same one item 3 actually used?"
- If item 1 gets re-executed on a resume (say, because its own idempotency key expired or it was marked non-resumable and redone), it can legitimately produce a different result than the first run.
- Item 3, if it already ran and consumed item 1's *original* result, is now silently stale — resuming it as "done" leaves a result built on data that no longer exists.
- Treating every downstream item as non-resumable "just in case" throws away most of Day 50's per-item resume benefit for any batch with real internal structure.

## Architecture

### 1. Declared dependency graph at submission

Batch items now carry declared edges instead of being treated as a flat set:

```python
batch = [
    {"id": "item_1", "op": "fetch_ephemeris", "deps": []},
    {"id": "item_2", "op": "fetch_ephemeris", "deps": []},
    {"id": "item_3", "op": "compute_conjunction", "deps": ["item_1"]},
]
```

The graph is built once, at submission time, from these edges — not inferred later from execution order.

### 2. Result versioning in the effect ledger

Each per-item ledger entry from Day 50 now stores a content hash of its committed result, not just a done/not-done flag:

```python
ledger_entry = {
    "item_id": "item_1",
    "status": "committed",
    "result_hash": sha256(result_bytes),
    "consumed_by": [],  # populated as downstream items read it
}
```

When item 3 executes and reads item 1's result, it records which `result_hash` it actually consumed:

```python
ledger_entry_item3 = {
    "item_id": "item_3",
    "status": "committed",
    "consumed": {"item_1": "a1b2c3..."},
}
```

### 3. Topological resume check

Resume no longer walks the batch flat. It walks the dependency graph in topological order:

```python
def resumable(item, ledger):
    if item.status != "committed":
        return False
    for dep_id, consumed_hash in item.consumed.items():
        current_hash = ledger[dep_id].result_hash
        if current_hash != consumed_hash:
            return False  # upstream moved out from under us
    return True
```

An item only counts as resumable if it's committed *and* every upstream result it actually consumed still matches what's currently on record.

### 4. Cascading re-execution

When an upstream item's hash has changed, the item that consumed the old value isn't just re-checked — it's marked for re-execution, and that decision propagates forward through the graph:

```python
def mark_cascade(item_id, graph, to_rerun):
    to_rerun.add(item_id)
    for dependent in graph.dependents_of(item_id):
        if dependent not in to_rerun:
            mark_cascade(dependent, graph, to_rerun)
```

One changed upstream result can ripple through several layers of dependents, not just the immediate consumer.

## Failure Modes

- **Consumed-hash never recorded.** An item that reads an upstream result but was written before this system existed has no `consumed` entry, so the hash check is vacuously skipped and it's treated as resumable regardless of drift. Fixed by defaulting to non-resumable when `consumed` is absent for an item with declared `deps`, mirroring Day 48's fail-closed default for unkeyed effects.
- **Diamond dependencies double-counted.** An item with two upstream paths that both changed got queued for re-execution twice in the naive cascade walk, wasting a redo. `to_rerun` as a set (not a list) fixes the double-execution; the mark function was already idempotent per-node, the bug was in the caller appending instead of using the set's own membership check.
- **False cascade on semantically-identical reruns.** Item 1 re-executes, fetches ephemeris data that's numerically identical to the first run, but the hash differs due to a non-deterministic timestamp field in the response envelope. This forces an unnecessary cascade. No fix shipped today — flagged for Day 52.

## What's Next

Day 51's cascade is correctness-first and can be pessimistic: any hash drift, however cosmetically small, triggers a full downstream re-run. For batches with deep or highly connected dependency graphs, that's expensive. Day 52 looks at scoping the cascade — comparing the *semantic* content of a changed result against what the dependent actually used, rather than a raw hash, so a re-run only cascades when the drift would actually change the dependent's outcome.

*Orbital Watch: a multi-agent system that watches the sky so you don't have to.*
