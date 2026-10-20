# Day 85: How Long Is Long Enough — Sequential Confidence for Canary Stages

## TL;DR
Day 84's canary plan used fixed traffic percentages and fixed observation windows — 1% for 30 minutes, 10% for 60, 50% for 120 — chosen because they felt reasonable, not because any of them represented a statistically meaningful amount of evidence. That's a real problem in both directions: a stage can "pass" purely because nothing bad happened to occur during an arbitrarily short window, and a stage can sit observing far longer than necessary once a mechanism is clearly healthy, burning deployment velocity for no added confidence. Today replaces fixed windows with sequential testing — advancing a stage the moment there's enough statistical evidence it's healthy, rolling back the moment there's enough evidence it isn't, and otherwise continuing to collect data — reusing Day 65's per-field statistical drift detection and Day 70's corroboration-quorum thinking, but turned toward a genuinely different question: not "has this value drifted" but "is this decision justified yet."

## The Problem
- Fixed observation windows conflate two completely different things: "enough time has passed" and "enough evidence has accumulated." A mechanism serving a trickle of low-volume satellite-pass traffic during its 1% canary window might see ten total requests in thirty minutes — nowhere near enough to distinguish a healthy error rate from an unhealthy one at any reasonable confidence level — while a mechanism serving the NOAA alert-tier hot path might see ten thousand requests in the same window, more than enough evidence five minutes in.
- This means Day 84's fixed windows are simultaneously too short for low-traffic mechanisms (false confidence — "passed" the stage without real evidence) and too long for high-traffic ones (wasted time — continuing to wait after the answer was already statistically clear), and there's no way to know which situation you're in without actually measuring the evidence as it accumulates, not just the clock.
- The arc has already built a statistical machinery for exactly this kind of question. Day 65's drift detection asks "has this field's distribution shifted enough, with enough persistence, to be a real signal rather than noise" — using a persistence gate specifically to avoid reacting to single-sample blips. A canary health check is asking a structurally identical question: "has this mechanism's behavior shifted enough from baseline, with enough accumulated evidence, to be a real regression rather than noise." The two problems were solved separately because they arrived at different points in the arc, but they're the same problem wearing different clothes.
- Without a principled stopping rule, the canary system is vulnerable to both failure directions at once, silently, with no way to tell from outside which one is happening on any given rollout — which is worse than being wrong in one consistent direction, because it means nobody can build calibrated trust in what "passed canary" actually means.

## Architecture

### 1. Framing canary evaluation as sequential hypothesis testing
Instead of waiting a fixed duration and checking once, each health signal is evaluated continuously as data arrives, using a sequential probability ratio test (SPRT) — a classical statistical method built for exactly this situation: deciding as early as honestly possible between "this is healthy" (null hypothesis) and "this is degraded" (alternative), given a stream of observations, with a controlled false-positive and false-negative rate rather than an arbitrary fixed sample size.

```python
@dataclass
class SequentialTestConfig:
    acceptable_rate: float      # baseline-acceptable value for this signal, e.g. error_rate <= 0.01
    unacceptable_rate: float    # regression threshold, e.g. error_rate >= 0.03
    alpha: float = 0.05          # false-positive rate: rolling back a healthy mechanism
    beta: float = 0.10           # false-negative rate: advancing a degraded mechanism

def sprt_log_likelihood_ratio(successes: int, trials: int, p0: float, p1: float) -> float:
    # standard SPRT for a Bernoulli-distributed signal (pass/fail per request)
    failures = trials - successes
    return (failures * math.log(p1 / p0) + successes * math.log((1 - p1) / (1 - p0)))

def sprt_decision(llr: float, alpha: float, beta: float) -> str:
    upper = math.log((1 - beta) / alpha)   # evidence threshold to reject null (mechanism is bad)
    lower = math.log(beta / (1 - alpha))   # evidence threshold to accept null (mechanism is fine)
    if llr >= upper:
        return "REGRESSED"
    if llr <= lower:
        return "HEALTHY"
    return "CONTINUE"
```

### 2. Per-signal sequential monitors replacing fixed-window checks
Each health signal from Day 84's `CanaryStage` now runs its own sequential monitor, updated as requests stream in rather than evaluated once after a fixed wait. A stage advances the moment *every* monitored signal independently reaches `HEALTHY`, and rolls back the moment *any* signal reaches `REGRESSED` — not waiting for the others, because one confirmed regression is enough evidence on its own, the same "don't wait for consensus on a clear signal" logic Day 70's quorum used for corroboration, applied here in the opposite direction.

```python
class SequentialCanaryMonitor:
    def __init__(self, stage: CanaryStage, configs: dict[str, SequentialTestConfig]):
        self.stage = stage
        self.configs = configs
        self.state = {signal: {"successes": 0, "trials": 0} for signal in stage.health_signals}

    def observe(self, signal: str, passed: bool):
        self.state[signal]["trials"] += 1
        if passed:
            self.state[signal]["successes"] += 1

    def check(self) -> dict[str, str]:
        decisions = {}
        for signal, cfg in self.configs.items():
            s, t = self.state[signal]["successes"], self.state[signal]["trials"]
            if t == 0:
                decisions[signal] = "CONTINUE"
                continue
            llr = sprt_log_likelihood_ratio(s, t, cfg.acceptable_rate, cfg.unacceptable_rate)
            decisions[signal] = sprt_decision(llr, cfg.alpha, cfg.beta)
        return decisions

    def stage_verdict(self) -> str:
        decisions = self.check()
        if any(d == "REGRESSED" for d in decisions.values()):
            return "REGRESSED"
        if all(d == "HEALTHY" for d in decisions.values()):
            return "HEALTHY"
        return "CONTINUE"
```

### 3. A minimum floor, because sequential testing can be fooled by low volume the same way fixed windows were
A pure SPRT can still "declare healthy" after very few observations if early results are unusually clean — which solves the wasted-time problem but reopens the false-confidence problem from a different angle: five lucky requests with zero errors can cross the `HEALTHY` boundary on a signal whose real behavior nobody has actually tested yet. A minimum trial floor per signal, informed by Day 73's threshold registry, prevents a verdict of `HEALTHY` from firing before there's a baseline amount of real exposure, regardless of how clean the early evidence looks.

```python
MIN_TRIALS_BEFORE_HEALTHY_VERDICT = {
    "error_rate": 200,
    "p99_latency": 200,
    "resource_bound_drift": 50,          # sampled periodically, not per-request
    "determinism_sample_mismatch": 50,
}

def stage_verdict_with_floor(monitor: SequentialCanaryMonitor) -> str:
    decisions = monitor.check()
    for signal, decision in decisions.items():
        trials = monitor.state[signal]["trials"]
        floor = MIN_TRIALS_BEFORE_HEALTHY_VERDICT.get(signal, 0)
        if decision == "HEALTHY" and trials < floor:
            decisions[signal] = "CONTINUE"   # not enough exposure yet, regardless of LLR
    if any(d == "REGRESSED" for d in decisions.values()):
        return "REGRESSED"
    if all(d == "HEALTHY" for d in decisions.values()):
        return "HEALTHY"
    return "CONTINUE"
```

Note the asymmetry: the floor only suppresses an early `HEALTHY` verdict, never an early `REGRESSED` one. A mechanism causing clear, severe harm in just a handful of requests should be rolled back immediately — the floor exists to prevent false confidence, not to slow down a real rollback, which mirrors exactly the asymmetric risk tolerance Day 46's suspect-state circuit breaker already established for isolation decisions.

### 4. Replacing Day 84's fixed `min_observation_minutes` end-to-end
The staged rollout loop from Day 84 changes from a fixed wait-then-check pattern to a streaming evaluate-as-you-go pattern, with a maximum duration as a safety backstop rather than a target — if a stage genuinely can't reach a verdict within a generous ceiling (low-traffic mechanism, ambiguous evidence), that itself is useful information, logged distinctly from both `HEALTHY` and `REGRESSED`.

```python
def run_canary_rollout_sequential(mechanism, plan: list[CanaryStage], router: TrafficRouter,
                                     max_stage_duration_minutes=240):
    for stage in plan:
        router.set_traffic_percent(mechanism, stage.traffic_percent)
        configs = {s: SIGNAL_CONFIGS[s] for s in stage.health_signals}
        monitor = SequentialCanaryMonitor(stage, configs)
        started = now()

        while True:
            for event in live_metrics_registry.stream(mechanism, since=monitor_last_checkpoint(monitor)):
                monitor.observe(event.signal, event.passed)
            verdict = stage_verdict_with_floor(monitor)

            if verdict == "REGRESSED":
                router.set_traffic_percent(mechanism, 0.0)
                raise CanaryRollbackTriggered(mechanism.name, stage, monitor.state)
            if verdict == "HEALTHY":
                break
            if elapsed_minutes(started) > max_stage_duration_minutes:
                incident_log.record(mechanism=mechanism.name, stage=stage.traffic_percent,
                                      action="inconclusive_after_max_duration")
                raise CanaryInconclusive(mechanism.name, stage, monitor.state)
            sleep(POLL_INTERVAL_SECONDS)

    router.set_traffic_percent(mechanism, 100.0)
```

## Failure Modes
- **`acceptable_rate` and `unacceptable_rate` are themselves judgment calls, and the SPRT's entire guarantee is conditional on them being roughly right.** Set them too close together and the test takes enormously longer to reach any verdict, defeating the speed benefit; set `unacceptable_rate` too permissively and a real regression can sit in `CONTINUE` far longer than the fixed-window approach would have tolerated it, because the statistical test is specifically built to resist declaring a verdict on borderline evidence — borderline-bad is still bad.
- **Independent per-signal SPRTs ignore correlation between signals**, the same blind spot Day 71 named for independent sources — if `error_rate` and `p99_latency` are degrading for the same underlying reason, treating them as independent evidence streams can produce a combined false-confidence effect where neither individually crosses its threshold as fast as the correlated reality warrants. This is the same `UPSTREAM_CORRELATION_BLINDNESS` dimension from Day 82's taxonomy — still uncovered there, and still a live gap here in a new form.
- **A `CanaryInconclusive` result (low-traffic mechanism, ambiguous evidence, hit the max-duration backstop) has no defined resolution path today** — it's logged, but nothing in the design says whether an inconclusive mechanism should be manually promoted, held indefinitely, or escalated for a longer observation window. That's a real operational gap, not just a cosmetic one, because low-traffic mechanisms are exactly the ones most likely to hit it.
- **The sequential monitor trusts the health-signal events it's streamed, and nothing here re-validates that the events themselves are accurate** — a monitoring pipeline bug that misreports `passed`/`failed` status feeds directly into a decision with real production consequences, with no corroboration step comparable to Day 70's quorum across independent sources protecting it.

## What's Next
`UPSTREAM_CORRELATION_BLINDNESS` has now surfaced as a live gap twice — once in Day 82's original taxonomy, unaddressed since, and again today in the correlated-signal blind spot of independent per-signal SPRTs. It's the oldest unresolved item on the board, and it keeps resurfacing in a slightly different costume every time a new piece of the system gets built. Day 86 takes it on directly: can canary health signals, and the independent upstream sources from Day 70, be evaluated jointly instead of independently — and what does a corroboration-aware version of sequential testing actually look like when the things being tested aren't independent to begin with.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
