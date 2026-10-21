# Day 88: Measuring the Lie — Realized Error Rates Under Adaptive Clustering, and a Budget to Cap Them

## TL;DR
Day 87 let the canary's cluster structure change mid-test and recompute verdicts from a ledger. It flagged, in its own failure modes, that this breaks the guarantee that made the sequential test trustworthy in the first place. A sequential probability ratio test with `alpha=0.05` and `beta=0.10` promises a 5% chance of rolling back a healthy mechanism and a 10% chance of promoting a degraded one, but only when the test is specified in advance and run once. A procedure that regroups its own inputs and re-reads the verdict has many more chances to see something conclusive, so the real rates are higher than the labels on the config say. Today does what Day 78 did for the test harness: it stops asserting and measures. A simulation harness with known ground truth quantifies how far realized error rates drift under adaptive clustering. The result is a real drift, so the second half of the day adds an error budget that caps how much re-clustering any single stage may do. The third piece turns the whole concern into a new taxonomy dimension and a standing preflight check, so that the next mechanism that makes adaptive decisions has to prove its error rates instead of inheriting someone else's.

## The Problem
- Every statistical guarantee in the canary system rests on a procedure being fixed before the data arrives. Day 85 specified the SPRT boundaries up front and ran each test once. Day 86's clustering was computed once from baseline data. Both were analyses a skeptical reviewer could have signed off on in advance. Day 87's procedure is different in kind: its grouping depends on the data, and its verdict is recomputed whenever the grouping changes. A procedure whose structure is chosen after looking at the data cannot claim the error rates of one chosen before.
- The direction of the error matters. Each re-clustering is another draw: if the data looks borderline under grouping A, a different grouping B might make it conclusive. Allowing multiple groupings to be tried and the verdict re-read each time systematically inflates the chance of a spurious `REGRESSED`. For a canary, a spurious rollback is the cheaper error, because it wastes a deployment attempt and triggers an investigation. But it erodes trust in rollbacks, and the opposite error also exists: a re-clustering that merges two signals can dilute a real regression's evidence enough to delay a true `REGRESSED`.
- "It's probably fine because of the persistence gate" is a belief, not a measurement. The persistence gate limits how often re-clustering fires, but nobody has computed what it does to realized error rates, and the same arc has repeatedly produced cases where a plausible safeguard turned out to have a hole when finally tested (Day 76's regime state, Day 80's timer race, Day 81's logical race, Day 83's retry queue). The honest default is to assume the guarantee doesn't hold until a measurement says it does.
- There's also no place in the preflight gate where this class of concern could be caught. Day 82's taxonomy has dimensions for races, latency, adversarial input, resource growth, stale reads and correlation blindness. None of them describes "the statistical guarantee this mechanism claims doesn't match its realized behavior." A mechanism could ship with fabricated confidence numbers and clear all five checks.

## Architecture

### 1. A simulation harness with known ground truth
The first step is a harness that generates synthetic canary streams where the truth is set by construction: some runs are healthy by design, some are degraded by a known amount, and the correlation structure between signals is specified and, in the adversarial variants, changed mid-run on a schedule. Because ground truth is known, every verdict can be scored as right or wrong, and error rates can be measured directly instead of inferred.

```python
@dataclass
class SyntheticRun:
    truly_degraded: bool
    base_error_rate: float            # 0.01 healthy, 0.03 degraded (matches Day 85 configs)
    corr_schedule: list[tuple[float, float]]   # (time_fraction, correlation) change points
    n_events: int
    seed: int

def generate_stream(run: SyntheticRun) -> Iterator[Observation]:
    rng = np.random.default_rng(run.seed)
    rate = run.base_error_rate
    for i in range(run.n_events):
        frac = i / run.n_events
        rho = correlation_at(run.corr_schedule, frac)
        # one latent failure draw per request; correlated signals read the same
        # latent through a coupling parameter rho, so rho=1 means identical symptoms
        latent_fail = rng.random() < rate
        for signal in SIGNALS:
            coupled = rng.random() < rho
            fail = latent_fail if coupled else (rng.random() < rate)
            yield Observation(ts=float(i), signal=signal, passed=not fail, request_id=f"r{i}")
```

### 2. Measuring realized false-positive and false-negative rates
The harness runs thousands of synthetic canaries through each monitor variant (Day 85's independent SPRT, Day 86's fixed clustering, Day 87's adaptive clustering) and tallies verdicts against ground truth. The comparison is the point. Day 85 and 86's numbers should land at or near nominal, which also validates the harness itself, and any gap in Day 87's numbers is attributable to adaptivity.

```python
def measure_error_rates(monitor_factory, scenarios: list[SyntheticRun]) -> dict:
    fp = fn = healthy = degraded = 0
    for run in scenarios:
        monitor = monitor_factory()
        verdict = None
        for obs in generate_stream(run):
            monitor.ingest(obs)
            verdict = monitor.stage_verdict()
            if verdict in ("REGRESSED", "HEALTHY"):
                break
        if run.truly_degraded:
            degraded += 1
            fn += (verdict == "HEALTHY")
        else:
            healthy += 1
            fp += (verdict == "REGRESSED")
    return {
        "realized_alpha": fp / max(healthy, 1),
        "realized_beta": fn / max(degraded, 1),
        "n_healthy": healthy,
        "n_degraded": degraded,
    }
```

Running 4,000 healthy and 4,000 degraded scenarios per variant, with half of each set including a mid-run correlation change, gives numbers like these:

```
variant                   nominal_alpha  realized_alpha   nominal_beta  realized_beta
day85_independent             0.050          0.083            0.100         0.097
day86_fixed_clusters          0.050          0.046            0.100         0.104
day87_adaptive_clusters       0.050          0.112            0.100         0.131
```

Three things are worth reading carefully. Day 85's independent test shows a realized false-positive rate of 8.3% against a nominal 5%, which confirms the double-counting problem Day 86 was built to fix: correlated signals really did inflate false rollbacks. Day 86's fixed clustering lands near nominal on both axes, so the harness is behaving. Day 87's adaptive clustering roughly doubles the nominal false-positive rate (11.2%) and degrades the false-negative rate from 10.0% to 13.1%. The adaptivity that was supposed to make the test more accurate on a drifting-correlation stream made its error guarantees materially worse. The drift is large enough to matter and small enough that nobody would have noticed it by eyeballing production rollbacks.

### 3. An alpha-spending budget across re-clustering epochs
The fix borrows the standard remedy for repeated looks at accumulating data: spend a fixed total error budget across looks instead of giving each look the full nominal rate. A stage has a total `alpha`. The initial clustering uses a portion of it, and each re-clustering epoch is allowed to spend only from what's left. Once the budget is exhausted, further re-clustering is disabled for the remainder of the stage and the current grouping is frozen.

```python
class ErrorBudget:
    def __init__(self, total_alpha: float, max_epochs: int = 3, first_share: float = 0.5):
        self.total_alpha = total_alpha
        self.max_epochs = max_epochs
        remaining = total_alpha * (1 - first_share)
        # epoch 0 gets first_share of alpha; later epochs split the rest evenly
        self.per_epoch = [total_alpha * first_share] + \
                         [remaining / (max_epochs - 1)] * (max_epochs - 1)

    def alpha_for_epoch(self, epoch: int) -> float | None:
        if epoch >= self.max_epochs:
            return None   # budget exhausted: caller must freeze the clustering
        return self.per_epoch[epoch]

class BudgetedClusterTracker(OnlineClusterTracker):
    def __init__(self, *args, budget: ErrorBudget, **kwargs):
        super().__init__(*args, **kwargs)
        self.budget = budget
        self.frozen = False

    def propose(self, corr_matrix) -> bool:
        if self.frozen:
            return False
        if self.budget.alpha_for_epoch(self.epoch + 1) is None:
            self.frozen = True
            incident_log.record(event="cluster_budget_exhausted", epoch=self.epoch)
            return False
        return super().propose(corr_matrix)
```

The monitor then runs each epoch's SPRT with that epoch's share of alpha rather than the stage-wide nominal value, so the cumulative false-positive probability across all epochs stays bounded by the stage total (a union bound, which is conservative but valid).

```python
def verdicts_with_budget(self) -> dict[tuple, str]:
    alpha = self.tracker.budget.alpha_for_epoch(self.tracker.epoch) or self.tracker.budget.per_epoch[-1]
    out = {}
    for cluster in self.tracker.active:
        s, t = self.cluster_counts(cluster)
        base_cfg = min((self.configs[x] for x in cluster),
                       key=lambda c: c.unacceptable_rate - c.acceptable_rate)
        cfg = dataclasses.replace(base_cfg, alpha=alpha)   # epoch-specific alpha
        llr = sprt_log_likelihood_ratio(s, t, cfg.acceptable_rate, cfg.unacceptable_rate) if t else 0.0
        out[tuple(cluster)] = sprt_decision(llr, cfg.alpha, cfg.beta) if t else "CONTINUE"
    return out
```

Re-running the harness with the budgeted tracker:

```
variant                        nominal_alpha  realized_alpha   nominal_beta  realized_beta
day87_adaptive_clusters            0.050          0.112            0.100         0.131
day88_budgeted_adaptive            0.050          0.052            0.100         0.119
```

Realized false positives come back to nominal. The false-negative rate improves from 13.1% to 11.9% but doesn't fully recover to 10%. That residual gap is the cost of the smaller per-epoch alpha making later epochs slower to reach `REGRESSED`, and it's a real tradeoff rather than something to hide: the budget buys error control at the price of some detection speed in late epochs.

### 4. A new taxonomy dimension and a preflight check for it
The measurement exposed a class of failure the gate had no name for, so the first step is Day 82's discipline: propose the dimension with evidence, not intuition. The evidence here is the table above.

```python
propose_taxonomy_addition(
    dimension="STATISTICAL_VALIDITY_DRIFT",
    definition="A mechanism that makes adaptive statistical decisions reports nominal "
               "error guarantees that its realized behavior does not meet "
               "(Day 87's adaptive clustering: nominal alpha 0.05, realized 0.112).",
    evidence="Day 88 simulation: realized false-positive rate 2.2x nominal under adaptive clustering.",
)
```

The check itself requires any `DesignIntent` that declares an adaptive statistical procedure to supply a scenario generator with known ground truth, and the gate runs the Day 88 harness against it. A candidate passes only if its realized error rates are within a declared tolerance of its claimed ones.

```python
@dataclass
class StatisticalClaim:
    claimed_alpha: float
    claimed_beta: float
    tolerance: float = 0.25        # realized may exceed claimed by at most 25% relative
    scenario_generator: Callable[[int], list[SyntheticRun]] = None

def preflight_statistical_validity_check(intent: DesignIntent, candidate_factory) -> CheckResult:
    claim = intent.statistical_claim
    if claim is None:
        return CheckResult(passed=True, note="no statistical claim declared, exempt")
    if claim.scenario_generator is None:
        return CheckResult(passed=False, note="statistical claim declared without ground-truth scenarios")
    rates = measure_error_rates(candidate_factory, claim.scenario_generator(4000))
    violations = []
    if rates["realized_alpha"] > claim.claimed_alpha * (1 + claim.tolerance):
        violations.append(("alpha", claim.claimed_alpha, rates["realized_alpha"]))
    if rates["realized_beta"] > claim.claimed_beta * (1 + claim.tolerance):
        violations.append(("beta", claim.claimed_beta, rates["realized_beta"]))
    return CheckResult(passed=not violations, violations=violations, measured=rates)

register_check("preflight_statistical_validity_check",
               preflight_statistical_validity_check,
               covers={"STATISTICAL_VALIDITY_DRIFT"})
```

Applied retroactively to Day 87's unbudgeted design, the check fails it on alpha (0.112 against a ceiling of 0.0625). Applied to the budgeted version it passes alpha (0.052) and passes beta (0.119 against a ceiling of 0.125), though only narrowly on beta, which is worth noticing.

```
>>> coverage_report(FAILURE_MODE_TAXONOMY, CHECK_COVERAGE)
{
  "covered": [... 8 dimensions ...],
  "uncovered": ["STALE_READ"],
  "coverage_ratio": 0.889
}
```

Nine dimensions, eight covered. `STALE_READ`, the dimension Day 82 named first, is still the one with no check.

## Failure Modes
- **The harness measures realized error rates against synthetic scenarios, and a synthetic scenario only tests the structure its author generated.** The correlation schedules above are step changes and linear ramps. Real correlation drift could be intermittent, load-dependent or confined to a subset of request types, none of which the generator produces. A mechanism could pass the validity check on every scenario here and still violate its claimed rates under a drift shape nobody modeled. This is the same ceiling Day 82 identified for the taxonomy and Day 83 for the load simulator, now appearing at the statistics layer.
- **Splitting alpha across epochs with a union bound is valid but conservative, and the conservatism has a cost that scales with the epoch cap.** At `max_epochs=3` the later epochs get a quarter of the stage's alpha each, which is why realized beta rose from 10% to 11.9% rather than staying flat. Raising the cap to allow more adaptation shrinks each epoch's share further and slows late detection more. The cap is a threshold-registry value (Day 73) chosen by a measurement here, and it should be revisited when the signal set or traffic mix changes.
- **The tolerance parameter in the validity check is itself a judgment call that can quietly turn a real failure into a pass.** A 25% relative tolerance accepts an alpha of 0.0625 against a claimed 0.05. That's defensible for a simulation with sampling noise of its own, but a mechanism that sits right at the edge repeatedly passes without ever demonstrating margin. Beta passing at 0.119 against a 0.125 ceiling is exactly that situation.
- **The check validates a claim, but the claim is declared by the candidate's author.** A candidate can make a deliberately modest claim (claimed alpha 0.20) and pass trivially. The gate currently has no floor on how strong a claim must be for a mechanism of a given criticality. A statistical claim that's too weak to be useful but honest enough to be true clears the check cleanly.

## What's Next
The validity check can only be as good as its ground truth, and every scenario in today's harness was generated by the same person who wrote the mechanism it tests. The question Day 89 takes up is where independent ground truth could come from. Past production canaries, whose outcomes were eventually known (rolled back and confirmed bad, or promoted and held up), are the obvious source. The difficulty is volume: a handful of real rollouts is not 4,000 scenarios, and the sample is biased toward the failures that got noticed. Calibrating a synthetic harness against a small, skewed real corpus is its own statistical problem.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
