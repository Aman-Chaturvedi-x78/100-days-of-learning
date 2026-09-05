# Day 42: When Pre-emptive Splits Collide — Admission Control for Concurrent Shard Migrations

## TL;DR
Day 41 gave every shard its own forecast-gated pre-emptive split trigger. But a real solar event doesn't threaten one shard at a time — it threatens every shard near the same severity tier at once. When forecast confirmation fires broadly, each shard's rebalancer independently kicks off its own dual-write migration, and those migrations pile onto the same lease store and routing layer that Day 38 already learned saturates under synchronized load — except now it's migrations doing the hammering instead of heartbeats. Today: a global migration admission controller that queues and throttles concurrent splits, prioritized by forecast confidence and time-to-impact, with priority aging so low-tier shards don't starve.

## The Problem
- Day 41's rebalancer is per-shard: the in-progress check only stops a shard from double-splitting itself, not coordination across shards
- Real storms escalate broadly — several severity-tier shards receive forecast confirmation within the same alert window
- Each shard independently starts its dual-write migration, multiplying concurrent load right when real event load is also about to arrive
- No global view of how many migrations are safe to run at once relative to available capacity headroom
- A naive global lock (one migration at a time) just serializes everything and blows past Day 41's "finish before real load arrives" timing requirement

## Architecture

### 1. Migration admission controller
A central gatekeeper all rebalancer instances must request a "split slot" from before starting a migration, instead of acting unilaterally.

```python
class MigrationAdmissionController:
    def __init__(self, max_concurrent_migrations: int):
        self.budget = max_concurrent_migrations
        self.active = set()
        self.pending = []  # priority queue

    async def request_slot(self, shard_id, priority_score):
        if len(self.active) < self.budget:
            self.active.add(shard_id)
            return True
        heapq.heappush(self.pending, (-priority_score, time.time(), shard_id))
        return False
```

### 2. Priority scoring
Combines forecast confidence and time-to-impact so imminent, high-confidence splits jump the queue ahead of speculative or distant ones.

```python
def priority_score(confidence: float, minutes_to_impact: float) -> float:
    urgency = 1.0 / max(minutes_to_impact, 1.0)
    return (0.6 * confidence) + (0.4 * urgency)
```

### 3. Concurrency budget
A small token bucket caps how many migrations run at once, sized to what the lease store can absorb without repeating Day 38's storm.

```python
def release_slot(self, shard_id):
    self.active.discard(shard_id)
    if self.pending:
        _, _, next_shard = heapq.heappop(self.pending)
        self.active.add(next_shard)
        self._notify(next_shard)
```

### 4. Priority aging
Waiting requests gain priority the longer they sit in queue, so a prolonged storm doesn't starve lower-severity shards indefinitely.

```python
def age_pending(self):
    now = time.time()
    self.pending = [
        (score - 0.01 * (now - queued_at), queued_at, sid)
        for score, queued_at, sid in self.pending
    ]
    heapq.heapify(self.pending)
```

## Failure Modes
- **Thundering herd of migrations**: letting every confirmed split proceed immediately saturates the lease store the same way Day 38's synchronized heartbeats did — just one layer up, at the migration layer instead of the renewal layer.
- **Priority starvation**: without aging, a steady stream of high-confidence splits during a prolonged storm can leave low-severity shards waiting indefinitely.
- **Priority inversion near deadline**: a low-priority split whose time-to-impact clock is about to run out needs to preempt fresher high-priority requests, or it finishes after real load has already hit it — Day 41's "finish before real load arrives" constraint, now enforced at fleet scale instead of per-shard.

## What's Next
Extending admission control to account for cross-region capacity once Orbital Watch federates across regions — a single global budget won't hold once migrations are competing for bandwidth across geographically separate lease stores.

*Orbital Watch: watching the sky so the sky doesn't watch us.*
