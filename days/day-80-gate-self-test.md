# Day 80: The Gate's First Real Test — Running a Mechanism Through Itself

## TL;DR
Day 79 built a preflight gate and closed by admitting it had never been run against anything real. Today is that run. A genuinely new mechanism — per-shape adaptive deadline tuning for Day 74's bypass thresholds, motivated by the observation that a fixed `SEVERE_DEVIATION_REFERENCE` doesn't fit every operation shape equally well — gets built as a `DesignIntent` manifest first and pushed through Day 79's four preflight checks before a line of its implementation touches the live pipeline. The gate catches something real: a declared-safe write that, on inspection, actually reintroduces the exact kind of race Day 76 found, because the author's own mental model of "this field is append-only, it doesn't need a lock" turned out to be wrong under the specific concurrent access pattern the new mechanism would have created.

## The Problem
- Day 79 ended by naming its own gap honestly: `preflight_concurrency_check`, `preflight_latency_check`, and `preflight_adversarial_check` had been run zero times against a real candidate mechanism. Infrastructure that has only ever been exercised against its own test fixtures carries exactly the same unearned-trust risk Day 78 diagnosed in the burst harness — a gate that has never actually rejected anything is indistinguishable, from the outside, between "nothing has been wrong yet" and "the gate doesn't actually catch anything."
- The adaptive-deadline mechanism itself is a reasonable next step independent of today's testing goal: Day 74's `SEVERE_DEVIATION_REFERENCE` is a single global constant, but different operation shapes have different natural volatility — a shape whose quiet-period baseline is already noisy needs a higher bar for "severe" than one that's normally very stable, and treating them identically either under-triggers the bypass for noisy shapes or over-triggers it for stable ones.
- Building this as a `DesignIntent` first, rather than as code, is itself new discipline for this arc — every previous mechanism from Day 56 onward was designed and implemented together, in the same breath. Declaring reads, writes, and dependencies as a standalone artifact before writing the implementation is Day 79's process applied literally, not just referenced.

## Architecture

### 1. The candidate mechanism, declared as intent first
Per-shape deadline tuning needs to read each shape's historical severity-estimate distribution (from Day 74's `severity_estimate`) and maintain a per-shape adaptive threshold that's recalculated periodically rather than fixed globally.

```python
candidate = DesignIntent(
    stage_name="adaptive_severity_threshold",
    reads=["severity_estimate_history"],
    writes=["adaptive_threshold_by_shape"],
    requires=[],   # assumed independent of the main detection chain — see point 2
    new_thresholds=[Threshold(
        owner="day80", value=0.9, unit="fraction",
        affects=["correlation_persistence_events"],
        rationale="per-shape starting point before adaptation kicks in",
    )],
    expected_latency_ms=0,  # assumed negligible — periodic recalculation, not per-commit
)
```

### 2. Preflight concurrency check finds the real bug
`adaptive_threshold_by_shape` was declared as a write target, and the author's working assumption was that recalculation happens on a slow periodic timer, so it "obviously" wouldn't race with anything reading it per-commit via `BYPASS_RULES` (Day 74). `preflight_concurrency_check` (Day 79) doesn't know about timers or assumptions — it only checks the declared write against `LOCK_REGISTRY`, and `adaptive_threshold_by_shape` isn't in it, because nobody had declared a lock for a key that didn't exist yet.

```python
preflight_concurrency_check(candidate)
# DesignRejected: adaptive_severity_threshold writes to unlocked state: ['adaptive_threshold_by_shape']
```

This is the correct, mechanical rejection — but the more important finding comes from investigating *why* it was unlocked: tracing through how `BYPASS_RULES` (Day 74) actually reads this value during live bypass evaluation reveals that the "slow periodic timer" assumption was simply wrong. A burst of commits during a severity spike (exactly Day 76's stress scenario) can trigger several bypass evaluations in the same window the periodic recalculation is writing a new threshold — a genuine read-write race the author's mental model had reasoned past, not a bookkeeping omission.

### 3. Closing the gap: locking, and a second look at `requires`
The fix adds `adaptive_threshold_by_shape` to `LOCK_REGISTRY` with its own per-key lock, consistent with Day 75's pattern — and, because the concurrency investigation surfaced a real read dependency between this mechanism and Day 74's bypass evaluation that the original `requires: []` declaration missed, the manifest itself gets corrected before resubmission.

```python
candidate.requires = ["severity_estimate"]   # corrected: genuinely reads Day 74's live output, not just history
LOCKS["adaptive_threshold_by_shape"] = asyncio.Lock()
LOCK_REGISTRY.append("adaptive_threshold_by_shape")
```

### 4. Resubmission passes all four checks
With the lock registered and the dependency corrected, `preflight_concurrency_check` passes; `preflight_latency_check` confirms the corrected `requires` edge doesn't push the critical path over Day 74's budget (the recalculation itself stays off the hot path, only its *read* by bypass evaluation is on it, and that read was already accounted for structurally); and `preflight_adversarial_check` specifically exercises the burst-during-recalculation scenario that motivated point 3's fix, passing because the lock now serializes it correctly.

## Failure Modes
- **The gate found a real bug, but only because this candidate happened to touch bypass evaluation.** Today's result is genuinely encouraging evidence the gate works, but it's a sample size of one, and the mechanism chosen for this test wasn't picked at random — it was picked because it plausibly interacted with existing hot-path state, which made it more likely to trip the concurrency check than an average new mechanism would be. The gate's track record on a less obviously-risky candidate is still unknown.
- **The author's corrected mental model is now trusted the same way the original, wrong one was.** Point 3's fix relies on the author correctly reasoning through *why* the rejection happened and fixing the actual dependency, not just satisfying the mechanical check by adding a lock without understanding the race — a developer who adds the lock reflexively, without tracing the read-write interaction the way today's investigation did, would pass the gate with a technically-locked but still conceptually misunderstood design.
- **`expected_latency_ms: 0` for the periodic recalculation was accepted at face value in point 1.** Nothing in today's run actually verified that assumption — the corrected `requires` edge changed what gets checked, but the latency estimate for the recalculation stage itself remains exactly as unverified as Day 79 flagged self-reported estimates would be.
- **This whole exercise still hasn't tested the gate's most important failure mode: letting something bad through.** Today demonstrates the gate catching a real problem, which is the easier direction to validate — the harder, unproven direction is whether the gate has blind spots that would let an equally real problem pass silently, which only shows up when something gets through the gate and still breaks later, the same asymmetry Day 78 flagged for the burst harness.
- **Process overhead is now visible, not just theoretical.** Writing the `DesignIntent` manifest, running it through rejection, investigating the real cause, correcting the manifest, and resubmitting took meaningfully longer than this arc's typical single-day build-and-ship cadence — exactly the friction Day 79 predicted, now experienced rather than estimated.

## What's Next
Day 81: today validated the gate's ability to catch a problem it was somewhat primed to catch. The next, harder test is the one flagged above — whether the gate has its own blind spots, the same question Day 78 asked of the burst harness. The honest way to probe that isn't to keep building mechanisms that plausibly trip the existing checks; it's to deliberately construct a candidate designed to look safe to all four preflight checks while still carrying a real defect, and see whether anything catches it.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
