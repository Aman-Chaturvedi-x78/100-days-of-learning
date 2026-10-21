# Day 87: Correlation Moves Mid-Test — Re-Estimating Clusters Online Without Corrupting the Evidence

## TL;DR
Day 86 grouped correlated canary signals into clusters and ran one shared sequential test per cluster, closing the double-counting hole that Day 85 left open. But it computed the clustering exactly once, at stage start, from historical baseline data, and held it fixed for the whole observation window. Day 86's own failure modes named the weakness: a mechanism can introduce a brand-new coupling between two signals that the baseline never contained, and some correlations only appear under real concurrent load, which means a clustering that was right at 1% traffic can be wrong at 10%. Today makes the clustering live: correlation is re-estimated continuously from the canary's own data, with a persistence gate so it doesn't flap. The hard part isn't the re-estimation. It's what to do with the evidence a sequential test has already accumulated when the clusters it was accumulated under stop existing. The answer turns out to be that you never patch accumulated state at all. You keep a ledger of raw observations and recompute verdicts from it under whichever clustering is current.

## The Problem
- A joint SPRT's state is a running log-likelihood ratio, and that number is only meaningful relative to the grouping it was built under. A cluster `{error_rate, p99_latency}` has an LLR that summarizes how surprising the shared stream of observations is, treating those two signals as one evidence source. If a later re-estimate decides `p99_latency` is actually independent of `error_rate` at this traffic level, there is no valid way to decompose that LLR back into two. The information about which observation contributed what was discarded the moment the two streams were merged into one counter.
- The inverse case is worse. Suppose `resource_bound_drift` and `determinism_sample_mismatch` start out as two singleton clusters, each with its own accumulated LLR, and mid-stage the re-estimate decides they're correlated. Merging means producing one LLR from two. Adding them is exactly the double-counting error Day 86 exists to prevent. Taking the max throws away real evidence. Averaging has no statistical justification. Every arithmetic option on the accumulated scalars is wrong in a different direction, because the scalars were never the right thing to store.
- Day 72 already established, for upstream sources, that correlation is itself a time-varying quantity and that reacting to it naively causes flapping. A re-estimate computed from a short window is noisy. Two signals that happen to move together for ten minutes will show a high sample correlation without any real coupling. If clusters re-form on every noisy estimate, the sequential test's accumulated evidence gets thrown away and rebuilt continuously, and the test never reaches a verdict.
- The same structure already exists elsewhere in this system and was solved there. Day 52 kept both the raw result and the projected hash in the effect ledger, precisely so comparison logic could change without losing the underlying data. Day 64 versioned trust with epochs so that a producer-side change invalidated downstream conclusions cleanly rather than leaving stale state half-valid. Neither idea has been applied to the canary monitor yet, because the monitor was built storing derived state (counters, LLRs) instead of source data.

## Architecture

### 1. An observation ledger instead of running counters
The monitor's primary state changes from "successes and trials per cluster" to an append-only ledger of individual signal observations, timestamped and tagged by signal. Counters and LLRs become derived views computed from the ledger, never the source of truth. This is Day 52's dual-storage principle applied to monitoring: raw data is kept so that the interpretation layer can be changed without data loss.

```python
@dataclass(frozen=True)
class Observation:
    ts: float
    signal: str
    passed: bool
    request_id: str   # lets us recognize one request observed via several signals

class ObservationLedger:
    def __init__(self, max_entries: int = 200_000):
        self._entries: deque[Observation] = deque(maxlen=max_entries)   # bounded per Day 83's rule

    def append(self, obs: Observation):
        self._entries.append(obs)

    def window(self, since_ts: float) -> list[Observation]:
        return [o for o in self._entries if o.ts >= since_ts]

    def for_signals(self, signals: set[str]) -> list[Observation]:
        return [o for o in self._entries if o.signal in signals]
```

The `request_id` field matters more than it looks. When two correlated signals both fire on the same underlying request (a slow request that also errors), the ledger can see that they're two views of one event. That gives a cleaner way to avoid double-counting than inferring it from correlation coefficients alone: collapse observations that share a `request_id` within a cluster into one.

### 2. Online correlation estimation with a persistence gate
Correlation is estimated over a sliding window of the ledger, and a proposed re-clustering only becomes active if the new clustering has been stable across several consecutive re-estimates. This reuses Day 65's persistence gate (a statistical signal must persist before it counts) and Day 68's hysteresis (the threshold to leave a cluster is lower than the threshold to join one), both now applied to correlation structure rather than to field distributions or regime state.

```python
class OnlineClusterTracker:
    def __init__(self, signals, join_threshold=0.70, leave_threshold=0.50, persistence=3):
        self.signals = signals
        self.join_threshold = join_threshold      # THRESHOLD_REGISTRY entries (Day 73)
        self.leave_threshold = leave_threshold    # lower than join: hysteresis (Day 68)
        self.persistence = persistence            # consecutive agreeing estimates (Day 65)
        self.active = [[s] for s in signals]
        self.epoch = 0
        self._pending = None
        self._pending_count = 0

    def propose(self, corr_matrix) -> bool:
        candidate = self._cluster(corr_matrix)
        if self._same(candidate, self.active):
            self._pending, self._pending_count = None, 0
            return False
        if self._pending is not None and self._same(candidate, self._pending):
            self._pending_count += 1
        else:
            self._pending, self._pending_count = candidate, 1
        if self._pending_count >= self.persistence:
            self.active = self._pending
            self.epoch += 1                        # Day 64: clustering is an epoch-versioned fact
            self._pending, self._pending_count = None, 0
            return True
        return False

    def _cluster(self, corr):
        # join at join_threshold, but an existing pair stays together until
        # correlation falls below leave_threshold
        def linked(i, j):
            r = abs(corr[i][j])
            already = any(self.signals[i] in c and self.signals[j] in c for c in self.active)
            return r >= (self.leave_threshold if already else self.join_threshold)
        parent = list(range(len(self.signals)))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i in range(len(self.signals)):
            for j in range(i + 1, len(self.signals)):
                if linked(i, j):
                    parent[find(i)] = find(j)
        groups = defaultdict(list)
        for i, s in enumerate(self.signals):
            groups[find(i)].append(s)
        return [sorted(g) for g in groups.values()]

    @staticmethod
    def _same(a, b):
        return {tuple(sorted(c)) for c in a} == {tuple(sorted(c)) for c in b}
```

### 3. On a cluster change, recompute from the ledger, never convert state
When the tracker's epoch increments, the monitor does not attempt to split or merge any accumulated LLR. It throws away all derived verdict state and recomputes each cluster's SPRT from the ledger's observations under the new grouping. Within a cluster, observations sharing a `request_id` are collapsed to one, so a request seen through two correlated signals contributes one trial.

```python
class LedgerBackedCanaryMonitor:
    def __init__(self, stage, configs, ledger, tracker, stage_start_ts):
        self.stage, self.configs = stage, configs
        self.ledger, self.tracker = ledger, tracker
        self.stage_start_ts = stage_start_ts

    def cluster_counts(self, cluster: list[str]) -> tuple[int, int]:
        seen_requests = {}
        for o in self.ledger.for_signals(set(cluster)):
            if o.ts < self.stage_start_ts:
                continue
            # one request observed via several correlated signals counts once;
            # a failure on any member signal makes the collapsed trial a failure
            prior = seen_requests.get(o.request_id, True)
            seen_requests[o.request_id] = prior and o.passed
        trials = len(seen_requests)
        successes = sum(1 for ok in seen_requests.values() if ok)
        return successes, trials

    def verdicts(self) -> dict[tuple, str]:
        out = {}
        for cluster in self.tracker.active:
            s, t = self.cluster_counts(cluster)
            if t == 0:
                out[tuple(cluster)] = "CONTINUE"
                continue
            cfg = min((self.configs[x] for x in cluster),
                      key=lambda c: c.unacceptable_rate - c.acceptable_rate)
            llr = sprt_log_likelihood_ratio(s, t, cfg.acceptable_rate, cfg.unacceptable_rate)
            out[tuple(cluster)] = sprt_decision(llr, cfg.alpha, cfg.beta)
        return out
```

Recomputation costs more than incrementing a counter. That cost is the explicit trade, and it's affordable here because canary stages are minutes to hours long and the ledger is bounded. The benefit is that no verdict ever depends on a lossy conversion of state from a grouping that no longer applies.

### 4. A stage-level guard against flapping verdicts
One consequence of recomputation: a verdict can change because the clustering changed, not because new data arrived. A stage that read `CONTINUE` at epoch 2 might read `REGRESSED` at epoch 3 on identical data. That's legitimate, since it means the earlier grouping was hiding something, but it should be visible and logged distinctly from a data-driven transition.

```python
def run_stage_with_online_clusters(monitor: LedgerBackedCanaryMonitor, router, mechanism):
    last_epoch = monitor.tracker.epoch
    while True:
        for ev in live_metrics_registry.stream(mechanism, since=monitor.cursor):
            monitor.ledger.append(Observation(ev.ts, ev.signal, ev.passed, ev.request_id))
        corr = estimate_signal_correlation_from_ledger(monitor.ledger, monitor.tracker.signals,
                                                         window_seconds=CORR_WINDOW_SECONDS)
        monitor.tracker.propose(corr)
        if monitor.tracker.epoch != last_epoch:
            incident_log.record(mechanism=mechanism.name, event="cluster_epoch_change",
                                  epoch=monitor.tracker.epoch, clusters=monitor.tracker.active)
            last_epoch = monitor.tracker.epoch
        verdicts = monitor.verdicts()
        if "REGRESSED" in verdicts.values():
            router.set_traffic_percent(mechanism, 0.0)
            raise CanaryRollbackTriggered(mechanism.name, monitor.stage, verdicts)
        if all(v == "HEALTHY" for v in verdicts.values()):
            return "HEALTHY"
        sleep(POLL_INTERVAL_SECONDS)
```

### 5. Correlation estimated per stage, with carry-forward as a prior
The tracker's final cluster assignment at the end of one canary stage becomes the starting assignment for the next, not a fresh read of the historical baseline. A correlation that emerged at 10% traffic is plausibly still present at 50%, and starting from that assumption is more informative than starting from a baseline that predates the mechanism. The persistence gate still applies in both directions, so a carried-forward cluster that the new stage's data contradicts will dissolve, but only after persistent evidence.

## Failure Modes
- **Recomputing verdicts under a changed clustering introduces a decision-procedure problem that the fixed-clustering SPRT never had.** An SPRT's error guarantees (the `alpha` and `beta` in Day 85's config) hold for a single pre-specified test run once over a stream of data. A procedure that can change its own grouping, recompute, and re-read the verdict is no longer that test. Each re-clustering is another chance for the data to look conclusive under some grouping, which inflates the realized false-positive rate above the nominal `alpha`. The persistence gate limits how often this happens but does not remove it. Today's design is knowingly incomplete on this point, and it is where Day 88 picks up.
- **The ledger's `deque(maxlen=...)` cap silently drops the oldest observations during a long stage.** That keeps memory bounded, but recomputation from a truncated ledger means a late re-clustering sees only recent history, and the verdict can drift away from what a full-history computation would have said. Truncation needs to be recorded and surfaced rather than happening quietly, or the system will disagree with itself in ways no one can reproduce.
- **Collapsing observations by `request_id` assumes request identity is reliably propagated across all the instrumentation producing signals.** If `resource_bound_drift` is sampled periodically and has no per-request ID, it can't be collapsed with per-request signals at all, and the ledger needs a defined rule for such signals. Today they're treated as their own trials, which is correct for double-counting but means mixed-granularity clusters have uneven weighting between their members.
- **The persistence gate adds latency to adapting to a real change.** Requiring three consecutive agreeing estimates means a genuine new coupling can sit unrecognized for three estimation intervals. During that window the system is running the Day 85 failure mode (double-counting) knowingly. This is the same detection-delay-versus-flapping tradeoff Day 68 accepted for regime transitions, now applied to a decision with rollback consequences.

## What's Next
The first failure mode above is a real problem rather than an implementation detail. The moment the grouping becomes adaptive, the nominal `alpha` and `beta` that justified the canary's stopping rule stop being the realized error rates, and nothing measures the gap. Day 88 puts a number on how far realized error rates drift from nominal under adaptive clustering using a simulation harness in the style of Day 78's retroactive scrutiny, then adds an error-budget mechanism that caps how much re-clustering a single stage is allowed to do, and turns the whole question into a standing check in the preflight gate.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
