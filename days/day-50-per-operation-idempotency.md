# Day 50: Per-Operation Idempotency — When a Batch Call Is Only Partly Safe to Retry

**TL;DR:** Day 49's allowlist classified idempotency per *callee* — one verdict per integration. That breaks for batch endpoints, where the same callee can be safe to retry for some calls and not others depending on the shape of the request. Today: decomposing the effect ledger to per-item granularity, correlating batch responses back to individual idempotency keys, and collapsing to non-resumable only when the API gives us no way to tell items apart.

## The Problem

- A single callee (e.g. `POST /orders/batch`) can be idempotent for one call shape and not another — a batch of 10 items where all 10 carry client-supplied idempotency keys is safe to retry; the same endpoint called with 9 keyed items and 1 unkeyed one isn't, but Day 49's classifier only sees "this callee auto-verified" and treats the whole call as retryable.
- Even when every item *is* keyed, "retry the whole batch" isn't free — if 7 of 10 items already committed before the crash and only 3 are genuinely outstanding, a full-batch resume re-sends 7 no-op requests that some downstream systems don't dedupe cheaply (rate limits, audit logs, side-channel notifications fired on receipt, not on effect).
- Some batch APIs return per-item status in the response (which of the 10 succeeded, failed, or are still processing); others return a single batch-level status and give no way to know which items landed. The Day 48 ledger currently logs "call made" and "call succeeded" at the call level, so this per-item information — when it exists — is being thrown away.

## Architecture

### 1. Per-item ledger entries instead of per-call

The effect ledger now logs one entry per item inside a batch call, not one entry for the call as a whole. Each entry carries the item's own idempotency key (if the client supplied one) and a status that starts `pending` and is updated from the response.

```python
def log_batch_call(callee, batch_id, items):
    for item in items:
        ledger.write(
            callee=callee,
            batch_id=batch_id,
            item_key=item.idempotency_key,  # may be None
            status="pending",
        )
```

### 2. Response correlation, where the API supports it

If the response includes per-item results, each ledger entry is updated individually — `committed`, `failed`, or left `pending` if the response doesn't cover it (async batch APIs sometimes ack receipt before processing finishes).

```python
def reconcile_batch_response(batch_id, response):
    for result in response.per_item_results:
        ledger.update(
            batch_id=batch_id,
            item_key=result.key,
            status=result.status,  # committed | failed | pending
        )
```

### 3. Operation-shape classification, not callee classification

Day 49's tiered classifier (static signal auto-verify / self-declared contract / default non-resumable) now runs per **operation shape** — a (callee, has-per-item-keys, has-per-item-response) triple — rather than per callee. The same endpoint can land in different tiers depending on how a given call was made.

```python
def classify_operation(callee, items, response_schema):
    if not all(i.idempotency_key for i in items):
        return Tier.NON_RESUMABLE  # can't distinguish items on retry
    if response_schema.has_per_item_results:
        return Tier.AUTO_VERIFIED  # can resume just the outstanding subset
    return callee_contract_tier(callee)  # fall back to Day 49's self-declared contract, whole-batch granularity
```

### 4. Partial resume

For an operation classified `AUTO_VERIFIED`, resume only replays items still `pending` in the ledger, not the whole batch. For anything coarser than that (no per-item response), resume behavior falls back to Day 49's per-callee verdict applied to the batch as a single unit — which for a self-declared contract with no dedupe guarantee still means "don't retry, restart from scratch."

## Failure Modes

- **Silent partial-key batches.** A batch where the client mixed keyed and unkeyed items used to get waved through if the callee itself was allowlisted. Now it's forced to `NON_RESUMABLE` at the operation-shape check, before Day 49's contract tier is even consulted — a missing key on *any* item poisons the whole batch's resumability, since there's no way to tell which unkeyed item, if any, already ran.
- **Response schema drift.** A downstream API that used to return per-item results and stopped (a version bump, a degraded-mode response) would have silently kept operations classified `AUTO_VERIFIED` on stale cached schema info. Fixed by classifying per actual response received, not a cached assumption — if the response for a given call doesn't include per-item results, that call's ledger entries stay batch-granularity even if a prior call from the same callee had them.
- **Pending-forever items.** Async batch APIs that ack receipt immediately and process later left some ledger entries stuck at `pending` past any reasonable resume window, with no `committed`/`failed` verdict ever arriving. Resume logic now treats a `pending` item past a configurable staleness threshold as unknown-state and defers to the callee's non-resumable fallback for that item specifically, rather than blocking the whole batch's resume decision on it indefinitely.

## What's Next

Day 50 assumes a batch is a flat list of independent items. Real batch APIs sometimes have inter-item dependencies within the same call — item 3 references the result of item 1. Per-item resume as built today would happily resume item 3 alone if item 1's status read `committed`, without checking whether item 1's *specific* committed result is the one item 3 depends on, versus a result from some earlier unrelated batch. Day 51 needs to look at whether the ledger should track result values, not just status, for operations with intra-batch dependencies.

*Orbital Watch: multi-agent space situational awareness, built one failure mode at a time.*
