# Day 84: When the Gate Isn't Enough — Staged Rollout and Automatic Rollback

## TL;DR
Every preflight check built across Days 79-83 evaluates a candidate mechanism inside a simulation — a simulated interleaving, a simulated burst, a simulated sustained load. That's the gate's entire nature: it runs *before* a mechanism ships, which means it can only ever test against conditions someone thought to simulate. Real production — real upstream flakiness shapes, real traffic correlation, real hardware and network behavior — is not reproducible by definition, because if it were fully reproducible in advance, it would just be another simulation. Today accepts that limitation instead of fighting it, and builds the thing that has to exist on the other side of it: a staged rollout system that treats "ships to production" not as a single irreversible event but as a monitored, reversible process, with automatic rollback wired directly into the same failure-mode taxonomy Day 82 built for preflight.

## The Problem
- A mechanism that clears every preflight check (concurrency, latency, adversarial-input, determinism, resource-exhaustion) has been validated against every failure mode anyone has thought to simulate — and nothing else. The taxonomy itself, per Day 82's own failure-modes section, cannot contain dimensions nobody has imagined yet, and the sustained-load simulator, per Day 83's own failure-modes section, cannot reproduce load shapes nobody thought to model.
- The pipeline currently has exactly one deployment mode: a mechanism passes the gate, or it doesn't, and if it does, it ships to 100% of traffic immediately. There is no concept of partial exposure, no observation window, no mechanism for catching a problem that only manifests under real conditions before it's affecting everything.
- This is a real gap, not a hypothetical one — Day 74's severity-estimated bypass path and Day 76's burst-testing both originated from production-shaped stress that nobody had simulated in advance; both would have been caught faster, and with less blast radius, by a staged rollout than by waiting for someone to notice after full deployment.
- The preflight gate and a staged rollout aren't doing the same job and one can't substitute for the other. The gate answers "does this mechanism violate a failure mode we already know to check for, under conditions we can simulate." A staged rollout answers a different question: "does this mechanism behave acceptably under conditions we *can't* simulate, observed at a blast radius small enough that being wrong is cheap." Treating gate-clearance as equivalent to production-readiness quietly assumes the first question's answer implies the second's, and today's whole premise is that it doesn't.

## Architecture

### 1. Canary stages as an explicit, declared sequence
A mechanism that clears the preflight gate doesn't go straight to full traffic — it enters a staged rollout defined as an ordered sequence of traffic percentages, each with a minimum observation duration and a set of health signals that must stay within bounds before advancing to the next stage.

```python
@dataclass
class CanaryStage:
    traffic_percent: float
    min_observation_minutes: int
    health_signals: list[str]   # keys into the live metrics registry

DEFAULT_CANARY_PLAN = [
    CanaryStage(traffic_percent=1.0,  min_observation_minutes=30,  health_signals=["error_rate", "p99_latency"]),
    CanaryStage(traffic_percent=10.0, min_observation_minutes=60,  health_signals=["error_rate", "p99_latency", "resource_bound_drift"]),
    CanaryStage(traffic_percent=50.0, min_observation_minutes=120, health_signals=["error_rate", "p99_latency", "resource_bound_drift", "determinism_sample_mismatch"]),
    CanaryStage(traffic_percent=100.0, min_observation_minutes=0,  health_signals=[]),
]
```

The health signals chosen for each stage aren't arbitrary — `resource_bound_drift` and `determinism_sample_mismatch` are live-production analogues of Day 83's and Day 81's preflight checks respectively, sampled continuously rather than simulated once. The staged rollout isn't a separate system bolted onto the gate; it's the same failure-mode vocabulary, re-applied with live data instead of simulated data.

### 2. Live health evaluation against the same taxonomy
Each health signal maps back to a taxonomy entry from Day 82, which means a canary failure can be classified using the exact same vocabulary as a preflight failure — "this canary failed because of a `DATA_RACE` symptom" is now a sentence that means the same thing whether it was caught in simulation or in production, and feeds back into the same tracking.

```python
HEALTH_SIGNAL_TO_TAXONOMY = {
    "error_rate": {"DATA_RACE", "LOGICAL_RACE", "ADVERSARIAL_INPUT_SENSITIVITY"},
    "p99_latency": {"LATENCY_BUDGET_VIOLATION"},
    "resource_bound_drift": {"RESOURCE_EXHAUSTION"},
    "determinism_sample_mismatch": {"LOGICAL_RACE", "STALE_READ"},
}

def evaluate_canary_stage(stage: CanaryStage, metrics: LiveMetricsRegistry) -> StageResult:
    violations = []
    for signal in stage.health_signals:
        current, baseline = metrics.get_current_and_baseline(signal)
        # reuses day 67/68's regime-segmented baseline + hysteresis machinery —
        # a canary stage IS a regime transition, just one triggered by a
        # deployment event instead of a detected environmental shift
        if is_significant_deviation(current, baseline, hysteresis=REGIME_HYSTERESIS[signal]):
            violations.append((signal, current, baseline, HEALTH_SIGNAL_TO_TAXONOMY[signal]))
    return StageResult(stage=stage, passed=not violations, violations=violations)
```

Reusing Day 67 and 68's regime-hysteresis machinery here isn't incidental — a canary rollout *is* a deliberate regime transition, triggered by a deployment rather than a detected environmental shift, and the same "don't flap on noise, but don't ignore a real shift either" logic applies directly.

### 3. Automatic rollback, not a paged human deciding under pressure
A stage that fails evaluation triggers automatic rollback to zero traffic for the new mechanism, falling back to whatever was running before — not a page asking a human to decide in real time whether a metric blip is real, which is exactly the kind of decision Day 46's suspect-state circuit breaker already established shouldn't be made by a human under time pressure with incomplete information.

```python
def run_canary_rollout(mechanism, plan: list[CanaryStage], router: TrafficRouter):
    for stage in plan:
        router.set_traffic_percent(mechanism, stage.traffic_percent)
        wait_until(stage.min_observation_minutes)
        result = evaluate_canary_stage(stage, live_metrics_registry)
        if not result.passed:
            router.set_traffic_percent(mechanism, 0.0)
            incident_log.record(
                mechanism=mechanism.name,
                stage=stage.traffic_percent,
                violations=result.violations,
                action="automatic_rollback",
            )
            raise CanaryRollbackTriggered(mechanism.name, stage, result.violations)
    router.set_traffic_percent(mechanism, 100.0)
```

### 4. Every canary rollback feeds back into the taxonomy, the way Day 82 intended
This is the step that closes the loop between production reality and preflight coverage. A rollback whose violations map to an *existing* taxonomy dimension is logged as evidence that a preflight check for that dimension either doesn't exist yet or isn't strict enough — a real, measurable count of production failures the gate is currently missing. A rollback whose symptoms don't cleanly map to any existing dimension is exactly how a genuinely new taxonomy dimension gets proposed, following Day 82's `propose_taxonomy_addition` discipline, except now grounded in a live incident instead of a hypothetical.

```python
def triage_rollback(rollback: CanaryRollbackTriggered):
    mapped_dims = set()
    for signal, current, baseline, dims in rollback.violations:
        mapped_dims |= dims
    if mapped_dims:
        preflight_gap_evidence.record(mechanism=rollback.mechanism, dims=mapped_dims,
                                        note="caught in canary, not in preflight — check needs strengthening")
    else:
        propose_taxonomy_addition(
            dimension=f"UNCLASSIFIED_{rollback.mechanism}_{rollback.stage.traffic_percent}",
            definition="production symptom with no mapped taxonomy dimension — needs manual review",
            evidence=f"canary rollback at {rollback.stage.traffic_percent}% traffic",
        )
```

## Failure Modes
- **A canary at 1% traffic can still miss a failure mode that only manifests under a specific real-world correlation** — a single upstream outage window, a specific satellite pass, a specific combination of NOAA alert tier and NeoWs refresh timing — if that condition simply didn't occur during the observation window. Staged rollout reduces blast radius; it does not guarantee exposure to every condition that matters, and a mechanism can clear all four stages purely because the unlucky conditions happened not to occur in 30+60+120 minutes of real time.
- **The regime-hysteresis reuse from Day 67/68 means a canary stage can be slow to flag a real regression if that regression looks similar enough to normal noise** — the same false-negative risk that hysteresis was built to accept in exchange for not flapping on every blip. A deliberate design tradeoff, but one that now applies to live production decisions with real blast radius, not just to internal regime bookkeeping.
- **Automatic rollback assumes "fall back to the previous mechanism" is always safe**, which isn't universally true — if the new mechanism changed shared state in a way the old mechanism doesn't know how to interpret (a schema change, a new field other consumers started depending on mid-canary), rolling back the code without also handling the state it left behind can produce a *second*, different failure on top of the first.
- **The feedback loop into preflight coverage (step 4) only works if rollback violations get triaged promptly** — an `incident_log` entry that nobody reviews for weeks is just as useless as the taxonomy gap it was meant to surface, and nothing in today's design enforces that review actually happens; it's a process dependency sitting on top of a code dependency, which is exactly the kind of thing that erodes quietly under deadline pressure.

## What's Next
The canary plan today is a single fixed sequence — 1%, 10%, 50%, 100%, with fixed observation windows chosen by gut feel, not by any principled measure of how much observation is actually enough to be statistically confident a stage is healthy. Day 85 asks the obvious next question: how long should a canary stage actually run, and how large a sample does it actually need, before "no violation detected yet" becomes a statistically meaningful statement instead of just "we got lucky for the time we were watching" — reusing Day 65's statistical drift detection, but turned toward a fundamentally different kind of decision.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
