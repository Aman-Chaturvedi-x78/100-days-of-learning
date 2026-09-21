# Day 56: Coverage-Aware Confidence — Knowing When a Read-Set Is Done

## TL;DR
Day 55's accumulated read-sets grow more complete with more observed runs, but nothing tracks whether a read-set is actually *finished* — whether every meaningful code path has been observed, or it just hasn't hit an untested branch yet. Today's fix: track coverage confidence per dependent using path fingerprints, and treat a read-set as provisional — falling back to Day 53's full canonical hash instead of Day 54's scoped hash — until enough distinct execution paths have been observed to trust it.

## The Problem
- A dependent's accumulated read-set (Day 55) only reflects branches actually taken during observed runs. A conditional path exercised rarely — or not yet at all — can read a field that's simply missing from the set, and the accumulation logic has no way to distinguish "this field genuinely isn't read" from "this field is read only under a condition we haven't hit yet."
- If that unobserved field later changes, the dependent-scoped hash (Day 54) won't notice, because the field was never in the read-set to begin with. That's a silent missed cascade — the exact failure mode this series has flagged as worse than an unnecessary one, every time it's come up since Day 52's under-triggering risk.
- There's currently no signal distinguishing "this read-set is well-observed and stable" from "this read-set just hasn't been tested enough to know" — both look identical from the outside, since both simply mean "no new fields added recently."
- Day 55's sampling-based instrumentation (point 4 of that day) makes this worse in a subtle way: once tracking drops to a sampled rate, the system can go a long time without observing a rare branch even on a dependent that's technically been running continuously, so elapsed time or total run count alone is a poor proxy for actual coverage.

## Architecture

### 1. Path fingerprinting
Alongside the field-access log, record a lightweight fingerprint of the code path taken on each run — a hash of the sequence of branch outcomes, captured where the dependent exposes hooks for it (e.g. a decorator around conditional blocks, or instrumentation at known branch points).

```python
def record_run(dependent_id, operation_shape, access_log, path_fingerprint):
    key = (operation_shape, dependent_id)
    READ_SETS[key] |= access_log
    PATH_FINGERPRINTS.setdefault(key, set()).add(path_fingerprint)
```

### 2. Coverage confidence
Confidence isn't a fixed run count — branch-space size varies per dependent, and a dependent with three branches converges much faster than one with thirty. Instead, track runs-since-last-new-path as a stabilization signal: if no new path has been seen in a while, treat the read-set as converged.

```python
def coverage_confidence(key, runs_since_new_path, threshold=50):
    return runs_since_new_path >= threshold
```

### 3. Conservative fallback below threshold
`is_resumable_for` only uses the scoped hash once confidence crosses the threshold. Below it, falls back to the full canonical hash — the same conservative-default pattern that's run through every day of this stretch since Day 52.

```python
def is_resumable_for(item, dependent_id, ledger):
    key = (item.operation_shape, dependent_id)
    confident = coverage_confidence(key, runs_since_new_path.get(key, 0))
    for upstream_id, consumed_hash in item.dependencies.items():
        shape = ledger.get(upstream_id).operation_shape
        canonical = ledger.get(upstream_id).canonical
        current = (scoped_hash(shape, dependent_id, canonical) if confident
                    else content_hash(canonical))
        if current != consumed_hash:
            return False
    return True
```

### 4. Sampling-rate coupling
Day 55's tracking sampling rate is now tied to coverage confidence instead of a flat schedule: a dependent below the confidence threshold keeps full tracking (every run observed) regardless of run count, and only drops to sampled tracking once it has actually converged — closing the gap where sampling could mask an unobserved rare branch indefinitely.

```python
def should_track(dependent_id, operation_shape, total_runs_seen):
    key = (operation_shape, dependent_id)
    if not coverage_confidence(key, runs_since_new_path.get(key, 0)):
        return True  # not yet confident -> track every run
    return total_runs_seen % SAMPLING_INTERVAL == 0
```

## Failure Modes
- **Arbitrary threshold.** "50 runs since a new path" is a heuristic, not a proof — it trades false confidence against how long a dependent stays on the more expensive full-hash path, and there's no principled way yet to set it per dependent based on actual branch complexity.
- **Low-traffic dependents never converge.** A dependent that runs rarely may never accumulate enough runs to cross the threshold, staying on the conservative (and more expensive) path indefinitely — which is safe, but permanently gives up the optimization Day 54 was built for, exactly for the dependents where the cost of full hashing is proportionally most noticeable relative to their total call volume.
- **Hook dependency.** Path fingerprinting needs the dependent to expose some notion of its own branch structure, which not every dependent provides — dependents without hooks can only ever fall back to run-count heuristics with weaker guarantees, or stay permanently unconfident.
- **Fingerprint collisions.** A coarse path fingerprint (e.g. hashing only the top-level branch taken, not the full call stack of decisions) can conflate two genuinely different paths into one fingerprint, understating the true branch space and triggering false convergence — confidence would report "done" when a real path is still unobserved.

## What's Next
Day 57: coverage confidence assumes a dependent's own code doesn't change while its read-set is being learned. A redeploy changes what the dependent reads without touching anything upstream — and nothing today detects that, so a stale read-set from before a redeploy could keep being trusted long after it stops reflecting reality.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
