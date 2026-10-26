# Day 91: The Oldest Gap on the Board — A Stale-Read Check, and What It Found in Day 80's Fix

## TL;DR
Day 82's first coverage report listed three uncovered dimensions: `RESOURCE_EXHAUSTION`, `UPSTREAM_CORRELATION_BLINDNESS` and `STALE_READ`. Day 83 closed the first, Day 86 and 87 addressed the second, and Day 88 added and closed a new one. `STALE_READ` has sat on the board the whole time, partly because it was the least obviously urgent and partly because the arc had already fixed it once in a specific mechanism (Day 64's epoch-based invalidation, Day 81's versioned-read-and-retry) and treating that as if it were a general solution. Today builds the check. It combines a static pass that finds read-then-use patterns with a gap between them, and a dynamic pass that forces that gap to be long and observes whether the mechanism's behavior changes. Then, following the arc's most reliable habit, it runs the new check backward over mechanisms already in production. The retroactive run finds a real stale read in Day 80's adaptive severity threshold, a mechanism that was fixed for a data race, passed the gate, and has been running since. The lock Day 80 added protects the write. It doesn't protect the decision made with the value after it's read.

## The Problem
- A data race and a stale read are different failures, and the arc's defenses so far treat the first much more thoroughly. A data race is two concurrent accesses to shared state where at least one is a write, with no ordering between them. A stale read is a perfectly ordered, perfectly synchronized read that returns a value which was correct when read and is wrong by the time it's acted on. Locks fix the first. They do nothing for the second unless the lock is held across the whole read-decide-act sequence, which in a system that makes network calls between steps is rarely feasible or desirable.
- Day 64 and Day 81 each fixed one instance of the problem and recorded the pattern, but the pattern was never turned into something the gate could look for. Day 64 added epoch invalidation for trust state, so a cached trust score derived from a changed producer is discarded. Day 81 added versioned reads with commit-if-unchanged for `combined_confidence_score`. Both are the right fix. Both depend on the author remembering the pattern, which is the dependency the preflight gate exists to remove.
- The existing checks can't see this class even in principle. `preflight_concurrency_check` verifies declared reads and writes against the lock registry: a read under a lock is "safe" regardless of what happens to the value afterward. `preflight_determinism_check` (Day 81) forces interleavings and compares outputs, but its interleavings are scheduled between operations, not inserted into the gap inside a single mechanism's own read-to-use span, so a mechanism that's deterministic under scheduling can still act on stale data whenever its internal delay is long enough.
- The pipeline is full of the exact shape that produces this. NOAA SWPC alert evaluation reads thresholds, calls out to an upstream for the current reading, and then compares. NeoWs ingestion reads a classification table, fetches a batch, and classifies. Every mechanism that reads internal state before awaiting an external call has a window in which the state can change. The windows are short on average, but Day 74's latency analysis established that upstream calls are the slowest and most variable part of the pipeline, which means the window is longest exactly when the system is under stress.

## Architecture

### 1. Declaring freshness requirements in `DesignIntent`
Each declared read gets a freshness policy, extending the manifest from Day 79 and the resource bounds from Day 83. The policy says what the mechanism needs from the value between the moment of reading and the moment of acting on it.

```python
class Freshness(Enum):
    NONE_NEEDED = "none_needed"            # value is advisory; staleness is harmless
    MAX_AGE = "max_age"                    # may be used if no older than N ms at point of use
    VERSION_PINNED = "version_pinned"      # must be unchanged at commit (Day 81's pattern)

@dataclass
class ReadDeclaration:
    state_key: str
    freshness: Freshness
    max_age_ms: int | None = None          # required when freshness == MAX_AGE

@dataclass
class DesignIntent:
    name: str
    reads: list[ReadDeclaration]
    writes: set[str]
    requires: set[str]
    expected_latency_ms: tuple[int, int]
    resource_bounds: list[ResourceBound] = field(default_factory=list)
    statistical_claim: StatisticalClaim | None = None
    criticality_tier: str = "core"
```

Forcing the declaration is half the value. An author who has to write `NONE_NEEDED` next to a threshold that feeds an alert decision has to say it out loud, which is where the Day 80 mistake (the "it's just a periodic timer" assumption) would have been caught.

### 2. A static pass over the mechanism's access surface
The static half reuses the AST access-surface machinery from Day 58. It looks for each declared read, then for any suspension point (an `await`, a network call, a sleep, a queue get) between that read and the first use of the value, and flags the pair if the freshness policy is stricter than `NONE_NEEDED`.

```python
SUSPENSION_NODES = (ast.Await, ast.Yield, ast.YieldFrom)
SUSPENSION_CALLS = {"sleep", "get", "fetch", "request", "recv", "acquire"}

def find_read_use_gaps(mech_ast: ast.AST, read_keys: set[str]) -> list[dict]:
    gaps = []
    for fn in ast.walk(mech_ast):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        reads = {}          # variable name -> (state_key, lineno)
        suspended_since = {}   # variable name -> line of first suspension after read
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and (key := read_of_state_key(node, read_keys)):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        reads[t.id] = (key, node.lineno)
            if isinstance(node, SUSPENSION_NODES) or is_suspending_call(node, SUSPENSION_CALLS):
                for var in reads:
                    suspended_since.setdefault(var, node.lineno)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in reads:
                if node.id in suspended_since and node.lineno > suspended_since[node.id]:
                    gaps.append({
                        "state_key": reads[node.id][0],
                        "read_line": reads[node.id][1],
                        "suspend_line": suspended_since[node.id],
                        "use_line": node.lineno,
                    })
    return gaps
```

Static analysis over-reports, since a suspension between read and use only matters if something can change the value in the meantime, and it under-reports where the value flows through a helper function or a data structure. That's why the dynamic half exists, and why the static half is a candidate generator rather than a verdict.

### 3. A dynamic pass that forces the gap open
The dynamic half follows Day 81's forced-interleaving approach but aims it differently. Instead of scheduling operations around each other, it inserts a controllable delay at every suspension point inside the mechanism and, during that delay, has an adversary mutate the declared state keys. If the mechanism's output differs from the output on an undisturbed run, or violates its declared invariants, it consumed a stale value.

```python
def preflight_staleness_check(intent: DesignIntent, candidate_fn, scenarios) -> CheckResult:
    violations = []
    pinned = [r for r in intent.reads if r.freshness != Freshness.NONE_NEEDED]
    if not pinned:
        return CheckResult(passed=True, note="all reads declared NONE_NEEDED or none declared")

    for scenario in scenarios:
        baseline = run_isolated(candidate_fn, scenario)
        for read in pinned:
            for delay_ms in STALENESS_DELAYS:                        # e.g. [5, 50, 500, 5000]
                with StalenessInjector(target=read.state_key, delay_ms=delay_ms) as inj:
                    inj.mutate_during_delay(mutator_for(read.state_key, scenario))
                    disturbed = run_with_injection(candidate_fn, scenario, inj)
                if canonicalize(disturbed) != canonicalize(baseline):
                    if read.freshness == Freshness.VERSION_PINNED and disturbed.aborted_cleanly:
                        continue          # acted on a stale value? no: detected the change and retried
                    if read.freshness == Freshness.MAX_AGE and delay_ms <= (read.max_age_ms or 0):
                        continue          # within tolerance by declaration
                    violations.append({
                        "state_key": read.state_key, "delay_ms": delay_ms,
                        "scenario": scenario.name, "baseline": baseline, "disturbed": disturbed,
                    })
    return CheckResult(passed=not violations, violations=violations)

register_check("preflight_staleness_check", preflight_staleness_check, covers={"STALE_READ"})
```

The two exemptions in the middle encode what "correct" means under each declared policy. A `VERSION_PINNED` read passes if the mechanism notices the mutation and retries or aborts (the Day 81 pattern), and a `MAX_AGE` read passes when the injected delay is within the age the author said they could tolerate. Anything else that changes the output is a real stale-read consequence.

### 4. Retroactive run: Day 80's adaptive severity threshold
Once registered, the check ran over the mechanisms currently in the pipeline, the way Day 78 re-audited the harness and Day 83 turned the new check on the gate's own retry wrapper. Four mechanisms declared non-trivial freshness. Three passed. The adaptive severity threshold (`adaptive_severity_threshold`, built in Day 80) failed at every delay of 50 ms and above.

```
mechanism: adaptive_severity_threshold
  read:    adaptive_threshold_by_shape   (declared MAX_AGE 100 ms by author in retroactive manifest)
  static:  read at L41, await noaa.fetch_reading() at L44, use at L47    -> gap flagged
  dynamic: delay=5ms     output unchanged
           delay=50ms    output unchanged
           delay=500ms   bypass decision differs in 3 of 40 scenarios
           delay=5000ms  bypass decision differs in 11 of 40 scenarios
  verdict: FAIL  (stale threshold applied to a reading evaluated under a newer threshold)
```

What happened in production terms: the mechanism reads the current per-shape threshold, awaits the latest NOAA reading, and then compares the reading against the threshold it read before the await. Day 80's fix put a lock around `adaptive_threshold_by_shape` writes and corrected the `requires` declaration, which is why `preflight_concurrency_check` passes it. The lock doesn't span the await, and it shouldn't, since holding a lock across a network call would be its own failure. So when the threshold retuner fires during a slow NOAA response, the bypass decision for that reading is made against a threshold that's already been superseded. In a burst that's the worst moment for it: the retuner is most active when severity is changing, and the upstream is slowest when it is loaded. Slow responses and fast retuning coincide.

The fix is Day 81's pattern applied one level up: read the threshold with its version, and commit the bypass decision only if the version is unchanged. If it changed, re-read and re-evaluate against the new threshold with the already-fetched reading, which costs no extra upstream call.

```python
async def evaluate_bypass(shape, reading_source):
    for _ in range(MAX_RETRIES):                       # bounded: Day 83's rule
        threshold, version = thresholds.read_versioned(shape)
        reading = await reading_source.fetch()         # the suspension point
        if thresholds.version_of(shape) == version:
            return reading.severity >= threshold
        # threshold changed during the await: reuse the reading, re-read the threshold
        reading_source.prefetched = reading
    raise PreflightRetryExhausted(shape)
```

Re-running the check on the fixed mechanism with `VERSION_PINNED` declared passes at every delay, and the retry loop is bounded, so it doesn't reintroduce Day 83's unbounded-retry problem.

### 5. Updating the coverage report, with a caveat
```
>>> coverage_report(FAILURE_MODE_TAXONOMY, CHECK_COVERAGE)
{
  "covered": [... all 9 dimensions ...],
  "uncovered": [],
  "coverage_ratio": 1.0
}
```

A 1.0 is the number to be most careful about reading. It means every dimension currently in the taxonomy has at least one check pointed at it, nothing more. Two caveats follow directly from the arc. First, `UPSTREAM_CORRELATION_BLINDNESS` is covered by the runtime clustering from Days 86 and 87, not by a preflight check, so the coverage map is currently mixing two kinds of defense: things the gate verifies before shipping and things the shipped system does at runtime. They're not interchangeable, and the report doesn't distinguish them. Second, Day 82's own ceiling still applies: the ratio is measured against a taxonomy that is a record of failures the arc has already encountered or imagined.

## Failure Modes
- **The static pass keys off syntactic suspension points and misses value flow through indirection.** A read stored in an object attribute, passed through a queue, or captured by a closure and used later never produces an `ast.Name` load after a suspension in the same function. The dynamic pass catches some of these because it injects delays at the runtime level, but only for state keys the author declared. An undeclared read has no injection, which makes the declaration step the weakest link.
- **The staleness injector mutates declared state with a scenario-specific mutator, and its realism is only as good as the scenarios.** A mutator that changes a threshold by 1% will rarely flip a decision. One that changes it by 50% will flip many. The mutation size used in the retroactive run (drawn from the retuner's real step-size distribution, reconstructed from its logs) was the best available proxy, and a mechanism could pass here under mild mutations and fail under a rare large one.
- **`MAX_AGE` declarations are author judgment, and the check enforces consistency with them rather than correctness of them.** The retroactive manifest declared 100 ms for the adaptive threshold because that's what the author believed was tolerable. The check showed the mechanism fails at 500 ms, which is a violation of the declaration only if delays that long actually occur, and Day 74's latency data says they do under load. A declaration that's too generous passes the check while leaving real exposure.
- **The retry pattern the fix uses trades staleness for latency.** Re-reading and re-evaluating on a version change adds work on the hot path exactly when the system is busiest, and Day 74's worst-case latency analysis now needs the retry bound included. The numbers shifted by under a millisecond in the retroactive recomputation, but the registry value `MAX_RETRIES=3` is a choice that interacts with the alert-tier deadline.

## What's Next
With every dimension in the taxonomy covered, the natural question changes from "what is the gate missing" to "what is the gate doing." Nine checks now exist, and nobody has looked at which of them have ever actually fired, how often a fired check was right, or whether any of them have gone a month without catching anything and might be dead weight or silently broken. Day 92 audits the gate itself using its own history, and, because the series is approaching its end, starts treating the ninety-odd days of mechanisms as a system to be reviewed as a whole.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
