# Day 45: When the Link Degrades Mid-Loan

**TL;DR:** Day 44 made loan TTLs latency-aware at grant time — computed once from a smoothed RTT estimate and then frozen for the life of the loan. That's fine if the link stays put. It doesn't if the link gets worse *after* the loan is issued. Today: continuous RTT monitoring during the loan's lifetime, and a graceful re-request path that renegotiates the TTL before it expires — instead of either blindly extending a stale estimate or yanking capacity out from under the borrower.

## The Problem

- Day 44's TTL is a point-in-time calculation: smoothed RTT at grant time → floor/ceiling clamp → done. Nothing revisits it.
- If a link degrades mid-loan (congestion, partial outage, route flap), the loan's TTL is now based on stale, optimistic latency data.
- Two naive fixes and why neither works alone:
  - **Blind TTL extension** — if the region just keeps extending the same TTL on a timer, it compounds the original estimate's error instead of correcting it. A loan issued on a good link and degrading afterward would just keep getting stretched on bad information.
  - **Forced re-request** — killing the loan the moment degradation is detected and making the borrower re-request from scratch is disruptive: in-flight sub-agent work tied to that borrowed capacity gets orphaned, and it re-triggers the full admission-controller priority-scoring path (Day 42) for something that's still mid-flight, not a new ask.
- Underlying issue: the gossip heartbeat (Day 43) was only ever used passively, as a one-shot RTT probe at grant time. It's already running continuously — it just wasn't being *used* continuously.

## Architecture

1. **Promote the gossip heartbeat to a live RTT feed, not a one-shot probe**

```python
class LoanRTTMonitor:
    def __init__(self, loan, rolling_window):
        self.loan = loan
        self.rtt_samples = rolling_window  # same rolling median as Day 44's grant-time calc

    def on_heartbeat(self, sample_rtt):
        self.rtt_samples.push(sample_rtt)
        current_estimate = self.rtt_samples.median()
        if self._degraded(current_estimate):
            self._trigger_regrant(current_estimate)
```

2. **Define degradation as drift from the grant-time estimate, not an absolute threshold**

```python
def _degraded(self, current_estimate):
    baseline = self.loan.grant_time_rtt_estimate
    drift = (current_estimate - baseline) / baseline
    return drift > DEGRADATION_DRIFT_THRESHOLD
```
   A relative-drift check, not an absolute RTT ceiling, so it fires consistently across region pairs with very different baseline latencies.

3. **Graceful re-request: overlap old and new loan instead of a hard cutover**

```python
def _trigger_regrant(self, current_estimate):
    new_ttl = compute_latency_aware_ttl(current_estimate)  # Day 44's formula, fresh inputs
    pending = admission_controller.request_regrant(
        loan=self.loan,
        new_ttl=new_ttl,
        priority=self.loan.original_priority,  # skip re-scoring, this is a renewal not a new ask
    )
    # borrower keeps operating on the OLD lease until pending resolves —
    # no gap, no orphaned in-flight work
    self.loan.pending_regrant = pending
```

4. **Admission controller treats regrants as a fast path, not a new admission**

   Reuses Day 42's admission controller, but a regrant request carries the loan's original priority score forward instead of re-competing from a cold priority. It's the same slot, recomputed TTL — not a new borrower entering the queue.

## Failure Modes

- **Drift threshold too sensitive** — a link with naturally noisy but not actually degrading RTT triggers regrant churn. Mitigated by requiring the drift condition to hold across `N` consecutive heartbeat samples (same debounce logic as Day 38's storm fix), not a single noisy sample.
- **Regrant request itself fails because the link is now too degraded to gossip reliably** — the monitor keeps the borrower on the old (undersized) TTL as a fallback, which just means an earlier-than-ideal but still-safe expiry. Better than a hang.
- **Old and new lease overlap window outlives its usefulness** — if the pending regrant never resolves before the old TTL actually expires, the system falls back to Day 44's plain forced re-request path. The graceful path is an optimization on top of the existing safety net, not a replacement for it.

## What's Next

Day 45 assumes the peer region is still reachable, just slower. Day 46's open question: what happens when the gossip heartbeat goes fully silent mid-loan — not degraded, but gone. Is a missing heartbeat treated as "assume worst-case RTT" or "assume peer region is down and fail the loan safe"?

*Orbital Watch: a multi-agent system slowly learning that most distributed systems problems are actually the same problem wearing a different hat.*
