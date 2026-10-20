# Day 83: Naming a Gap Is Free. Closing It Isn't — Building the Resource-Exhaustion Check

## TL;DR
Day 82's coverage report named `RESOURCE_EXHAUSTION` as a taxonomy dimension with zero checks pointed at it — a clean, satisfying line in a gap report. Writing the actual check turned out to be nothing like writing the other four. Concurrency, latency, adversarial-input, and determinism checks all evaluate a mechanism against a *fixed point*: run it once (or a bounded number of times) under controlled conditions and inspect the result. Resource exhaustion isn't a fixed-point property — it's a property of *trajectories under sustained load*, and a mechanism can be perfectly correct at every individual point in time while still being a slow-motion outage waiting for enough traffic. Today builds a check that actually measures that, and in doing so, finds a real unbounded-growth bug in a mechanism that every other check in the gate had already cleared.

## The Problem
- Every check built so far (Days 79-81) answers a yes/no question about a *single evaluation* of a candidate mechanism: does this one run race? does this one run exceed budget? does this one run behave adversarially? is this one run deterministic across interleavings? None of them run the candidate repeatedly under sustained conditions and watch what happens to the system's internal state *over* that sequence of runs.
- Resource exhaustion is specifically a property of that missing dimension. A retry policy that's individually correct — it retries on failure, it eventually succeeds or gives up — can still be a resource-exhaustion bug if its queue of pending retries grows faster than it drains under sustained upstream degradation, because "eventually succeeds or gives up" says nothing about how long "eventually" takes relative to arrival rate.
- This is exactly the shape of failure the pipeline has already had, informally: Day 42's migration admission controller needed explicit admission control because unbounded concurrent migrations would have degraded the whole region, and Day 76's burst stress testing found a lost-increment bug specifically because it finally applied *sustained* load instead of isolated test cases. Both of those were found by load-testing the *whole system*. Neither of them was ever turned into a *preflight* check that could catch the same class of bug in a *new* mechanism before it ships, which is precisely the gap Day 82's taxonomy named.
- The honest reason this is hard: a resource-exhaustion check has no single "did it pass" moment. It has to define what "exhaustion" even means for an arbitrary candidate — bounded queue depth? bounded retry count? bounded memory for accumulated state? — and those definitions have to come from the candidate's own `DesignIntent` declaration (Day 79), not be invented fresh for each mechanism, or the check becomes unmaintainable the moment a second resource-bound mechanism is proposed.

## Architecture

### 1. Extending `DesignIntent` with declared resource bounds
Every candidate that touches a queue, a retry loop, or accumulating state must now declare its bounds explicitly, the same way Day 79 required declared reads/writes and Day 74 required declared latency budgets. A mechanism with no such resources declares an empty set and is exempt from this check — not every mechanism needs it, and forcing a declaration on ones that don't would just produce noise.

```python
@dataclass
class ResourceBound:
    resource_name: str       # e.g. "retry_queue_depth", "pending_checkpoints"
    max_value: int
    growth_model: str        # "bounded" | "steady_state" | "drains_under_backpressure"

@dataclass
class DesignIntent:
    name: str
    reads: set[str]
    writes: set[str]
    requires: set[str]
    expected_latency_ms: tuple[int, int]   # (best_case, worst_case), from Day 74
    resource_bounds: list[ResourceBound] = field(default_factory=list)
```

### 2. Sustained-load simulation instead of single-shot evaluation
The check runs the candidate under a simulated sustained-load harness — not Day 76's burst generator directly (bursts are short, intense spikes; this needs long, steady pressure to see accumulation), but a related harness that holds load at a declared fraction of the system's admission-controlled capacity (Day 42) for a simulated extended duration, sampling each declared resource at fixed intervals.

```python
def preflight_resource_exhaustion_check(intent: DesignIntent, candidate_fn) -> CheckResult:
    if not intent.resource_bounds:
        return CheckResult(passed=True, note="no declared resource bounds, exempt")

    sim = SustainedLoadSimulator(
        duration_simulated_minutes=120,
        sample_interval_seconds=30,
        load_fraction_of_capacity=0.85,   # deliberately below the admission-control
                                            # ceiling from Day 42 — this isn't testing
                                            # overload, it's testing steady-state drift
    )
    samples = sim.run(candidate_fn, intent)

    violations = []
    for bound in intent.resource_bounds:
        series = [s[bound.resource_name] for s in samples]
        if bound.growth_model == "bounded" and max(series) > bound.max_value:
            violations.append((bound.resource_name, "exceeded max_value", max(series)))
        elif bound.growth_model == "steady_state" and is_monotonically_increasing(series, tolerance=0.05):
            violations.append((bound.resource_name, "unbounded growth under sustained load", series[-1]))
        elif bound.growth_model == "drains_under_backpressure" and not drains_within(series, window=10):
            violations.append((bound.resource_name, "fails to drain after backpressure signal", series))

    return CheckResult(passed=not violations, violations=violations)
```

`is_monotonically_increasing` with a tolerance matters here — real systems have noise, and a check that fires on any single uptick would be useless. The tolerance is itself a threshold-registry entry (Day 73), not a magic number buried in this function.

### 3. The check immediately finds a real bug — in `preflight_latency_check`'s own retry wrapper
Running this against the gate's own existing mechanisms as a sanity check (the same instinct that drove Day 78's retroactive scrutiny of the test harness) turned up something: `preflight_latency_check` itself, from Day 79, wraps each simulated candidate execution in a bounded-retry loop to tolerate flaky simulation timeouts — and that retry loop's pending-retry queue was never declared, never bounded in code, and grows without limit if a candidate's simulated execution degrades mid-check rather than failing cleanly. The gate's own machinery had exactly the failure mode it was built to catch.

```python
# Before — unbounded retry accumulation, no declared bound, no backpressure
class LatencyCheckRunner:
    def __init__(self):
        self.pending_retries = []   # never capped, never drained proactively

    def run_with_retry(self, candidate_fn, intent):
        try:
            return candidate_fn(intent)
        except SimulationTimeout:
            self.pending_retries.append((candidate_fn, intent))
            return self.run_with_retry(candidate_fn, intent)  # unbounded recursion too
```

```python
# After — declared bound, explicit cap, backpressure instead of unbounded recursion
class LatencyCheckRunner:
    MAX_PENDING_RETRIES = 50

    def __init__(self):
        self.pending_retries = deque(maxlen=self.MAX_PENDING_RETRIES)

    def run_with_retry(self, candidate_fn, intent, max_attempts=5):
        for attempt in range(max_attempts):
            try:
                return candidate_fn(intent)
            except SimulationTimeout:
                self.pending_retries.append((candidate_fn, intent, attempt))
        raise PreflightCheckInconclusive(
            f"latency check timed out {max_attempts}x — treating as a check "
            f"failure, not silently retrying forever"
        )
```

The fix also changes the failure semantics: a candidate that can't even get a conclusive latency measurement after bounded retries now fails the *gate*, rather than silently consuming unbounded runner resources while the author waits. That's a meaningfully different, and more honest, default than "keep trying forever and hope."

### 4. Registering the new check against Day 82's coverage map
Per Day 82's mandatory-declaration rule, the new check can't be added without declaring what it covers — and the honest declaration is narrower than it might first appear. It catches unbounded growth and failure-to-drain under sustained steady load; it does *not* catch a resource spike during a sudden burst, which is a different dimension (closer to `ADVERSARIAL_INPUT_SENSITIVITY` crossed with load, not yet its own named dimension).

```python
register_check(
    "preflight_resource_exhaustion_check",
    preflight_resource_exhaustion_check,
    covers={"RESOURCE_EXHAUSTION"},
)
```

```
>>> coverage_report(FAILURE_MODE_TAXONOMY, CHECK_COVERAGE)
{
  "covered": ["ADVERSARIAL_INPUT_SENSITIVITY", "DATA_RACE", "DEADLOCK",
              "LATENCY_BUDGET_VIOLATION", "LOGICAL_RACE", "RESOURCE_EXHAUSTION"],
  "uncovered": ["STALE_READ", "UPSTREAM_CORRELATION_BLINDNESS"],
  "coverage_ratio": 0.75
}
```

## Failure Modes
- **Sustained-load simulation is itself a simulation, and simulations lie in a specific, consistent direction: they only reproduce conditions someone thought to model.** `SustainedLoadSimulator` holds load at a fixed fraction of admission-controlled capacity for a fixed simulated duration — real production traffic has daily cycles, correlated bursts (Day 71), and failure cascades that a steady 85%-of-capacity simulation will never recreate. The check catches "grows without bound under *this* load shape"; it cannot catch "grows without bound only under *this specific other* load shape nobody simulated."
- **The `growth_model` classification is a judgment call made at declaration time, and a wrong classification produces false confidence rather than an honest failure.** A resource declared `"drains_under_backpressure"` that actually behaves as `"steady_state"` under a backpressure signal the author didn't anticipate will pass the check while still being capable of unbounded growth under real conditions — the bound was declared wrong, not missing, and a wrong bound is harder to notice than an absent one.
- **Fixing the gate's own retry-queue bug changes the gate's failure semantics in a way that could itself regress candidates that depended on the old "retry forever" behavior**, even though nothing used that behavior intentionally — it's worth noting explicitly because this is the second time (after Day 78) that scrutiny applied to the gate's own machinery turned up something that needed a real fix, which says more about how much implicit trust gets extended to infrastructure code than about this particular bug.
- **Simulated duration is a proxy for real duration, and the proxy can be wrong in the direction of false safety.** A resource that would start growing unboundedly only after four real hours of sustained load, well past the 120 simulated minutes tested here, passes cleanly today and could still be a live incident in production. There's a real tension between keeping the gate fast enough that engineers actually run it before shipping and making the simulated duration long enough to catch slow-growth bugs — today's number is a starting guess, not a validated one.

## What's Next
`STALE_READ` and `UPSTREAM_CORRELATION_BLINDNESS` remain uncovered in the taxonomy, and both of today's failure modes point at the same underlying limitation: every check built across this entire arc — Day 79 through today — only evaluates mechanisms inside a *simulation*, however sophisticated. None of them have ever run against real, live upstream behavior, real traffic shape, real correlated failures. Day 84 asks what happens to a mechanism that passes every one of these checks cleanly and *still* breaks the first week it's actually live — and what a gate that only runs before shipping can possibly do about failures that, by definition, only exist after shipping.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
