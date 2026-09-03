# Day 40: Rebalancing Without Thrashing — Making Shard Count Adaptive

## TL;DR

Day 39 sharded the lease store to get past the single-store throughput ceiling, but the shard count was fixed at deploy time — sized to a tested worst case, not to whatever load actually shows up. Day 40 makes shard count adaptive: a monitor watches sustained per-shard load and triggers a rebalance when imbalance persists past a threshold, using the Day 39 dual-write handoff window to migrate leases safely. The real problem wasn't detecting imbalance — it was not overreacting to it.

## Context

- **Day 38** fixed the renewal thundering herd with jitter, batching, and a grace buffer.
- **Day 39** sharded the lease store on a composite key (severity tier + hashed sub-agent ID) once batching alone stopped absorbing load past ~150 concurrent sub-agents, with a routing layer resolving shard assignment once at acquisition and a dual-write handoff window for safe shard splits.

Day 39 deliberately picked a modest, fixed shard count sized to the tested worst case rather than a large speculative one — "sharding for load you don't have yet just adds coordination overhead for no benefit." That was the right call at the time. But a fixed shard count only holds up as long as sustained load stays near what was tested. Real space-weather seasons don't cooperate: a quiet week of low-tier monitoring followed by a multi-day geomagnetic storm cascade shifts the load profile for hours or days at a stretch, not just a passing spike. A shard count sized for the average case starts sagging under the sustained case.

## The Problem

- The composite shard key from Day 39 spreads load well for the traffic pattern it was tuned against, but it can't predict a sustained shift in event mix — a week where critical-tier cascades dominate skews load differently than the mixed profile the shard count was originally sized for.
- Manually resizing shards means a human has to notice the imbalance, judge whether it's temporary or sustained, and trigger a reshard — too slow for something that should be a background system property, not an on-call task.
- The obvious automated fix — rebalance whenever any shard's load crosses a threshold — turned out to be actively worse than doing nothing. Short-lived spikes (a single burst spawn, not a sustained trend) triggered rebalances that added migration overhead on top of load that was already about to subside on its own.
- Rebalancing itself isn't free: even with Day 39's dual-write handoff window, a reshard means temporarily doubled write load on the shards involved. Triggering it during the same load spike it's meant to relieve makes the spike worse before it gets better.

## Architecture

### 1. Sustained-imbalance detection, not spike detection

A monitor tracks per-shard load on a rolling window and only flags imbalance if it persists across multiple consecutive windows — not on a single reading.

```python
load_history[shard_id].append(current_load)
if all(l > IMBALANCE_THRESHOLD for l in load_history[shard_id][-SUSTAINED_WINDOWS:]):
    flag_for_rebalance(shard_id)
```

`SUSTAINED_WINDOWS` is set high enough that a single burst spawn — the kind of thing Day 38's jitter and batching already absorb gracefully — can't trigger a reshard on its own. Only a load pattern that holds across several consecutive windows counts as "sustained" rather than "a moment."

### 2. Hysteresis on the rebalance trigger

Flagging imbalance and un-flagging it use different thresholds, so a shard sitting right at the boundary doesn't flap between rebalanced and not-rebalanced.

```python
if not rebalancing and load > UPPER_THRESHOLD:
    start_rebalance(shard_id)
elif rebalancing and load < LOWER_THRESHOLD:
    complete_rebalance(shard_id)
```

Without hysteresis, a shard oscillating near a single threshold value triggers rebalance churn — starting and reversing migrations repeatedly, which is worse than staying imbalanced.

### 3. Rebalance execution reuses Day 39's handoff window

No new migration mechanism was needed. Once a shard is flagged for rebalance, it splits using the same dual-write handoff window Day 39 built for manual resharding — both old and new shard accept renewals for affected leases during the transition, so no in-flight renewal gets rejected as unknown.

```python
new_shard = provision_shard()
migrate_leases(source=shard_id, target=new_shard, handoff_window=HANDOFF_MS)
router.update_mapping(affected_sub_agents, new_shard)
```

### 4. Cooldown after rebalance

After a rebalance completes, that shard is exempt from triggering another rebalance for a cooldown period, even if its load history technically qualifies again.

```python
if shard_id in recent_rebalances and now - recent_rebalances[shard_id] < COOLDOWN:
    skip_rebalance_check(shard_id)
```

This is the second half of the anti-thrashing story: hysteresis stops flapping around one threshold crossing, cooldown stops a shard from immediately re-qualifying right after a migration, before the system has had time to settle into its new distribution.

## Failure Modes

- **Rebalance on any threshold crossing.** The first version rebalanced the moment any shard's load crossed the threshold, full stop. It reacted to every burst spawn Day 38 was already handling fine, adding migration overhead on top of load that would have resolved itself within a couple of renewal cycles. Sustained-window detection fixed this by requiring the imbalance to persist, not just occur.
- **No hysteresis.** A shard sitting near the threshold triggered and reversed rebalances repeatedly — each reversal itself costing a partial migration. Splitting the trigger and release thresholds apart stopped the oscillation.
- **Rebalancing during the load spike it's reacting to.** Migration temporarily increases write load on the shards involved. Triggering a reshard in the middle of the same sustained spike that caused it compounds the problem right when headroom matters most. This didn't get a dedicated fix beyond sustained-window detection and cooldown — but it's worth flagging as unresolved: a true sustained high-load period (not just a spike) still means rebalancing has to happen under load, because waiting for calm isn't always an option.
- **No cooldown.** Without one, a shard that just finished a migration could immediately re-qualify for another if its post-migration load still happened to sit above threshold for a couple of windows, before the system had settled. Cooldown gives the new distribution time to actually take effect before being judged again.

## What's Next

The unresolved point above — rebalancing under genuinely sustained high load, not just reacting to it — is the open question for Day 41. Possibly worth exploring whether shards can pre-emptively split based on forecasted event severity (NOAA space weather alerts arrive with some lead time) rather than purely reactive load monitoring.

*Orbital Watch: watching the sky, one resilient agent at a time.*
