# Day 79: Pre-Ship Adversarial Review — Scrutiny Before the Mechanism Ships, Not After

## TL;DR
Days 75 through 78 were entirely reactive: a latency number Day 74 happened to compute triggered a restructuring, which introduced a concurrency bug nobody designed a test for until after it shipped, which was only caught by a burst generator built after the fact, whose own coverage gaps were only found by accident. Every fix in that chain was a response to damage already done. Today builds the thing Day 78 explicitly asked for: a pre-ship review gate that runs the arc's own accumulated scrutiny — dimension classification, adversarial scenario construction, lock-ordering analysis, latency-chain impact — against a new mechanism *before* it's allowed into the pipeline, modeled directly on Day 73's threshold registry but aimed at the design itself rather than at a tunable constant.

## The Problem
- Every mechanism in this arc from Day 56 onward entered the pipeline the same way: get built, get reasoned about in isolation, ship, and only face systematic scrutiny once something downstream broke or a cross-cutting problem (like Day 73's latency computation) happened to surface it. Day 65's drift detector wasn't checked against corroboration until Day 66 found the gap by inspection. Day 71's quorum wasn't checked for correlated sources until Day 71 itself, written as a direct sequel to Day 70's shipped design.
- This pattern repeated at the meta level too — the test harness built across Days 76-77 had exactly the same shape: shipped first, scrutinized only after Day 77 noticed the gap, formalized on Day 78. The arc has now built the exact same realization twice, once for production mechanisms and once for the tests meant to validate them, without ever generalizing the lesson into something that runs *before* either one ships.
- The cost of this pattern is concrete and already measured: Day 74's latency problem existed, unnoticed, from the moment Day 65 through Day 72 were each individually, locally reasonable decisions — it took nine days of accumulated unreviewed design before anyone checked whether the combination still worked. A pre-ship check wouldn't have prevented any single day's design choice from being locally correct, but it could have flagged the cumulative latency cost at Day 68 or Day 70 instead of Day 74, and the burst-testing gaps at Day 75 instead of Day 76.
- Nothing about this is specific to any one mechanism's content — it's a process gap. The arc has excellent tools for scrutinizing something once it exists (Day 73's registry, Day 76-78's burst harness) and nothing that scrutinizes a design before it's committed to the pipeline it will then have to coexist with.

## Architecture

### 1. A design-intent manifest, declared before implementation
Before a new mechanism is wired into the pipeline, its author declares what it reads, what it writes, what existing stages it depends on or will be depended on by, and what thresholds it introduces — reusing Day 73's `Threshold` and `STAGE_DEPENDENCIES` shapes as the declaration format, filled in prospectively instead of reconstructed after the fact.

```python
@dataclass
class DesignIntent:
    stage_name: str
    reads: list[str]              # state keys it reads
    writes: list[str]             # state keys it mutates
    requires: list[str]           # genuine data dependencies on other stages
    new_thresholds: list[Threshold]
    expected_latency_ms: float
```

### 2. Automatic concurrency-safety check against the declared intent
Before the stage is registered, its declared `writes` are checked against `LOCK_REGISTRY` (Day 75) the same way Day 76's `register_stage` assertion did retroactively — but run here as a pre-commit gate rather than a runtime assertion discovered later, catching the exact class of bug that caused Day 76's lost-increment issue before any code runs against production traffic.

```python
def preflight_concurrency_check(intent: DesignIntent):
    unlocked = [w for w in intent.writes if w not in LOCK_REGISTRY]
    if unlocked:
        raise DesignRejected(f"{intent.stage_name} writes to unlocked state: {unlocked}")
```

### 3. Automatic latency-budget check against the declared intent
The new stage's `expected_latency_ms` and declared `requires` edges are inserted into Day 75's critical-path calculation *before* the stage ships, so `critical_path_latency` can be recomputed hypothetically and compared against the standing deadline Day 74 established from NOAA's alert-tier timing — turning Day 74's reactive discovery into a standing, proactive gate.

```python
def preflight_latency_check(intent: DesignIntent, deadline_ms: float):
    hypothetical_latency = critical_path_latency_with(STAGE_DEPENDENCIES | {intent.stage_name: intent}, use_bypass=False)
    if hypothetical_latency > deadline_ms:
        raise DesignRejected(f"{intent.stage_name} would push worst-case latency to {hypothetical_latency}ms, over the {deadline_ms}ms budget")
```

### 4. Mandatory adversarial scenario generation against the declared intent
Before the stage ships, Day 77's combinatorial burst generator is run specifically against scenarios that exercise the new stage's declared dependencies and writes, including deliberately constructing the kind of cross-path interaction (bypass vs. non-bypass, mixed severity) that caused Day 77's own deadlock — closing the loop Day 78 opened by making adversarial construction a pre-ship requirement rather than a post-hoc audit.

```python
def preflight_adversarial_check(intent: DesignIntent):
    targeted_scenarios = generate_adversarial_scenarios_for(intent, existing=STAGE_DEPENDENCIES)
    results = run_burst_suite_against(targeted_scenarios, candidate_stage=intent)
    if any(not r.passed for r in results):
        raise DesignRejected(f"{intent.stage_name} fails {sum(not r.passed for r in results)} adversarial scenarios")
```

## Failure Modes
- **A design-intent manifest can be wrong in exactly the way every prior hand-declared structure in this arc has been wrong.** `DesignIntent.reads` and `.writes` are self-reported by whoever is building the new stage, which is the same trust-on-declaration pattern that caused Day 54's original read-set problem and Day 75's missed regime-state lock — a manifest that under-declares its own writes sails through `preflight_concurrency_check` cleanly while still carrying the exact risk the check exists to catch.
- **Hypothetical latency estimation is only as good as `expected_latency_ms`.** Point 3 depends on the stage author's own estimate of their new stage's timing, which has no grounding until the stage actually runs against real traffic — a wildly optimistic self-estimate would pass the preflight check and then blow the real budget anyway, reproducing Day 74's original problem one layer later, just caught slightly earlier in the lifecycle.
- **Preflight checks test against today's pipeline, not the pipeline the new stage will actually ship into.** Multiple stages proposed concurrently (plausible in any real development process) could each pass their own isolated preflight check while their combination, once both are merged, reproduces exactly the kind of interaction bug Day 77 found — the gate checks one candidate against a static baseline, not candidate-against-candidate.
- **This adds real friction to shipping anything new.** Every one of today's four checks is a new hurdle between having an idea and having it running, which is a deliberate tradeoff — slower, more scrutinized shipping in exchange for catching problems nine days earlier — but it's a real cost against this arc's own daily cadence, and nothing today weighs whether that tradeoff is actually worth it for every class of change versus only the riskiest ones.
- **The gate itself is new, unproven infrastructure, the same category of risk flagged for the test harness on Day 78.** `preflight_concurrency_check`, `preflight_latency_check`, and `preflight_adversarial_check` have been run exactly zero times against a real new mechanism as of today — their own correctness is exactly as unearned as the burst harness was before Day 78's retroactive scrutiny, just one level removed.

## What's Next
Day 80: today's gate was itself built and shipped without going through itself — the same irony this arc keeps running into, now one level deeper. The honest next step is running the preflight gate against its own next use: the very next mechanism this arc builds should be the first real test of whether `DesignIntent` declarations, latency estimation, and adversarial scenario generation actually catch something before it ships, rather than becoming one more piece of unvalidated infrastructure trusted by default.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
