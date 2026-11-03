# Day 92: Auditing the Auditor — Nine Checks Declared, How Many Actually Work?

## TL;DR
Day 91 took the failure-mode coverage report to 9 of 9 and immediately warned that the number only measures declaration: every named dimension has a check pointed at it, which says nothing about whether the check works. Today stops looking at what the gate was designed to do and looks at what it has done. There are 31 gate evaluations on record since Day 79, and every one logged its verdict. Reading that history surfaced one check that has not fired once in 41 days, and a mutation test, which seeds known defects into fixture mechanisms and asks the gate to find them, showed why: `preflight_adversarial_check` has been passing vacuously since a refactor on Day 83. It generates its scenarios from a registry key that was renamed, gets an empty list back, runs zero scenarios, finds zero failures, and reports a clean pass. The fix is three structural rules: a check that did no work must report an error and never a pass, a standing battery of known-bad fixtures runs against the gate on a schedule, and every canary rollback gets attributed back to the specific check that should have caught it.

## The Problem
- A coverage ratio is built from `register_check(name, fn, covers={...})` calls. It counts declarations. A check that was correct on the day it was registered and silently broken since is indistinguishable, in that report, from one that works. Day 82 said the ratio was a statement about the taxonomy and not about safety. It is also a statement about registration and not about function.
- Verification instruments that fail silently have already appeared in this arc three times. Day 78 found the burst harness had never itself been checked. Day 83 found an unbounded retry queue inside `preflight_latency_check`. Day 89 measured the validity harness's fidelity at 0.64. Each time the instrument was found broken by looking at it directly, and each time that look was a one-off retroactive audit. Nothing standing exists that would find the next broken instrument.
- The gate produces a particularly hard failure to notice, which is the vacuous pass. A check that loops over scenarios and fails if any scenario fails returns `passed=True` when the loop body never executes. Zero scenarios and a hundred clean scenarios produce the same output. A dead check looks exactly like a healthy check on a candidate with no defects, and since most candidates have no defects, the dead state hides well.
- The gate's own history had the clue sitting in it. Of 31 evaluations, eight checks had fired at least once and one had fired zero times. A check that never fires is either guarding against something rare or not working, and nothing in the existing logs distinguishes the two.

## Architecture

### 1. Per-check telemetry with a work counter
Every check now returns, alongside its verdict, how much work it did: scenarios generated, scenarios executed, mutations injected, trials completed. The gate logs the counter with each evaluation, so a check's history is a time series of verdict and effort together.

```python
@dataclass
class CheckResult:
    passed: bool
    work_done: int                 # scenarios actually executed, not merely requested
    work_expected_min: int         # the floor below which the result is not meaningful
    violations: list = field(default_factory=list)
    note: str = ""

def log_evaluation(candidate: str, check: str, result: CheckResult, ts: float):
    gate_history.append({
        "ts": ts, "candidate": candidate, "check": check,
        "passed": result.passed, "work_done": result.work_done,
        "work_expected_min": result.work_expected_min,
    })
```

The history query that found the problem was a single aggregation. For `preflight_adversarial_check` it returned: 17 evaluations since Day 83, `work_done = 0` on all 17, zero fires.

```
check                          evals  fires  correct  work_done_median
preflight_concurrency_check      31      5       5         214
preflight_lock_order_check       31      2       2          36
preflight_determinism_check      31      2       2          40
preflight_latency_check          31      3       3          60
preflight_adversarial_check      31      0       -           0   <- last 17 evals: work_done = 0
preflight_resource_check         31      3       2         120
preflight_correlation_check      31      2       2          48
preflight_statistical_validity   31      2       -        4000
preflight_staleness_check         9      1       1         200
```

### 2. Fail closed when a check did no work
The gate treats a result with `work_done` below its declared floor as an error state, not a pass. An error blocks promotion exactly as a failure does, with a distinct reason code so that "the candidate is bad" and "the instrument is broken" are never confused.

```python
class Verdict(Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"
    CHECK_ERROR = "check_error"     # the instrument failed, not the candidate

def effective_verdict(check_name: str, result: CheckResult) -> Verdict:
    if result.work_done < result.work_expected_min:
        incident_log.record(event="vacuous_check", check=check_name,
                            work_done=result.work_done, floor=result.work_expected_min)
        return Verdict.CHECK_ERROR
    return Verdict.PASS if result.passed else Verdict.BLOCK
```

Applied retroactively to the 17 silent evaluations, the rule would have returned `CHECK_ERROR` on every one, and the first would have been on Day 83.

### 3. The root cause, and a structural fix for the whole class
The adversarial check built its scenario list from `LOCK_REGISTRY["burst_dimensions"]`. The Day 83 refactor, which introduced declared resource bounds, renamed that key to `scenario_dimensions` and updated the code that wrote it but not the check that read it. The read used `.get("burst_dimensions", [])`, and the default turned a missing key into an empty list.

```python
# Before: a missing key silently becomes "no scenarios"
dims = LOCK_REGISTRY.get("burst_dimensions", [])

# After: a missing key is a loud failure at the point of the mistake
dims = LOCK_REGISTRY["scenario_dimensions"]          # KeyError if renamed again
if not dims:
    raise CheckMisconfigured("scenario_dimensions is empty; refusing to evaluate vacuously")
```

The wider lesson is that a defaulting `.get(..., [])` in a verification path is a vacuous-pass generator. A lint rule in the gate's own repository now rejects `.get(..., [])`, `.get(..., {})` and `.get(..., None)` inside any file under `preflight/`.

### 4. A mutation battery of known-bad fixtures
Fail-closed handles the no-work case. It does not handle a check that runs and still misses things. The remedy is to feed the gate candidates whose defects are known by construction. One fixture mechanism per taxonomy dimension is built with a seeded defect, kept in a fixtures directory, and the gate is expected to reject each one.

```python
BATTERY = [
    Fixture("fx_unlocked_write",       defect="DATA_RACE",                   expect_check="preflight_concurrency_check"),
    Fixture("fx_inverted_lock_order",  defect="DEADLOCK",                    expect_check="preflight_lock_order_check"),
    Fixture("fx_order_dependent_sum",  defect="LOGICAL_RACE",                expect_check="preflight_determinism_check"),
    Fixture("fx_slow_path_over_budget",defect="LATENCY_BUDGET_VIOLATION",    expect_check="preflight_latency_check"),
    Fixture("fx_boundary_overflow",    defect="ADVERSARIAL_INPUT_SENSITIVITY", expect_check="preflight_adversarial_check"),
    Fixture("fx_unbounded_queue",      defect="RESOURCE_EXHAUSTION",         expect_check="preflight_resource_check"),
    Fixture("fx_held_read_then_act",   defect="STALE_READ",                  expect_check="preflight_staleness_check"),
    Fixture("fx_mirrored_sources",     defect="UPSTREAM_CORRELATION_BLINDNESS", expect_check="preflight_correlation_check"),
    Fixture("fx_inflated_alpha",       defect="STATISTICAL_VALIDITY_DRIFT",  expect_check="preflight_statistical_validity"),
]

def run_battery(gate) -> dict:
    caught, missed = [], []
    for fx in BATTERY:
        results = gate.evaluate(fx.intent, fx.candidate)
        fired = {name for name, r in results.items() if effective_verdict(name, r) != Verdict.PASS}
        (caught if fx.expect_check in fired else missed).append(fx.name)
    return {"caught": caught, "missed": missed, "catch_rate": len(caught) / len(BATTERY)}
```

The first run produced `catch_rate = 0.889`. The single miss was `fx_boundary_overflow`, which confirmed the history finding from the other direction, and the battery would have shown the break on the day it happened.

### 5. A schedule, and a trigger on any change to the gate
The battery runs nightly and again on any commit that touches `preflight/`, the taxonomy, the threshold registry or `LOCK_REGISTRY`. A catch rate below 1.0 raises a blocking incident on the gate itself: until the battery is green, new candidates are evaluated with a visible banner that the gate is degraded.

```python
def on_gate_change_or_nightly(gate):
    report = run_battery(gate)
    if report["catch_rate"] < 1.0:
        gate.set_status("DEGRADED", reason=f"battery missed: {report['missed']}")
        incident_log.record(event="gate_degraded", missed=report["missed"])
    else:
        gate.set_status("HEALTHY")
```

### 6. Attributing escapes to specific checks
Day 91 defined an escape report at the dimension level. It is now extended to name the check. Every canary rollback records which dimension its symptoms map to, and the report joins that to the check that claimed the dimension and to that check's recent battery and work history.

```python
def attribute_escape(rollback, coverage_map, gate_history):
    out = []
    for dim in rollback.dims:
        check = next(c for c, dims in coverage_map.items() if dim in dims)
        recent = [h for h in gate_history if h["check"] == check and h["ts"] < rollback.decided_at][-5:]
        out.append({"dim": dim, "check": check,
                    "recent_work": [h["work_done"] for h in recent],
                    "vacuous_before_escape": any(h["work_done"] == 0 for h in recent)})
    return out
```

Of the 14 canaries since Day 84, five rolled back. Three mapped to dimensions the gate claimed to cover, one mapped to no dimension, and one was a false alarm. Of the three escapes, one traced to `ADVERSARIAL_INPUT_SENSITIVITY`, and its attribution record showed `vacuous_before_escape: true`. A real production rollback happened because the dead check had waved the candidate through.

## Failure Modes
- **The battery's fixtures were written by the author of the checks.** That is the Day 89 problem again at one remove. Each fixture seeds the defect the author thought of, in the shape the author thought of, so a check can catch every fixture and still miss a differently shaped instance. A catch rate of 1.0 means the checks still detect what they were built to detect, which is a regression test and not a completeness proof.
- **`work_expected_min` is declared by the check's own author.** A floor set to 1 makes the guard trivial and a floor set too high produces constant `CHECK_ERROR` noise. The floors are threshold-registry entries and were set from the observed median work of each check in healthy periods, with the weakness that a check which was already degraded when the floors were calibrated gets a floor calibrated to its degraded behavior.
- **Fail-closed shifts cost onto the author of the candidate.** A broken instrument now blocks innocent mechanisms until the instrument is repaired, which is correct but creates pressure to bypass the gate. The `CHECK_ERROR` verdict needs a defined, logged override path with an owner and an expiry, or people will invent an undefined one.
- **Vacuous passes have more disguises than an empty list.** A scenario set that is non-empty but trivially easy, a mutation that never lands on a reachable code path, or a timeout swallowed and counted as success all produce "work done" without meaningful work. The work counter catches only the crudest form.

## What's Next
The audit looked at the gate, which is one component. The gate guards a pipeline that now contains well over sixty mechanisms accumulated over ninety days, and nobody has looked at those as a system. Which depend on which, which are single points of dependence, and how many of them shipped before the gate existed and have therefore never been through it at all? Day 93 draws the map that was never drawn.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
