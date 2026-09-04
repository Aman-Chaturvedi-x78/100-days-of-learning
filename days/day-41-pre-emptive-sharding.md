# Day 41: Pre-emptive Sharding — Splitting Ahead of the Storm, Not During It

## TL;DR

Day 40 closed with an open question: reactive rebalancing works, but it still means splitting shards under the exact sustained load pressure you're trying to relieve. Day 41 adds a second trigger alongside the reactive one — forecasted event severity. NOAA space weather alerts arrive with real lead time before a storm actually escalates, so shards can now split ahead of the load, not just in response to it. The reactive path from Day 40 didn't go away; it's the fallback for whatever the forecast misses.

## Context

- **Day 39** sharded the lease store on a composite key once single-store throughput became the ceiling.
- **Day 40** made shard count adaptive — sustained-window imbalance detection, hysteresis, and a cooldown period so rebalancing doesn't thrash — but explicitly flagged that rebalancing under genuinely sustained high load still means migrating shards while under the same pressure that caused the imbalance.

That tension is structural to any purely reactive system: by the time load is high enough to trigger a rebalance, the rebalance itself competes with that load for resources. The way out isn't a smarter reactive trigger — it's not being purely reactive at all.

## The Problem

- Solar wind and geomagnetic events don't arrive instantly. NOAA issues watches and warnings with real lead time — a G3+ geomagnetic storm watch can precede the actual event by hours. That lead time was sitting unused; the system only reacted once sub-agents were already spawning and load was already climbing.
- Purely reactive rebalancing (Day 40) means the migration itself — even with the dual-write handoff window from Day 39 — happens concurrently with the load spike it's trying to relieve. Best case, it catches up. It never gets ahead.
- The obvious fix — "just split shards whenever a severe alert comes in" — has its own failure mode: forecasts aren't guarantees. A watch can escalate to a warning or quietly downgrade. Splitting shards for every severe watch, regardless of whether it actually materializes, means paying migration overhead for storms that fizzle.
- Two trigger sources (forecast-based and load-based) running independently risk double-triggering a rebalance on the same underlying event — one shard getting flagged by both signals within the same window and getting split twice, or split while a previous split is still mid-handoff.

## Architecture

### 1. Forecast ingestion as a leading signal

A lightweight subscriber watches NOAA space weather alerts and maps alert severity to an expected sub-agent spawn multiplier per tier, based on how past alerts of similar severity actually played out.

```python
alert = noaa_feed.latest()
expected_multiplier = severity_to_multiplier(alert.category, alert.scale)  # e.g. G3 -> 2.5x critical-tier spawns
```

### 2. Confirmation gating before acting on a forecast

Rather than triggering on the first watch, the pre-emptive path only fires once an alert reaches a confirmation tier — escalated from watch to warning, or sustained across consecutive forecast updates — filtering out alerts likely to downgrade before they matter.

```python
if alert.status == "warning" or alert.consecutive_watch_updates >= CONFIRMATION_THRESHOLD:
    trigger_preemptive_split(affected_tier, expected_multiplier)
```

### 3. One rebalancer, two trigger inputs

Day 40's sustained-window/hysteresis/cooldown machinery didn't get replaced — it got a second input. The forecast signal can trigger a split immediately (it's a leading indicator, not noise that needs a sustained window to confirm), but it still respects the same cooldown so it can't stack with a split already in progress on the same shard.

```python
def maybe_rebalance(shard_id):
    if shard_id in in_progress_splits:
        return  # already handling this shard, regardless of trigger source
    if forecast_trigger(shard_id) or sustained_load_trigger(shard_id):  # Day 40's reactive path, unchanged
        start_rebalance(shard_id)
```

### 4. Lead-time margin

A pre-emptive split only helps if it finishes before the forecasted load actually arrives. The confirmation gate factors in typical migration duration (from Day 39's handoff mechanism) against typical alert-to-escalation timing, so there's a real margin — not a split that's still mid-handoff when the storm hits.

## Failure Modes

- **Trusting every watch as if it were certain.** The first version triggered a pre-emptive split on any severe watch, no confirmation gate. A meaningful fraction of watches never escalated, so shards were splitting — and paying migration overhead — for events that never generated the load they were sized for. The confirmation gate fixed this by requiring escalation or sustained persistence before acting.
- **No coordination between the two triggers.** Early on, forecast and load triggers ran as fully independent watchers. A severe, confirmed alert could fire the forecast trigger while load was already climbing fast enough to independently trip the Day 40 sustained-load trigger — both firing on the same shard within the same window, attempting two splits at once. Routing both signals through a single rebalancer with an in-progress check fixed this.
- **Insufficient lead-time margin.** An early pre-emptive split triggered on confirmation but didn't account for how long the migration itself takes. In one simulated cascade, the split was still mid-handoff when the real load arrived — worse than not pre-splitting at all, since it added migration overhead on top of live load instead of ahead of it. Factoring expected migration duration into the confirmation gate's timing fixed this.
- **Assuming pre-emption replaces the reactive path.** Forecasts can still be wrong, missed, or arrive too late to matter. The Day 40 reactive path stayed fully in place, unmodified — pre-emption is a head start, not a replacement for the fallback that catches whatever the forecast doesn't.

## What's Next

Forecast-to-multiplier mapping is currently based on a small set of historical alert outcomes — worth tracking prediction accuracy over time and feeding it back into the confirmation threshold, so the gate tightens or loosens based on how reliable NOAA's own escalation pattern has actually been for Orbital Watch's specific workload.

*Orbital Watch: watching the sky, one resilient agent at a time.*
