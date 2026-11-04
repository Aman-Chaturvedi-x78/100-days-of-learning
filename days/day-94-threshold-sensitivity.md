# Day 94: 118 Knobs — Which Ones Matter, Which Pairs Fight, and Invariants That Stop Them

## TL;DR
The threshold registry Day 73 created to hold every tunable constant now has 118 entries. Each was set on the day its mechanism was built, by reasoning about that mechanism alone: a hysteresis margin here, a persistence count there, a correlation cutoff, a timeout. Day 93's map showed that many of them feed the same downstream decision, which means their effects compound in ways no single day's reasoning considered. Today runs a sensitivity analysis over the whole registry, replaying 90 days of recorded traffic under perturbed thresholds and measuring how three outcome metrics respond: alert-tier end-to-end latency, false-alarm rate, and missed-event rate. The result is lopsided. Nine of 118 thresholds account for 81% of the variance in outcomes, and 61 of them contribute under 0.5% each. The analysis also found a real interaction: two thresholds set on different days, Day 68's hysteresis margin and Day 86's cluster join cutoff, produce regime flapping when they sit within 0.1 of each other, and each looked safe alone. The fix is declared invariants in the registry, enforced at load time, so a retune that breaks a relationship between constants is rejected before it ships.

## The Problem
- A threshold is only meaningful relative to the thresholds around it. Day 68's `leave_threshold` (0.50) and `join_threshold` (0.70) for regime hysteresis define a dead band whose width is what prevents flapping. Day 86 reused the same join/leave idea for correlation clustering with its own pair. Nothing records that the two pairs are structurally the same mechanism feeding the same stability property, so retuning one has no effect on the other and no warning.
- The registry stores values and units. It does not store relationships. Whatever a constant is supposed to satisfy relative to another (a timeout must exceed a persistence window; a leave threshold must sit below a join threshold by a margin) lives in the head of whoever wrote the mechanism, and in prose in a design post.
- With 118 constants there is also a prioritization problem. A retune proposal for a low-influence constant gets the same review as one for a high-influence constant, because nobody knows which is which. The review effort is spread evenly across things whose impact is wildly uneven.
- The registry changes more often than anything else in the system. Every day since Day 56 has added entries, and tuning changes go through a lighter path than code changes. That makes it the place where a regression is easiest to introduce and hardest to attribute.

## Architecture

### 1. Outcome metrics defined once, replayed on recorded history
The analysis needs a function from a threshold configuration to a vector of outcome metrics. The pipeline already supports replay (Day 52, 89), so the function is a replay of 90 days of recorded feeds under a given configuration, scored against the incident ground truth the arc has collected.

```python
@dataclass
class Outcomes:
    alert_latency_p99_s: float
    false_alarm_rate: float
    missed_event_rate: float

def evaluate_config(config: dict[str, float], replay_window: ReplayWindow) -> Outcomes:
    pipeline = build_pipeline(threshold_overrides=config)
    result = replay_window.run(pipeline)
    return Outcomes(
        alert_latency_p99_s=result.alert_latency.percentile(99),
        false_alarm_rate=result.false_alarms / max(result.alerts, 1),
        missed_event_rate=result.missed / max(result.true_events, 1),
    )
```

One replay takes about 40 seconds, so the evaluation budget, not the statistics, constrains the design.

### 2. Morris screening to cut 118 down to a shortlist
A full variance decomposition over 118 parameters needs far more evaluations than the budget allows. The Morris method is a cheap screening step: it perturbs one parameter at a time along random trajectories through the space and measures the average absolute effect (`mu_star`) and the spread of effects (`sigma`), where a high `sigma` relative to `mu_star` signals interaction or nonlinearity.

```python
def morris_screen(registry, baseline_config, replay_window, trajectories=20, delta=0.3):
    params = list(registry)
    effects = {p: [] for p in params}
    for _ in range(trajectories):
        order = random.sample(params, len(params))
        cfg = dict(baseline_config)
        prev = evaluate_config(cfg, replay_window)
        for p in order:
            step = delta * registry[p].baseline * random.choice([-1, 1])
            cfg[p] = clamp(registry[p], cfg[p] + step)
            cur = evaluate_config(cfg, replay_window)
            effects[p].append(composite_loss(cur) - composite_loss(prev))
            prev = cur
    return {p: {"mu_star": mean(abs(e) for e in effs), "sigma": stdev(effs)}
            for p, effs in effects.items()}
```

`composite_loss` weights the three metrics by cost, with a missed event costing ten times a false alarm, consistent with the cost-weighted ceiling from Day 90. 20 trajectories over 118 parameters is 2,400 replays, which is roughly 27 hours of compute. Screening cut the field to the top 20 by `mu_star`.

### 3. Sobol indices on the shortlist
For the 20 survivors a variance-based analysis is affordable. Saltelli sampling with first-order and total-order Sobol indices separates how much of the output variance each parameter explains on its own from how much it explains including all interactions. A large gap between total and first order marks a parameter whose influence runs through other parameters.

```
parameter                              S1     ST     ST - S1
regime_enter_persistence (Day 68)      0.19   0.27   0.08
regime_hysteresis_margin (Day 68)      0.14   0.31   0.17    <- interacts
cluster_join_threshold (Day 86)        0.11   0.26   0.15    <- interacts
bypass_severity_cutoff (Day 74)        0.10   0.12   0.02
quorum_min_agreement (Day 70)          0.08   0.11   0.03
drift_persistence_gate (Day 65)        0.06   0.09   0.03
transition_timeout_s (Day 69)          0.05   0.09   0.04
cluster_leave_threshold (Day 86)       0.04   0.19   0.15    <- interacts
corroboration_window_s (Day 66)        0.03   0.05   0.02
(9 parameters total)                  0.80   1.19
(remaining 109 parameters, sum)        0.06   0.13
```

The nine listed account for 81% of first-order variance. Of the 109 others, 61 contribute under 0.5% individually, and many of those exist because a mechanism needed some default, not because a decision hung on the value.

### 4. A pairwise interaction sweep that found the flapping
Three of the nine show a large gap between ST and S1, and two of them sit in different mechanisms. A two-dimensional sweep over regime hysteresis margin and cluster join threshold, holding everything else at baseline, showed a clear region of trouble.

```python
def pairwise_sweep(p1, p2, grid=9):
    grid_results = {}
    for v1 in linspace(*registry[p1].range, grid):
        for v2 in linspace(*registry[p2].range, grid):
            out = evaluate_config({**baseline, p1: v1, p2: v2}, replay_window)
            grid_results[(v1, v2)] = {"flaps": count_regime_flips(out), "loss": composite_loss(out)}
    return grid_results
```

Whenever `join - leave < 0.10` for the cluster pair while the regime margin was under 0.15, regime flips per day rose from a baseline of about 3 to over 40. The mechanism is that the cluster membership changed on noise, the regime baseline estimator re-segmented in response, and that moved the correlation input back across the cluster boundary. It is the Day 93 cycle between correlation drift and clustering, now with a measured trigger region. Neither threshold was unsafe alone.

### 5. Declared invariants in the registry, enforced at load
Relationships between constants become first-class. Each invariant is a named predicate with an explanation, evaluated whenever the registry loads and whenever a retune is proposed.

```python
INVARIANTS = [
    Invariant("cluster_hysteresis_gap",
              lambda r: r["cluster_join_threshold"] - r["cluster_leave_threshold"] >= 0.15,
              why="Day 94 sweep: gap below 0.10 caused regime flapping (3/day -> 40/day)"),
    Invariant("regime_margin_vs_cluster_gap",
              lambda r: r["regime_hysteresis_margin"] >= 0.15 or
                        r["cluster_join_threshold"] - r["cluster_leave_threshold"] >= 0.20,
              why="coupled through Day 93 correlation/clustering cycle"),
    Invariant("timeout_exceeds_persistence",
              lambda r: r["transition_timeout_s"] > r["regime_enter_persistence"] * r["estimation_interval_s"],
              why="Day 69 timeout must outlast Day 68's persistence window"),
    Invariant("epoch_budget_floor",
              lambda r: r["cluster_epoch_cap"] >= 2,
              why="Day 88 budget: a cap of 1 disables adaptive clustering silently"),
]

def load_registry(values: dict) -> Registry:
    violated = [i for i in INVARIANTS if not i.predicate(values)]
    if violated:
        raise RegistryInvalid([(i.name, i.why) for i in violated])
    return Registry(values)
```

The invariants are also checked in the preflight gate: a candidate that proposes new constants must declare which existing constants they relate to, and a proposed retune is validated against every invariant before it is accepted.

### 6. Sensitivity tiers change the review process
Each threshold is labelled by its measured influence. High-sensitivity constants (the nine) require replay evidence and a second reviewer for any change. Medium ones require replay evidence. Low-sensitivity constants are frozen at their current value and carry a note that they were measured to have no material effect, so a future change has to justify itself against that note rather than the other way round.

```python
TIERS = {
    "high":   {"requires": ["replay_diff", "second_reviewer", "invariant_check"]},
    "medium": {"requires": ["replay_diff", "invariant_check"]},
    "low":    {"requires": ["invariant_check"], "default": "frozen"},
}
```

## Failure Modes
- **Sensitivity is local to the replay window.** The 90 recorded days contain no extreme geomagnetic event. A threshold that only matters during a storm onset, such as Day 69's transition timeout, shows modest sensitivity here because the window rarely exercises it. A low score on this measure does not mean a constant is unimportant. It may mean the window never tested it, which is the same bias that Day 89 found in the rollout corpus.
- **Morris screening can miss parameters whose influence is a threshold effect.** A constant that does nothing until it crosses a cliff, then fails catastrophically, gets a small average effect from a handful of random steps that rarely straddle the cliff. The screen is a heuristic and the top-20 cut was a budget decision.
- **The invariants encode relationships that were found, not relationships that exist.** Four invariants came from one sweep and a read through the mechanisms. There are certainly other coupled pairs among 118 constants whose interaction was too weak to rank, and a registry that enforces four invariants can create the impression that coupling is handled.
- **Freezing the low-sensitivity tier is a policy bet that the replay window represents the future.** If the traffic mix changes, a frozen constant can become influential with no alert, since nothing re-measures sensitivity on a schedule.

## What's Next
The sensitivity analysis found 61 constants that contribute almost nothing. Constants usually exist because a mechanism needed them, so a pile of near-zero constants suggests a pile of mechanisms that barely change the outcome. That is a harder question than tuning, because it asks whether the code should exist at all. Day 95 runs the experiment the series has avoided for 94 days: turn each mechanism off, replay the same history, and measure what changes.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
