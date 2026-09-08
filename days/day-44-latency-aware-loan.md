# Day 44: Latency-Aware Loan TTLs for Asymmetric Inter-Region Links

## TL;DR
Day 43 gave Orbital Watch's admission controller the ability to federate across regions: a saturated region could borrow spare capacity slots from a peer via a slow capacity-gossip heartbeat, with loans bounded by a TTL so borrowed capacity always eventually returns home. That TTL was a single global constant. Day 44 replaces it with a per-region-pair value derived from measured round-trip latency, because "how long should a loan last" turns out to be inseparable from "how far away is the lender." The fix reuses infrastructure that already existed — no new probing system, just a new use for the gossip traffic Day 43 introduced.

## The Problem

Day 43's loan mechanism worked like this: a saturated region requests a slot loan from a peer with spare capacity, the peer grants it with a TTL attached, and when the TTL expires the slot reverts to the lender automatically — no explicit release message required, which was deliberate, since Day 42 already showed what happens when coordination messages get lost or delayed under load.

The problem is what "TTL" was actually standing in for. A loan's TTL has to cover:

1. The time for the grant to propagate back to the borrower over the gossip channel.
2. The time for the borrower to actually start using the slot (spin up a sub-agent, attach it to the resource-lease system from Day 37-39).
3. Some working buffer so the borrower isn't racing the clock on every operation.
4. The time for the *eventual* reclaim signal (or expiry) to be meaningful to the lender's own capacity accounting.

Every one of those four legs is dominated by inter-region network latency, and inter-region latency is not remotely uniform. In a real multi-region deployment:

- us-east ↔ us-west: roughly 60-70ms RTT
- us-east ↔ eu-west: roughly 70-90ms RTT
- us-east ↔ ap-south: routinely 200ms+ RTT, higher and more variable under transatlantic/transpacific route congestion

A flat TTL tuned for the us-east/us-west case is dangerously short for the ap-south case, and a flat TTL tuned to be safe for ap-south is wastefully long for us-west.

**Concretely, two failure directions:**

- **TTL too short for the pair:** the grant message, or the borrower's spin-up, hasn't finished propagating before the TTL clock runs out. The lender reclaims a slot the borrower hasn't even started using yet — or worse, reclaims mid-use, which under Day 35/36's teardown logic triggers a forced (not graceful) teardown of whatever sub-agent the borrower spun up on it. That's a false reclaim: capacity gets ping-ponged and wasted rather than doing useful work, and the borrowing region is right back to being saturated, having burned a round-trip for nothing.
- **TTL too long for the pair:** the lender's capacity sits committed to a far-away borrower for longer than the actual handoff required. If the lender itself then hits a local load spike (say, its own storm confirmation from Day 41's forecast path), it has to wait out a TTL that's longer than it needed to be before it can reclaim what should already be back in its own pool. This is the same class of problem Day 42 solved for *simultaneous local* splits — a resource held longer than necessary starves a legitimate local need — except now the axis is geographic instead of temporal.

Neither direction is hypothetical under Orbital Watch's actual event pattern: a solar storm can hit while multiple regions are watching the same NOAA SWPC feed, so a region with a fast link to its lender and a region with a slow link to *its* lender can both be mid-loan at the same moment, and a single global TTL cannot be correct for both simultaneously.

## Architecture

### 1. Passive RTT sampling via the existing gossip channel

The obvious naive fix is to add a dedicated ping/pong RTT probe between every region pair. That's a second background protocol to run, tune, and reason about failure modes for — and Orbital Watch already has a periodic message crossing every region pair: the capacity-gossip heartbeat from Day 43. Every heartbeat round-trip is already timestamped for staleness detection, so RTT sampling is free:

```python
def on_gossip_response(region_pair, sent_ts, recv_ts):
    rtt = recv_ts - sent_ts
    rtt_estimates[region_pair].push(rtt)  # rolling window, matches gossip cadence
```

No new wire protocol, no new failure surface, no extra load on the network. The only change is that the gossip handler now feeds a second downstream consumer (the RTT estimator) in addition to its Day 43 job (capacity availability exchange).

### 2. Rolling, cadence-matched smoothing

A single sample is noisy — one slow gossip round shouldn't swing the TTL for the next loan grant. The estimator keeps a bounded rolling window sized to the gossip cadence itself, so the smoothing horizon is proportional to how often fresh data actually arrives:

```python
class RollingRTT:
    def __init__(self, window=GOSSIP_CADENCE_WINDOW):
        self.samples = deque(maxlen=window)

    def push(self, rtt):
        self.samples.append(rtt)

    def smoothed(self):
        return median(self.samples) if self.samples else DEFAULT_RTT
```

Median over mean was a deliberate choice, not a default. Mean is pulled hard by a single congested round-trip; median absorbs it without needing a separate outlier-rejection pass. Given that gossip cadence is already "slow" by design (Day 43 explicitly kept it slow to avoid becoming a second heartbeat-storm surface like Day 38), a median over a modest window converges fast enough relative to how often TTLs are actually computed — loan grants are much rarer events than gossip rounds.

### 3. Per-pair TTL derivation at grant time

TTL is no longer read from a constant. It's computed at the moment a loan is granted, from that specific pair's current smoothed RTT:

```python
def compute_loan_ttl(region_pair):
    rtt = rtt_estimates[region_pair].smoothed()
    ttl = BASE_HANDOFF_TIME + LATENCY_MULTIPLIER * rtt
    return clamp(ttl, MIN_TTL, MAX_TTL)
```

`BASE_HANDOFF_TIME` covers the fixed, latency-independent cost — sub-agent spin-up, lease-system attachment — that exists even for a same-region loan. `LATENCY_MULTIPLIER * rtt` covers the propagation cost, scaled up rather than added 1:1, since a loan needs headroom for more than one round-trip (grant propagation *and* the eventual expiry/reclaim signal being meaningful) rather than just a single leg.

The clamp is not cosmetic. Without `MAX_TTL`, a genuinely pathological link (packet loss triggering retries, a route flap) could compute a TTL long enough to defeat the entire purpose of bounding loans — a region could sit on borrowed capacity for an unreasonable stretch just because its RTT estimate briefly spiked. Without `MIN_TTL`, a near-zero RTT pair (two regions in the same metro, or a same-region degenerate case) could compute a TTL shorter than `BASE_HANDOFF_TIME` alone requires, causing false reclaims on the *fastest* links — the opposite failure from what latency-awareness was supposed to fix.

### 4. Cold-start handling

A region pair with no gossip history yet — a newly added region, or a pair that's never needed to exchange spare capacity before — has no RTT samples to smooth. Rather than blocking the first loan on that pair, `RollingRTT.smoothed()` falls back to `DEFAULT_RTT`, set conservatively (closer to the ap-south end of the observed range than the us-west end), so a cold-start loan errs toward the safer failure direction — TTL slightly too generous rather than slightly too tight — until enough real samples accumulate to replace the default.

## Failure Modes

- **Median smoothing lags real degradation.** A rolling median over a window sized for *typical* jitter will, by construction, respond slowly to a real, sustained shift in link quality (a route change, not just transient congestion). If a link degrades and stays degraded, the smoothed estimate takes a full window to catch up, during which TTLs are computed slightly too short for the *new* reality — this is exactly the gap Day 45's "mid-loan degradation" question grows out of.
- **Clamp bounds tuned too tight silently reintroduce Day 43's flat-TTL bug.** If `MIN_TTL`/`MAX_TTL` are set close together "to be safe," every computed TTL gets pulled toward the same clamped value regardless of actual RTT, which is functionally the old constant-TTL behavior wearing a latency-aware costume. The bounds need to be wide enough that only genuinely pathological pairs hit them — verified in testing by checking that the *median* computed TTL across a realistic mix of region pairs sits meaningfully inside the clamp range, not pinned to an edge.
- **Cold-start default has to be wrong in the safe direction, not just wrong.** Picking `DEFAULT_RTT` as an average across known pairs seems reasonable but isn't — an average would be too short for a genuinely far, never-before-seen pair (say, a new region on a different continent from everything else). The default has to be conservative specifically, biased toward the slow end of the known distribution, even though that means slightly over-long TTLs for cold-start pairs that turn out to be fast.
- **RTT sampling piggybacking on gossip couples two concerns that used to be independent.** Before Day 44, a gossip round-trip only mattered for capacity-availability freshness (Day 43). Now it also feeds TTL computation. A future change to gossip cadence (for capacity-freshness reasons) has a second-order effect on TTL responsiveness that's easy to forget about if the two concerns aren't documented as coupled.

## What's Next
Every fix in this post treats the link as static for the lifetime of a granted loan — RTT is sampled and the TTL computed once, at grant time, and never revisited before expiry. Day 45 picks at the seam that leaves open: what happens when a region's outbound link degrades genuinely *during* an active loan, after the TTL was already fixed? Does the borrower get a mid-loan TTL extension based on fresh gossip data, or does a degraded link during an active loan just force a clean re-request from scratch — accepting the cost of restarting the handoff rather than trying to patch a TTL that's already ticking?

*Building Orbital Watch, one failure mode at a time.*
