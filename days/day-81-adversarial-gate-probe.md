# Day 81: Red-Teaming the Gate — Building Something Designed to Slip Through

## TL;DR
Day 80 proved the preflight gate can catch a problem it was somewhat primed to catch. Today deliberately builds a candidate mechanism engineered to look clean to all four of Day 79's checks — correctly declared locks, a `requires` edge that's technically accurate but misleadingly incomplete, a latency estimate that's honest in isolation but wrong in combination — specifically to find the gate's blind spots rather than hope they surface naturally. It works: the candidate passes every preflight check and still contains a real defect, because the defect lives in an interaction the gate's checks don't model at all — a *logical* race between two correctly-locked operations, where the lock prevents data corruption but not an outcome that depends on which one happens to run first.

## The Problem
- Day 79's four checks each validate a specific, narrow property: are declared writes locked (point 2), does the declared critical path stay under budget (point 3), and do targeted burst scenarios pass (point 4). None of them validate whether the *order* in which two independently-locked operations execute produces a consistent, intended result — they check for data races, not for logical races, and those are different failure classes that happen to look similar.
- This gap exists because every prior concurrency fix in this arc — Day 75's locking, Day 76's regime-state fix, Day 77's lock ordering — was aimed specifically at preventing corrupted or lost state. None of them were aimed at preventing two *individually correct*, properly serialized operations from producing a result that depends on arbitrary scheduling, because that class of bug hadn't been found yet. The gate inherited its checks from the bugs the arc already knew about, not from the full space of concurrency bugs that exist.
- A candidate built specifically to exploit this gap: a mechanism that reads Day 80's `adaptive_threshold_by_shape` and Day 71's `source_reliabilities` to compute a combined confidence score for a quorum decision, writing the result to a new field, `combined_confidence`. Both reads are correctly locked at their source. The write is correctly locked at its destination. `preflight_concurrency_check` passes cleanly — there's no unlocked state anywhere in the declared manifest.
- The actual defect: if the adaptive-threshold recalculation (Day 80) and a source-reliability update (Day 71, triggered by a newly resolved quorum event) both happen to fire in the same narrow window, `combined_confidence` can be computed from an old threshold and a new reliability, or a new threshold and an old reliability, depending purely on which lock this stage happens to acquire first — both results are "correct" in the sense that no data was corrupted, but they're *different* results for the same nominal input state, and nothing about which one a given run gets is deterministic or intended.

## Architecture

### 1. The adversarial candidate, declared honestly
The manifest is built to be truthful about every property the gate actually checks, which is exactly what makes it dangerous — it isn't lying to the gate, it's exploiting the gap between what the gate checks and what actually matters.

```python
adversarial_candidate = DesignIntent(
    stage_name="combined_confidence_score",
    reads=["adaptive_threshold_by_shape", "source_reliabilities"],   # both correctly locked
    writes=["combined_confidence"],                                   # correctly locked at destination
    requires=["adaptive_severity_threshold", "correlation_drift_eval"],  # both declared, both real
    new_thresholds=[],
    expected_latency_ms=2,  # genuinely accurate in isolation
)
```

### 2. All four preflight checks pass
`preflight_concurrency_check` finds every declared write locked. `preflight_latency_check` finds the critical path unaffected, since both declared `requires` are already on the path and this stage's own latency is negligible. `preflight_adversarial_check` runs Day 77's combinatorial burst suite against the declared dependencies and finds no data corruption, no lost updates, no deadlock — because there genuinely isn't one. Every check Day 79 built returns clean.

```python
preflight_concurrency_check(adversarial_candidate)   # passes
preflight_latency_check(adversarial_candidate, deadline_ms=CURRENT_BUDGET)   # passes
preflight_adversarial_check(adversarial_candidate)   # passes — no corruption found, because there isn't any
```

### 3. The fifth check: logical determinism verification
Closing the gap requires a check that doesn't exist yet — one that doesn't look for corrupted state, but for *non-deterministic but individually valid* state, by running the same input scenario multiple times under different artificial scheduling orders and comparing outputs rather than just checking each run's internal consistency.

```python
def preflight_determinism_check(intent: DesignIntent, scenario, trials=50):
    outputs = [run_with_forced_interleaving(intent, scenario, seed=i) for i in range(trials)]
    distinct_outputs = {canonicalize(o) for o in outputs}
    if len(distinct_outputs) > 1:
        raise DesignRejected(
            f"{intent.stage_name} produced {len(distinct_outputs)} distinct results "
            f"from {trials} scheduling orders of the same logical input — non-deterministic by construction"
        )
```

### 4. Fixing the actual candidate once the gap is found
With the real problem named, the fix is a snapshot-and-version pattern: `combined_confidence_score` reads both inputs' versions alongside their values, and only commits if both versions are still current at write time — otherwise it retries against the now-current values, borrowing directly from Day 64's epoch-based staleness detection rather than inventing a new mechanism.

```python
def compute_combined_confidence(shape):
    threshold, threshold_version = read_versioned("adaptive_threshold_by_shape", shape)
    reliability, reliability_version = read_versioned("source_reliabilities", shape)
    result = combine(threshold, reliability)
    commit_if_versions_unchanged("combined_confidence", shape, result,
                                  expected=(threshold_version, reliability_version))
```

## Failure Modes
- **The determinism check is expensive and still sampling-based.** Fifty forced-interleaving trials (point 3) is a sample of the real scheduling space, not an exhaustive proof — a race that only manifests under a specific, rare interleaving could still slip through, the same sampling-coverage limitation Day 77 already flagged for its combinatorial burst suite, now recurring for a check built to close a gap in that very suite.
- **This is now a fifth check layered onto four that were themselves added reactively.** The preflight gate started as Day 79's answer to the arc's reactive pattern, and today it grew reactively in response to a deliberately constructed attack — which is progress, since the attack was intentional rather than accidental, but it means the gate's own design is still accreting checks one discovered gap at a time rather than being derived from a complete model of what concurrency correctness actually requires.
- **Today's adversarial candidate was itself hand-constructed by someone who already knew the gate's internals.** Finding this gap required deliberately reasoning backward from "what does the gate NOT check" — a real-world new mechanism built in good faith, without that adversarial framing, might stumble into the same gap by accident just as easily as today's deliberate construction did, which is reassuring for how realistic the found bug is, but also means there's no guarantee this was the *last* gap a sufficiently motivated (or sufficiently unlucky) construction could find.
- **Logical non-determinism isn't always a bug.** `preflight_determinism_check` as written rejects any stage producing more than one distinct output across scheduling orders, but some legitimately concurrent designs might intentionally tolerate equally-valid alternative outcomes (a genuine race that doesn't matter) — the check as built today can't distinguish "this non-determinism is a real defect" from "this non-determinism is an accepted, harmless property," and would reject both identically.
- **Five checks is more process overhead stacked onto what Day 80 already found slow.** Each new check that closes a gap also adds to the preflight latency Day 80 experienced directly — there's a real, uncounted cost to how many layers this gate can accumulate before the "catch it before it ships" benefit starts being outweighed by how long it takes to ship anything at all.

## What's Next
With a fifth check in place closing the logical-determinism gap, the honest pattern across Days 79 through 81 is now visible on its own terms: build a gate, find what it misses, add a check, repeat. That's a legitimate way to harden a review process, but it has no natural end — each new check narrows the space of undetected defects without ever proving the space is empty. The next open question is whether there's a way to reason about the *completeness* of this gate's coverage directly, rather than continuing to discover its gaps one adversarial construction at a time.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
