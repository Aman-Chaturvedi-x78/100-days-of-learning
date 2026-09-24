# Day 60: Mismatch Attribution — Who's Actually Wrong When They Disagree

## TL;DR
Day 59's trust scoring assumes every reconciliation disagreement is the analyzer's fault, sharply cutting trust on any mismatch. But Day 55 already flagged that runtime tracking itself can be an unreliable witness — a proxy-escape event (the tracked result serialized and re-parsed before a field is accessed) makes a correct analyzer prediction look wrong. Today's fix: don't charge a mismatch against analyzer trust until it's been attributed to the right culprit. A disagreement triggers a diagnostic mode — heavier, boundary-level instrumentation that independently corroborates whether the field was truly unused or whether runtime tracking simply missed it.

## The Problem
- Day 59's trust update treats a reconciliation disagreement as unambiguous evidence the static extractor was wrong, resetting trust immediately.
- But the runtime-observed read-set it's compared against (Day 55) has a known blind spot: if a dependent serializes the tracked result before accessing fields, the proxy's `__getitem__`/`__getattr__` tracking never fires, so the observed set under-reports what was actually used.
- In that scenario, the analyzer's prediction can be entirely correct while reconciliation flags it as a mismatch — and Day 59's design punishes the analyzer for a failure that belongs to the tracking layer, not the extractor.
- There's currently no way to tell these two failure modes apart at the moment a mismatch is detected — both look identical: "the static surface claims a field is used; the runtime read-set doesn't contain it."

## Architecture

### 1. Diagnostic mode instead of immediate penalty
A reconciliation mismatch no longer directly updates trust. It queues the dependent for diagnostic mode — non-sampled, maximally thorough instrumentation on its next several calls — before any trust consequence is applied.

```python
def on_mismatch(dependent_id, field):
    DIAGNOSTIC_QUEUE[dependent_id] = {"field": field, "calls_remaining": DIAGNOSTIC_WINDOW}
```

### 2. Serialization-boundary tracking as a second witness
A second, independent instrumentation point hooks the boundary where a tracked object actually leaves the process's control — serialization calls (`json.dumps`, request-payload assembly) — catching field usage that the in-process proxy from Day 55 misses when an object escapes it.

```python
def track_boundary_usage(dependent_id, payload):
    for field in extract_field_names(payload):
        BOUNDARY_OBSERVED.setdefault(dependent_id, set()).add(field)
```

### 3. Attribution logic
Once diagnostic mode completes its window, the boundary-level observation is compared against both the static prediction and the original in-process read-set to determine where the mismatch actually belongs.

```python
def attribute_mismatch(dependent_id, field):
    if field in BOUNDARY_OBSERVED.get(dependent_id, set()):
        # field really was used -> proxy missed it, not an analyzer error
        merge_into_read_set(dependent_id, field)
        return "runtime_tracking_gap"
    else:
        # boundary tracking agrees the field wasn't used -> analyzer was wrong
        update_trust(dependent_id, agreed=False)
        return "analyzer_error"
```

### 4. Inconclusive case stays neutral
If boundary tracking still finds no evidence either way — the escape happened through a channel neither the proxy nor the boundary hook covers — the mismatch is flagged for manual review and trust is left unchanged rather than guessing in either direction.

```python
def resolve_diagnostic(dependent_id, field):
    verdict = attribute_mismatch(dependent_id, field)
    if verdict is None:
        flag_for_manual_review(dependent_id, field)
    return verdict
```

## Failure Modes
- **Deferred trust decisions.** Diagnostic mode takes several calls to resolve, so a genuine analyzer bug gets a grace period before its trust penalty lands — during which an already-trusted dependent may still be using a wrong surface-scoped hash.
- **Boundary tracking has its own escape channels.** Passing the tracked result into compiled/native code, writing it directly to disk, or logging it through a path that bypasses both the proxy and the serialization hook still evades detection entirely — this narrows Day 55's gap, it doesn't close it.
- **False corroboration.** If a field name coincidentally appears in a serialized payload for an unrelated reason (e.g. a similarly-named field from a different structure), attribution could wrongly credit the runtime-tracking-gap verdict when the analyzer was actually correct all along.
- **Diagnostic overhead at scale.** Every mismatch across the dependent population queues its own diagnostic window — if mismatches spike (e.g. after a broad deploy touching many dependents at once), the heavier instrumentation cost compounds exactly when the system is under the most churn.

## What's Next
Day 61: boundary-level tracking narrows Day 55's proxy-escape gap but doesn't close it — a tracked result handed to native code, written straight to disk, or piped through a channel with no hook at all still escapes both witnesses. The open question is what to do about escape channels that no reasonable amount of instrumentation can observe.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
