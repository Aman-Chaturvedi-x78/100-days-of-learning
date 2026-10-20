# Day 86: Stop Double-Counting Evidence — Correlation-Aware Canary Testing

## TL;DR
Day 85's sequential canary monitor treats every health signal — error rate, p99 latency, resource-bound drift, determinism mismatch — as an independent stream of evidence, running its own SPRT in isolation. Day 85 already named the problem this causes in its own failure modes: when two signals are secretly degrading for the same underlying reason, evaluating them independently doesn't just miss the correlation, it actively *overstates* confidence, because four semi-redundant signals each crossing their threshold looks like four independent confirmations when it might really be one real signal measured four different ways. This is the exact blind spot Day 71 named for upstream sources and Day 82's taxonomy filed as `UPSTREAM_CORRELATION_BLINDNESS` — still open after four days. Today closes it for canary evaluation specifically, by building a correlation-aware joint sequential test that treats correlated signals as one combined evidence stream instead of several independent ones, reusing Day 71's correlation detection and Day 72's correlation-drift machinery rather than inventing new statistics from scratch.

## The Problem
- Day 85's `SequentialCanaryMonitor` runs one SPRT per health signal and combines verdicts with simple boolean logic: any `REGRESSED` triggers rollback, all `HEALTHY` triggers advancement. That combination rule implicitly assumes each signal's evidence is independent — which is exactly the assumption Day 71 already proved false for upstream sources in this same system, and there's no structural reason canary health signals would be exempt from the same failure.
- Concretely: `error_rate` and `p99_latency` are not independent in Orbital Watch's actual architecture. A single root cause — say, a downstream dependency entering a degraded state — tends to produce both elevated latency (requests queue behind the slow dependency) and elevated errors (requests eventually time out) simultaneously. Two SPRTs each independently accumulating evidence toward `REGRESSED` on the *same underlying event* isn't two confirmations; it's one piece of evidence double-counted, arriving at a `REGRESSED` verdict faster and with apparently stronger statistical backing than the real, single signal actually warrants.
- The inverse failure is just as real and arguably more dangerous: `resource_bound_drift` and `determinism_sample_mismatch` can be correlated in the *other* direction — a mechanism under load naturally produces more scheduling variance, which both looks like resource growth and produces more interleaving-dependent output. If a rollout is marginally unhealthy in a way that shows up faintly in both signals, independent SPRTs can each sit just short of their `REGRESSED` threshold indefinitely, while the true joint evidence — accounting for the fact that both signals are responding to the same underlying marginal issue — would already be conclusive.
- Day 71's fix for this exact shape of problem, applied to independent upstream sources, was a correlation-aware quorum: detect which sources are statistically correlated, collapse correlated groups into a single effective vote, and require real, independent corroboration rather than counting mirrored copies of the same signal. That machinery already exists in this codebase. Nothing from Day 79 onward ever connected it to the canary system, because the canary system (Day 84-85) was built as its own vertical, reusing regime-hysteresis and drift-detection pieces individually without revisiting whether the sources feeding it were actually independent.

## Architecture

### 1. Estimating pairwise signal correlation from historical baseline data
Before any canary runs, the correlation structure between health signals is estimated from historical baseline traffic — the same regime-segmented baseline windows Day 67 already maintains for drift detection, reused here for a different statistic. This produces a correlation matrix that's periodically refreshed, not computed fresh per-canary, because a single canary's own data is far too sparse to estimate correlation reliably on its own.

```python
def estimate_signal_correlation(baseline_window: RegimeBaseline, signals: list[str]) -> np.ndarray:
    # reuses day 67's regime-segmented baseline so correlation is estimated
    # against "normal" behavior, not polluted by past incidents the way
    # day 67 originally kept event data out of the quiet baseline
    series = {s: baseline_window.sample_series(s) for s in signals}
    matrix = np.corrcoef([series[s] for s in signals])
    return matrix  # symmetric, signals x signals, Pearson correlation
```

### 2. Clustering correlated signals, directly reusing Day 71's collapsing logic
Signals whose pairwise correlation exceeds a threshold-registry value (Day 73) get grouped into correlation clusters — the same operation Day 71 performs on upstream sources, just applied to a different kind of node in the same conceptual graph. A cluster of three correlated signals contributes as much *independent* evidence as roughly one signal, not three.

```python
def cluster_correlated_signals(corr_matrix: np.ndarray, signals: list[str],
                                  threshold: float = THRESHOLD_REGISTRY["signal_correlation_cluster"]) -> list[list[str]]:
    # identical algorithm to day 71's source-clustering — union-find over
    # pairs exceeding the correlation threshold, producing disjoint groups
    parent = {s: s for s in signals}
    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, s1 in enumerate(signals):
        for j, s2 in enumerate(signals):
            if i < j and abs(corr_matrix[i][j]) >= threshold:
                union(s1, s2)

    clusters = defaultdict(list)
    for s in signals:
        clusters[find(s)].append(s)
    return list(clusters.values())
```

### 3. A joint SPRT per cluster, not per signal
Instead of one SPRT per signal, each correlation cluster gets exactly one joint sequential test. Within a cluster, a single observation is drawn from whichever signal reports first (or a declared primary signal, if the cluster has one that's cheapest/fastest to sample), and every signal in the cluster shares one accumulated likelihood ratio — directly preventing the double-counting problem, because the cluster's evidence can only move its shared LLR once per real underlying event, not once per correlated symptom of that event.

```python
class ClusteredSequentialMonitor:
    def __init__(self, clusters: list[list[str]], configs: dict[str, SequentialTestConfig]):
        self.clusters = clusters
        self.configs = configs
        self.cluster_state = {tuple(c): {"successes": 0, "trials": 0} for c in clusters}

    def observe(self, signal: str, passed: bool):
        cluster = self._cluster_for(signal)
        # one observation from any member updates the SHARED cluster state —
        # this is the structural fix: correlated signals can no longer each
        # independently accumulate evidence toward the same verdict
        self.cluster_state[cluster]["trials"] += 1
        if passed:
            self.cluster_state[cluster]["successes"] += 1

    def _cluster_for(self, signal: str) -> tuple:
        for c in self.cluster_state:
            if signal in c:
                return c
        raise ValueError(f"signal {signal} not in any declared cluster")

    def check(self) -> dict[tuple, str]:
        decisions = {}
        for cluster, state in self.cluster_state.items():
            # uses the most conservative (widest-tolerance) config among a
            # cluster's member signals, so merging clusters never accidentally
            # makes the test LESS sensitive than its strictest member alone
            cfg = min((self.configs[s] for s in cluster), key=lambda c: c.unacceptable_rate - c.acceptable_rate)
            s, t = state["successes"], state["trials"]
            if t == 0:
                decisions[cluster] = "CONTINUE"
                continue
            llr = sprt_log_likelihood_ratio(s, t, cfg.acceptable_rate, cfg.unacceptable_rate)
            decisions[cluster] = sprt_decision(llr, cfg.alpha, cfg.beta)
        return decisions
```

### 4. Singleton clusters degrade cleanly to Day 85's original behavior
A signal with no correlated partners forms its own cluster of size one, and the joint SPRT for a singleton cluster is mathematically identical to Day 85's original per-signal SPRT — this isn't a parallel system that replaces Day 85's, it's a strict generalization of it. Day 85's design wasn't wrong; it was the special case of today's design where the correlation matrix happens to be the identity matrix.

```python
def build_clustered_monitor(stage: CanaryStage, baseline: RegimeBaseline) -> ClusteredSequentialMonitor:
    corr = estimate_signal_correlation(baseline, stage.health_signals)
    clusters = cluster_correlated_signals(corr, stage.health_signals)
    # clusters is a list of lists; any singleton list behaves exactly like
    # day 85's independent-signal SPRT for that one signal
    return ClusteredSequentialMonitor(clusters, SIGNAL_CONFIGS)
```

### 5. Feeding cluster membership back into incident triage
When a rollback does happen, the triage logic from Day 84 now reports which *cluster* triggered it, not which individual signal — which directly improves the quality of evidence fed back into Day 82's coverage map. A rollback attributed to "the latency/error cluster" is a more honest, less noisy piece of evidence than two separate rollback records that look like two independent confirmations of the same underlying gap.

```python
def triage_rollback_clustered(rollback, clusters: list[list[str]]):
    triggering_cluster = next(c for c in clusters if rollback.signal in c)
    mapped_dims = set()
    for signal in triggering_cluster:
        mapped_dims |= HEALTH_SIGNAL_TO_TAXONOMY.get(signal, set())
    preflight_gap_evidence.record(
        mechanism=rollback.mechanism,
        dims=mapped_dims,
        cluster=triggering_cluster,
        note=f"correlated cluster {triggering_cluster} triggered rollback as one event, not {len(triggering_cluster)} independent ones",
    )
```

## Failure Modes
- **Correlation estimated from historical baseline data can be wrong for the specific mechanism currently under canary.** A new mechanism can introduce a *new* correlation between two previously-independent signals — for instance, a caching layer that makes latency and error rate newly correlated in a way the pre-existing baseline never captured, because the baseline was estimated from mechanisms that didn't have this coupling. Clustering based on stale correlation data would then treat two genuinely-correlated signals as independent, reopening exactly the double-counting problem today's fix closes, just for a different pair.
- **The "declared primary signal, if cheapest to sample" option in step 3 silently drops information from the other cluster members if implemented naively** — sampling from one representative signal instead of genuinely combining evidence from all of them trades a double-counting problem for an under-utilization problem, throwing away real (if correlated) evidence rather than appropriately discounting it. The shared-trial-count version above avoids this specific trap, but it's worth flagging explicitly that the easier-looking implementation path was a worse one.
- **Clustering is run once at stage start and held fixed for that stage's duration**, which assumes correlation structure is stable across the observation window — but a mechanism that behaves differently under 1% traffic than under 50% traffic (entirely plausible; some correlations only emerge under real concurrent load) could have its correlation structure change mid-stage without the clustering ever being re-evaluated. This is structurally the same staleness risk Day 72 already named for corroboration quorum correlation, just not yet re-applied here.
- **The "most conservative config among cluster members" rule in step 3 is a reasonable default but not obviously the correct one in every case** — it guarantees clustering never loosens sensitivity below any individual member's own threshold, but it also means a cluster's effective sensitivity is dictated by whichever member happened to have the tightest tolerance, which may not be the signal most relevant to the specific way a given mechanism is likely to fail.

## What's Next
Step 4's failure mode and Day 72's correlation-drift detection point at the same open thread: correlation structure estimated once at stage start can go stale during the stage itself, the same way Day 72 found correlation between upstream sources could drift over time. Day 87 asks whether correlation clustering can be re-estimated *online*, mid-canary, as new data arrives — and, if it can, what happens to a joint SPRT's accumulated evidence when the clusters it was grouped under change shape partway through the test it's already running.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
