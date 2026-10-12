# Day 77: Combinatorial Burst Coverage — Testing the Space, Not the Sample

## TL;DR
Day 76's burst generator found a real bug, but only because the hand-picked scenario it happened to run against stressed the specific piece of unlocked state that was broken. Nothing proved the scenarios covered the system's actual risk surface — they were chosen the same ad hoc way every other test in this arc has been chosen since Day 56: by reasoning about what seemed likely to matter, not by systematically walking the space of what *could* matter. Today replaces hand-picked scenarios with generated coverage across the dimensions that actually drive concurrency risk — severity, shape, commit count, and timing jitter — and finds a second, subtler bug that none of the hand-picked scenarios from Day 76 would ever have surfaced: a deadlock between two stages waiting on each other's partial results under a specific, narrow timing window.

## The Problem
- Day 76's burst generator ran a small number of manually chosen scenarios — a handful of severity levels, a single shape, a fixed commit count and window. It found a bug, which created a false sense that burst testing as a practice was now validated, but the coverage itself was never actually measured against the full space of conditions the pipeline can encounter.
- The real risk surface has at least four independent dimensions that interact: which operation shape (different shapes have different `STAGE_DEPENDENCIES` subsets active, per Day 75), how severe the synthetic signal is (which determines whether Day 74's bypass path activates, changing which stages even run), how many commits arrive in the burst, and the timing jitter between them (commits arriving near-simultaneously stress the locks differently than commits spaced just inside the burst window). Day 76 tested a handful of points in this space, not the space itself.
- This matters specifically because concurrency bugs are famously sensitive to exact timing — the deadlock found today only manifests when a commit triggering Day 74's bypass path arrives within a narrow window of a commit that does *not* trigger the bypass, for the *same* shape, because the bypassed commit's quarantine-decision task (Day 75's `requires: [drift_detection, hysteresis_transition, correlation_shift_eval]`) ends up waiting on a hysteresis-transition task that the non-bypassed commit's path has already started but not finished, while that task is itself blocked waiting on the regime lock the bypassed commit's path is holding to update its own provisional state.
- None of Day 76's hand-picked scenarios mixed bypass and non-bypass commits in the same burst, because nobody writing Day 76 had a reason to think of that combination specifically — which is exactly the systemic problem today exists to fix: coverage chosen by what a person thinks to test is bounded by what that person thinks of.

## Architecture

### 1. Dimension-based scenario generation
Define the risk surface explicitly as a set of independent axes, then generate burst scenarios by sampling combinations across all of them rather than hand-selecting points.

```python
BURST_DIMENSIONS = {
    "shape": list(STAGE_DEPENDENCIES_BY_SHAPE.keys()),
    "severity_mix": ["all_low", "all_high", "mixed_50_50", "mixed_90_10"],   # bypass vs non-bypass ratio
    "commit_count": [5, 20, 100, 500],
    "jitter_ms": [0, 5, 50, 500],
}

def generate_combinatorial_scenarios(dimensions, sample_fraction=1.0):
    full_space = list(itertools.product(*dimensions.values()))
    if sample_fraction < 1.0:
        full_space = random.sample(full_space, int(len(full_space) * sample_fraction))
    return [dict(zip(dimensions.keys(), combo)) for combo in full_space]
```

### 2. Full-space run for small axes, sampled run for large ones
The full cross product is tractable for today's dimension sizes, but as more stages and shapes get added, pure exhaustive testing stops scaling — so the harness supports both exhaustive runs (used in today's actual testing, since the space is still small enough) and random sampling with a configurable fraction, for when the space grows past what's tractable to run on every change.

```python
async def run_burst_suite(dimensions, exhaustive=True, sample_fraction=0.1):
    scenarios = generate_combinatorial_scenarios(
        dimensions, sample_fraction=(1.0 if exhaustive else sample_fraction)
    )
    results = await asyncio.gather(*(run_single_burst_scenario(s) for s in scenarios))
    return [r for r in results if not r.passed]
```

### 3. The mixed-severity scenario surfaces the deadlock
Running the `mixed_50_50` and `mixed_90_10` severity-mix scenarios — specifically absent from Day 76's hand-picked set — against low jitter reliably reproduces the deadlock within the first several runs, confirming it's not a rare fluke but a structural gap in how Day 74's bypass path and Day 75's dependency graph interact under concurrency.

### 4. The fix: explicit lock ordering instead of implicit acquisition order
The deadlock exists because the bypassed and non-bypassed paths can acquire the regime lock and a hysteresis-transition future in opposite orders depending on which commit's task happens to run first. The fix imposes a single, declared global lock-acquisition order across all shared resources touched by the task graph, so no two paths can ever wait on each other in a cycle.

```python
LOCK_ACQUISITION_ORDER = ["regime_state", "field_profiles", "pairwise_profiles"]

async def acquire_in_order(*lock_names):
    for name in sorted(lock_names, key=LOCK_ACQUISITION_ORDER.index):
        await LOCKS[name].acquire()
```

## Failure Modes
- **Exhaustive testing doesn't stay exhaustive.** Today's full cross product is tractable because the dimension sizes are still small — adding one more shape, one more severity tier, or one more jitter bucket multiplies the space, and the harness's own design (point 2) acknowledges this will need to shift to sampling, at which point coverage becomes probabilistic again, just with better odds than Day 76's hand-picked approach.
- **The dimensions themselves are a judgment call.** `BURST_DIMENSIONS` declares four axes as the ones that matter, but that declaration is itself made by a person reasoning about what's likely to matter — exactly the same kind of hand-picked judgment call that limited Day 76, just applied one level up, at the level of choosing dimensions instead of choosing scenarios. A fifth, undeclared dimension that actually drives a real bug would be invisible to this entire framework.
- **A global lock-acquisition order is a strong, costly guarantee.** Point 4's fix eliminates deadlock risk by construction, but it also serializes access to every listed resource relative to every other one whenever a task needs more than one — a stage needing both `regime_state` and `field_profiles` now always acquires them in the same fixed order, which can reduce the concurrency gains Day 75 was built to capture if resource contention across the ordered locks turns out to be common.
- **Sampled coverage gives a confidence level, not a guarantee.** Once the space grows past exhaustive and the suite shifts to `sample_fraction < 1.0`, "no failures found" stops meaning "no bugs exist" and starts meaning "no bugs were found in the sampled subset" — a distinction that's easy to lose sight of once a test suite has been green for a while.
- **This whole apparatus is itself new and unvalidated by the standards this arc has held everything else to.** The combinatorial generator, the deadlock fix, and the lock-ordering discipline were all built and shipped in a single day, with none of the replay-evidenced, earned-trust treatment this series insisted on for far smaller mechanisms back on Days 49 and 59 — which is a double standard worth naming rather than quietly carrying forward.

## What's Next
Day 78: the double standard flagged above is the honest next problem. Testing infrastructure built this quickly, under its own pressure to "just find the bugs," hasn't been held to the same bar the rest of this arc has insisted on — no accumulated evidence that the dimension set is right, no replay-based validation that the lock ordering doesn't just move contention somewhere less visible. The next step is turning the same scrutiny this series applies to production mechanisms onto the test harness itself.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
