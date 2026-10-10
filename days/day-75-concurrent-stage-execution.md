# Day 75: Concurrent Stage Execution — Fixing Latency for the Case That Needed It Most

## TL;DR
Day 74's bypass bought speed only for high-confidence signals, leaving the fundamental shape of the chain untouched: every stage still waits for the previous stage to fully conclude before starting its own work, even when two stages don't actually need each other's *verdicts* — only the same underlying *data*. Today restructures the pipeline so independent stages run concurrently against shared data as it arrives, and only join at the points where one stage's output is genuinely required as another's input. This lowers latency for the patient path too — the ambiguous, no-bypass case that Day 74 explicitly left unaddressed — rather than only creating an escape hatch around it.

## The Problem
- The chain as it existed through Day 74 is a strict sequence: drift detection (65) completes and confirms before corroboration/quorum resolution (66/70) begins, which completes before hysteresis entry confirmation (68) begins, which completes before correlation-shift evaluation (72) begins. Each stage's code literally calls the next only after its own confirmation finishes.
- Several of these stages don't actually have that dependency in substance. Corroboration-source querying (66/70) needs only a time window, not drift detection's conclusion — it can be issued the moment a commit arrives, in parallel with drift detection running against the same commit, since both only need the raw observation, not each other's output. Correlation-shift evaluation (72) similarly only needs the pairwise agreement outcome for that window, which itself only depends on corroboration results, not on whether drift detection or hysteresis have finished concluding anything.
- The real dependency graph is narrower than the code's sequential structure suggests: hysteresis (68) genuinely needs corroboration's status output to decide enter/exit, and quarantine decisions (via Day 64's pipeline) genuinely need a confirmed regime label before they can act — but drift detection and corroboration querying have no real ordering constraint between them, and neither does correlation-shift evaluation relative to drift detection.
- This gap exists because every stage in this arc was built and reasoned about in isolation, on its own day, bolted onto the end of whatever came before it — which was the right way to build each piece correctly, but it defaulted every new stage into "runs after the previous one" without ever asking whether that ordering was load-bearing or just an accident of the order the days were written in.

## Architecture

### 1. Explicit dependency declaration, reusing Day 73's `affects` graph
Day 73's threshold registry already declares which mechanisms affect which others for latency-accounting purposes. Today extends the same declarations to mean something executable: a `requires` edge means a genuine data dependency (must wait), while the absence of one means two stages can run concurrently against the same commit.

```python
STAGE_DEPENDENCIES = {
    "drift_detection": {"requires": []},                          # only needs the raw commit
    "corroboration_query": {"requires": []},                      # only needs the raw commit's window
    "hysteresis_transition": {"requires": ["corroboration_query"]},  # needs corroboration's status output
    "correlation_shift_eval": {"requires": ["corroboration_query"]}, # needs corroboration's pairwise outcome
    "quarantine_decision": {"requires": ["drift_detection", "hysteresis_transition", "correlation_shift_eval"]},
}
```

### 2. A commit-scoped task graph instead of a call chain
Each incoming commit spawns a small task graph built from `STAGE_DEPENDENCIES` rather than a function calling the next function directly — stages with no unmet `requires` start immediately and in parallel; a stage with unmet dependencies waits only for those specific upstream tasks, not for an arbitrary linear predecessor.

```python
async def process_commit(commit):
    tasks = {}
    for stage, meta in STAGE_DEPENDENCIES.items():
        tasks[stage] = asyncio.ensure_future(
            run_stage_when_ready(stage, meta["requires"], tasks, commit)
        )
    await asyncio.gather(*tasks.values())

async def run_stage_when_ready(stage, requires, tasks, commit):
    if requires:
        await asyncio.gather(*(tasks[r] for r in requires))
    return await STAGE_IMPLEMENTATIONS[stage](commit)
```

### 3. Latency recomputation with concurrency-aware critical path
`worst_case_detection_latency` and `best_case_detection_latency` (Days 73-74) are recomputed as the longest path through `STAGE_DEPENDENCIES`'s actual dependency graph rather than a flat sum across every stage — concurrent stages contribute only their own duration to the critical path, not an additive one, which is the structural reason this change lowers latency for every signal, not just the bypass-eligible ones from Day 74.

```python
def critical_path_latency(shape, use_bypass=False):
    def stage_duration(stage):
        return 0 if use_bypass and has_bypass_rule(stage) else estimate_wall_time(stage, shape)
    memo = {}
    def longest_path_to(stage):
        if stage in memo:
            return memo[stage]
        deps = STAGE_DEPENDENCIES[stage]["requires"]
        memo[stage] = stage_duration(stage) + (max((longest_path_to(d) for d in deps), default=0))
        return memo[stage]
    return max(longest_path_to(s) for s in STAGE_DEPENDENCIES)
```

### 4. Concurrent access to shared mutable state is now a real hazard, handled explicitly
Running drift detection and corroboration querying concurrently against the same commit means both can read and write adjacent entries in `FIELD_PROFILES`, `PAIRWISE_PROFILES`, and the threshold registry's decision log at the same time — previously impossible by construction, since the sequential chain never had two stages touching state simultaneously. Each shared structure gets a narrow per-key lock scoped to the specific `(shape, field)` or `(shape, pair)` being updated, rather than a single global lock that would serialize the very concurrency this day exists to introduce.

```python
async def update_profile_concurrently(key, value):
    async with FIELD_PROFILE_LOCKS[key]:  # per-key, not global
        FIELD_PROFILES[key].observe(value)
```

## Failure Modes
- **Concurrency bugs are a new category of failure this arc hasn't had to handle before.** Every mechanism built since Day 56 was reasoned about assuming a strict, single-threaded sequence of events — introducing genuine concurrency doesn't just speed things up, it opens the door to race conditions, partial-state reads, and ordering-dependent bugs that none of the prior 19 days' designs were ever tested against.
- **The `requires` graph is a manually declared claim, same risk as Day 73's `affects` graph.** Declaring that two stages have no real dependency is a judgment call about the system's actual semantics, and getting it wrong in the dangerous direction (declaring independence where a real dependency exists) would mean a stage acts on incomplete or stale input without any error ever surfacing — concurrency bugs of this kind tend to be intermittent and hard to reproduce, unlike the deterministic sequential bugs this arc has dealt with so far.
- **Per-key locking narrows contention but doesn't eliminate ordering subtleties.** Two concurrent updates to the same `(shape, field)` key are now serialized correctly by the lock, but the *order* in which they're serialized is no longer deterministic the way a sequential call chain was — a replay of the "same" event sequence can now produce different internal state orderings depending on scheduling, which complicates the decision-log tracing Day 73 built specifically to make past decisions reconstructable.
- **Concurrent stages racing to read corroboration's output introduce a new coordination surface.** Day 70 already flagged that corroboration resolution waits on the slowest registered source — concurrent stages dependent on that result (hysteresis, correlation-shift eval) must now correctly handle the case where they start executing, find the dependency not yet ready, and correctly suspend rather than proceeding on a stale or default value, which is new logic with its own correctness burden.
- **This doesn't eliminate genuine serial dependencies, it only removes the artificial ones.** Hysteresis still can't start before corroboration concludes, and quarantine still can't act before all three of drift detection, hysteresis, and correlation-shift evaluation conclude — the real floor on latency, set by genuinely necessary sequencing, remains, and today's fix only closes the gap between that real floor and the artificially longer sequential chain the code happened to be written as.

## What's Next
Day 76: the new concurrency-correctness burden is the most consequential thing this day introduced, and it's untested against anything resembling the actual event patterns this arc has spent weeks reasoning about — a genuinely concurrent burst of commits during a fast-escalating event (exactly the scenario Day 74 was trying to serve) is also exactly the scenario most likely to surface a race condition in the per-key locking introduced today. The next step is stress-testing the concurrent path specifically under burst conditions before trusting it for the severe-event case it was built to help.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
