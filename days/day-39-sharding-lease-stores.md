# Day 39: Sharding the Lease Store — When Batching Alone Stops Being Enough

## TL;DR

Day 38 fixed the renewal thundering herd with jitter, batching, and a grace buffer — but all of that still funnels through a single lease store. As sub-agent counts keep climbing past what one store can absorb even with batching, the store itself becomes the next ceiling. Day 39 partitions the lease store across shards, keyed on a composite of severity tier and sub-agent ID hash, with a thin routing layer in front and per-shard batching behind it. The interesting part wasn't the sharding itself — it was picking a shard key that didn't just recreate a hot shard in a different shape.

## Context

- **Day 37** introduced TTL leases at acquisition, scaled by severity tier, so resources self-expire instead of relying on a sweep.
- **Day 38** found that under burst spawns, all those renewals synchronized into a thundering herd against the lease store — fixed with per-lease jitter fixed at acquisition, a batching broker that coalesces renewals within a 250ms window, and a grace buffer to stop false-positive expiry kills.

Batching bought real headroom. But it has a ceiling: it reduces the *number* of round trips to the store, not the store's total throughput capacity. Once sub-agent counts grew further — testing against a simulated worst case of a full-catalog multi-event cascade, north of 150 concurrent sub-agents — the single store started showing sustained write contention even with everything batched. Batching had been treating the symptom; the store itself needed to scale horizontally.

## The Problem

- A single lease store, even with batched renewals, has a hard throughput ceiling once concurrent sub-agent count grows past a few hundred — every batch still lands on the same underlying store, and its write path doesn't parallelize past some point.
- The naive fix — just add more store instances and route by round robin — breaks lease semantics: a sub-agent's renewal has to land on the same instance that holds its lease, or the store can't tell if it's renewing or creating a duplicate.
- The first real sharding attempt used severity tier alone as the shard key, since that felt like the natural partition already used everywhere else in the system. It backfired immediately: critical-tier events spawn disproportionately more sub-agents than low-tier ones, so the "critical" shard became a hot shard carrying most of the load — sharding in name only.
- Rebalancing is its own trap. Resharding live leases mid-flight (say, splitting an overloaded shard in two) risks losing track of a lease during the move, which reopens exactly the false-positive-kill problem Day 38 just closed.

## Architecture

### 1. Composite shard key: severity tier + hashed sub-agent ID

Severity tier alone clustered load unevenly. The fix combines it with a hash of the sub-agent ID, so leases within a severity tier still spread across multiple shards instead of collapsing onto one.

```python
shard_id = hash(f"{severity_tier}:{sub_agent_id}") % shard_count
```

This keeps tier-aware locality where it's useful (a shard failure only affects a bounded mix of tiers) without letting any single tier dominate a shard's load.

### 2. A thin routing layer, not a smart one

Rather than teaching every sub-agent which shard it lives on, a routing layer sits in front of the shards and resolves `sub_agent_id -> shard_id` on lease acquisition, then hands that mapping back to the sub-agent to cache for its lifetime. Sub-agents never re-resolve mid-life; only new acquisitions hit the routing layer.

```python
shard_id = router.resolve(sub_agent_id)  # resolved once, at acquisition
lease = shard_store[shard_id].acquire(sub_agent_id, ttl)
```

Keeping resolution as a one-time, acquisition-time step (rather than something re-checked on every renewal) was a deliberate echo of the Day 38 lesson: anything recomputed per-cycle tends to reintroduce the exact synchronization problem you just fixed.

### 3. Per-shard batching, unchanged from Day 38

The Day 38 batching broker didn't need to change conceptually — it just now operates per shard instead of against one global store. Each shard gets its own renewal queue and its own 250ms coalescing window, so shards fail and recover independently of each other.

```python
for shard_id, due in renewal_queue.drain_by_shard(window_ms=250).items():
    shard_store[shard_id].renew_batch([r.lease_id for r in due])
```

## Failure Modes

- **Severity tier alone as shard key.** Covered above, but worth restating as the headline mistake: it looked like a natural partition because tier was already a first-class dimension elsewhere in the system, but it directly tracks load skew rather than counteracting it. Any shard key that correlates with load volume just relocates the hot shard instead of eliminating it.
- **Re-resolving shard routing on every renewal.** An early version had sub-agents call the router on every renewal cycle "just to be safe" in case of rebalancing. That reintroduced a coordination bottleneck at the router itself — right back to a single-point contention problem, just one layer removed. Caching the resolution at acquisition and only invalidating it on an explicit rebalance event fixed it.
- **Live resharding without a handoff window.** A first attempt at splitting an overloaded shard moved leases immediately and cut over routing atomically. Any renewal in flight during the cutover landed on the old shard and got rejected as unknown. Fixed by adding a brief dual-write handoff window where both old and new shard accept renewals for affected leases before the old shard is retired.
- **Under-sharding to avoid complexity.** There was a real temptation to pick a small, fixed shard count and call it sufficient, since more shards means more operational surface area. Went with a modest shard count sized to the tested worst case instead of a large speculative one — sharding for load you don't have yet just adds coordination overhead for no benefit.

## What's Next

Rebalancing is still manual right now — the shard count is fixed at deploy time. Day 40 is likely looking at whether shard count needs to flex dynamically as sustained load changes, or whether a fixed count sized generously enough is actually fine and the energy is better spent elsewhere.

*Orbital Watch: watching the sky, one resilient agent at a time.*
