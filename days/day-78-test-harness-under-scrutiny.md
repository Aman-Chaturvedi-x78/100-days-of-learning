# Day 78: Turning the Arc's Own Standard on the Test Harness

## TL;DR
Day 77 named the double standard directly: production mechanisms in this arc earned trust through replay evidence (Day 49), persistence gates (Day 56, 65), and probation periods (Day 63) — the testing infrastructure that shipped on Days 76 and 77 earned none of that, built and trusted within a single day under pressure to find bugs fast. Today applies the arc's own standard retroactively. The dimension set from Day 77 gets evidenced rather than assumed, the lock-ordering fix gets checked for the contention cost it was flagged as possibly introducing, and the whole harness gets a trust-scoring treatment modeled directly on Day 59's analyzer trust — because a test harness that's wrong in a way that looks like "no bugs found" is exactly as dangerous as the silent missed-cascade failure mode this series has flagged as worst-case since Day 52.

## The Problem
- Day 77's `BURST_DIMENSIONS` was declared by one person reasoning about what seemed likely to matter — shape, severity mix, commit count, jitter — with no evidence that these four dimensions are actually the ones that drive real concurrency bugs, as opposed to some fifth dimension nobody thought to include. This is structurally identical to Day 54's original hand-declared read-sets, which this series spent Days 55 through 58 systematically replacing with runtime-observed, evidenced alternatives — but the dimension set has had none of that treatment.
- Day 77's lock-acquisition-order fix was flagged, in its own failure modes, as potentially reducing the concurrency gains Day 75 was built to capture, but nobody has actually measured whether that cost is real or how large it is. An unmeasured performance regression hiding behind a correctness fix is exactly the kind of thing Day 74 spent an entire day trying to surface for the detection pipeline itself — and the fix for that pipeline has not been held to the same standard.
- Most importantly: a test harness that reports "no bugs found" is trusted by default, the same way Day 59 flagged that a static analyzer's predictions were trusted by default before that trust was made to be earned through reconciliation against ground truth. If the combinatorial generator has a gap — a scenario class it structurally cannot produce, a dimension it's missing — "all scenarios passed" becomes indistinguishable from "we never tested the case that would have failed," and nothing today tells those two states apart.

## Architecture

### 1. Dimension coverage evidenced against production incident history, not assumed
Rather than trusting that `BURST_DIMENSIONS` captures what matters, every concurrency-related bug found from here forward — starting retroactively with Day 76's lost-increment bug and Day 77's deadlock — gets classified against which declared dimensions it depended on. A dimension set is only "evidenced" once enough distinct incidents have been explained entirely within it; an incident that required a condition outside the declared dimensions is logged as a coverage gap, mirroring Day 49's drift-check replay reasoning applied to test design instead of production behavior.

```python
def classify_incident_against_dimensions(incident, dimensions):
    explained = all(incident.triggering_conditions.get(d) in dimensions[d] for d in dimensions)
    if not explained:
        COVERAGE_GAPS.append({"incident": incident, "missing_factor": incident.unexplained_conditions})
    return explained
```

### 2. Lock-ordering contention cost, measured rather than estimated
Day 77's global lock order is benchmarked directly against the pre-ordering concurrent implementation from Day 75, under the same combinatorial burst suite, specifically measuring wall-clock time where a task needs more than one ordered lock — turning the flagged-but-unmeasured risk into an actual number, the same move Day 74 made for detection latency.

```python
def measure_ordering_overhead(scenarios):
    unordered_times = [run_with_acquisition_strategy(s, ordered=False) for s in scenarios]
    ordered_times = [run_with_acquisition_strategy(s, ordered=True) for s in scenarios]
    return {
        "median_overhead_pct": percentile_overhead(unordered_times, ordered_times, 50),
        "worst_case_overhead_pct": percentile_overhead(unordered_times, ordered_times, 99),
    }
```

### 3. Harness trust scoring, modeled on Day 59's analyzer trust
The combinatorial burst suite's "no failures found" verdict is treated the same way Day 59 treated an unvalidated static analyzer's prediction — trusted only in shadow mode at first, with its track record checked against an independent signal (here, production incidents and targeted adversarial scenarios specifically constructed to try to break it) before a clean run is allowed to be read as strong evidence of correctness rather than merely an absence of evidence.

```python
def harness_trust_score():
    score = HARNESS_TRUST.setdefault("burst_suite", {"clean_runs_confirmed_safe": 0, "clean_runs_total": 0})
    return score["clean_runs_confirmed_safe"] / score["clean_runs_total"] if score["clean_runs_total"] else 0.0
```

### 4. Adversarial scenario injection as a standing check on the generator itself
Deliberately hand-construct scenarios designed to be hard for the combinatorial generator to produce naturally — edge-of-window timing, maximally asymmetric severity mixes, the specific shape-pair interactions that caused Day 77's deadlock — and verify the generator's sampling would eventually reach them, rather than assuming breadth from the dimension list alone.

```python
def verify_generator_can_reach(known_hard_scenario, dimensions, max_samples=10000):
    for _ in range(max_samples):
        if generate_combinatorial_scenarios(dimensions, sample_fraction=1/max_samples)[0] == known_hard_scenario:
            return True
    return False  # generator structurally cannot produce this scenario -> dimension gap
```

## Failure Modes
- **Evidencing against incident history has an inherent cold start.** Point 1 can only classify incidents that have already happened — a dimension gap that hasn't caused a bug yet is invisible to this scheme by construction, which is the same fundamental limitation every replay-evidenced mechanism in this arc has carried since Day 49, now acknowledged rather than ignored for test infrastructure specifically.
- **Measuring overhead doesn't resolve the tradeoff, it just makes it visible.** Point 2 turns an unknown cost into a known number, but a nontrivial measured overhead still leaves open the harder question of whether Day 77's deadlock-safety guarantee is worth that cost, or whether a narrower, lower-contention ordering could have achieved the same safety — today only produces the number, not the decision.
- **Trust scoring a test harness against incidents is a slower feedback loop than scoring production code.** Day 59's analyzer trust had frequent runtime calls to reconcile against; a burst-test harness only gets evidence when a real concurrency bug actually occurs in production, which is rare by design (the whole point of Days 75-77 was reducing that rate) — meaning `harness_trust_score` will accumulate very slowly and may never reach the same statistical confidence as faster-reconciling mechanisms elsewhere in this arc.
- **Adversarial scenario construction is itself a hand-picked activity.** Point 4 is explicitly designed to counter hand-picked bias, but the hard scenarios it checks against are still chosen by a person's judgment about what's hard — it narrows the blind spot without claiming to eliminate the category of blind spot entirely.
- **This is a meta-layer with real maintenance cost of its own.** Coverage-gap logging, overhead measurement, harness trust scoring, and adversarial verification are four new pieces of infrastructure watching the infrastructure that watches the pipeline — a third layer of indirection that needs its own upkeep as the underlying system continues to change.

## What's Next
With the testing apparatus itself now held to the arc's standard, the open thread running since Day 65 is nearly closed on the detection side — but the concurrency work from Days 75 through 78 was entirely reactive, triggered by a latency problem found on Day 74. The natural next question is whether the same rigor now applied after the fact to testing could be applied *before* building a new mechanism going forward, rather than this arc continuing to find its gaps only once they're already in production.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
