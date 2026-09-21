# Day 55: Runtime-Traced Read-Sets — Stop Trusting Declarations

## TL;DR
Day 54's dependent-scoped hashing only works if the declared read-set is accurate — and hand-declared read-sets are exactly the kind of unverified assumption this series has been eliminating since Day 52's replay-evidence requirement. Today's fix: stop declaring read-sets and start observing them. Wrap each canonical result in a lightweight tracking object before it reaches a dependent, record every field actually accessed during a real run, and accumulate those observations into a read-set over time — with a conservative fallback for any dependent that hasn't been observed enough yet.

## The Problem
- A hand-declared read-set (Day 54) is only as good as a developer's memory of what their own code touches — easy to under-declare (missing a field → silent false negative, the read-set says a field is irrelevant when the code actually depends on it) or over-declare defensively (listing every field "just in case," which gives up all the benefit of scoping in the first place).
- Declarations don't update themselves. A refactor that starts reading a new field, or stops reading an old one, leaves the declared read-set silently wrong until someone notices a bad cascade or — worse — a missed one that only surfaces as a downstream correctness bug.
- There's currently no automatic way to observe what fields a dependent actually touches during execution — Day 54's registry is entirely static, populated once and never re-validated against reality.
- This is also a scaling problem: as the dependent population grows (Day 54's fallback means every undeclared dependent gets zero benefit), manually declaring and maintaining read-sets for dozens or hundreds of dependents becomes its own maintenance burden, independent of correctness risk.

## Architecture

### 1. Access-tracking wrapper
Before a canonical result reaches a dependent, wrap it in a proxy that records every field access into a per-call log instead of trusting a static list. The wrapper is transparent to normal dict-like access patterns so dependent code doesn't need to change to be tracked.

```python
class TrackedResult:
    def __init__(self, data, access_log):
        self._data = data
        self._access_log = access_log

    def __getitem__(self, key):
        self._access_log.add(key)
        return self._data[key]

    def __getattr__(self, key):
        self._access_log.add(key)
        return getattr(self._data, key)
```

For nested structures, the wrapper recursively wraps any dict or object value it returns, so a nested field access (`result.metadata.orbit_class`) is recorded at the full path, matching Day 54's dotted-path read-set format rather than only tracking top-level keys.

### 2. Read-set accumulation
A single run only exercises one code path, so the access log from one call is a lower bound, not the full picture. Union each run's observations into a running read-set per dependent rather than overwriting it.

```python
def record_access(dependent_id, operation_shape, access_log):
    key = (operation_shape, dependent_id)
    READ_SETS.setdefault(key, set())
    READ_SETS[key] |= access_log
```

### 3. Bootstrapping fallback
Until a dependent has accumulated any tracked runs at all, fall back to Day 53's full canonical hash — same conservative-default pattern used everywhere else in the series: no evidence, no shortcut.

```python
def scoped_hash(operation_shape, dependent_id, canonical_result):
    fields = READ_SETS.get((operation_shape, dependent_id))
    if not fields:
        return content_hash(canonical_result)
    scoped = {k: extract_path(canonical_result, k) for k in fields}
    return content_hash(scoped)
```

### 4. Instrumentation scope control
Tracking every field access on every run has a real runtime cost, so instrumentation is sampled rather than always-on once a dependent's read-set has stabilized enough to be useful — early runs are always tracked to bootstrap the set quickly, then tracking drops to a lower sampling rate to keep catching drift without paying full overhead indefinitely.

```python
def should_track(dependent_id, operation_shape, total_runs_seen):
    if total_runs_seen < BOOTSTRAP_RUN_COUNT:
        return True
    return total_runs_seen % SAMPLING_INTERVAL == 0
```

## Failure Modes
- **Coverage blindness.** A single run's access log only reflects the branch actually taken — a conditional path that reads a different field under conditions not yet exercised stays invisible, so the accumulated read-set can look stable while still being incomplete. This is the same coverage gap Day 54's declarations had, just moved from "a developer forgot" to "the observed traffic hasn't exercised it yet."
- **Proxy escape.** If a dependent serializes the tracked result (e.g. `json.dumps` then re-parses) before accessing fields, tracking loses visibility the moment the object leaves the wrapper — the access log undercounts silently, and there's currently no detection for when this happens.
- **Per-access overhead.** Wrapping every field access adds a small but real cost on every call, on top of Day 54's per-dependent scoped hashing — sampling (point 4) mitigates but doesn't eliminate this, and the sampling interval itself is a manually tuned constant with no principled derivation yet.
- **Sampling gaps.** Once instrumentation drops to sampled tracking, a rare branch that only gets exercised on unsampled runs can go unobserved indefinitely even though the dependent has technically been running long enough to have seen it — coverage and sampling rate aren't currently reconciled against each other.

## What's Next
Day 56: an accumulated read-set grows more complete with more runs, but there's currently no way to know whether it's actually *done* — whether every meaningful branch has been observed, or whether it just hasn't hit an untested one yet. Next is tracking coverage confidence, not just accumulation.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
