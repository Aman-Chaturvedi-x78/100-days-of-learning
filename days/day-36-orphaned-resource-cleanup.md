# Day 36: Orphaned Resource Cleanup — What Teardown Leaves Behind

**TL;DR:** Graceful and forced teardown (Day 35) kill the sub-agent *process*, but not necessarily what it was holding — open DB connections, distributed locks, rate-limit budget, half-written checkpoint files. Today: a resource ledger + reconciliation sweep that finds and reclaims anything a dead sub-agent left behind, without racing the sub-agent's own cleanup.

## The Problem

- Forced teardown (the hard-kill-and-salvage path from Day 35) skips a sub-agent's normal `finally` blocks — so anything it acquired mid-run just stays acquired.
- A CelesTrak-polling sub-agent holding a connection-pool slot that's never returned means the pool slowly starves under repeated forced kills during a busy severity-tier spawn burst.
- Distributed locks (used so two sub-agents don't double-process the same NOAA SWPC alert) can outlive the agent that took them, silently blocking every future agent from touching that alert.
- Rate-limit budget borrowed from the shared NASA NeoWs quota doesn't get returned, so the system thinks it has less headroom than it actually does — throttling requests it didn't need to throttle.
- Half-written checkpoint files from an interrupted drain/checkpoint/exit path linger on disk, slowly eating the volume they're written to.
- None of this shows up as an error. It shows up two hours later as pool exhaustion or a permanently stuck alert, with no obvious link back to a teardown event.
- The first time this bit us in practice: a solar-storm-severity spawn burst forced-killed a batch of sub-agents in quick succession, and twenty minutes later the connection pool for NOAA SWPC was reporting zero available slots — with every log line pointing at *new* requests, none of them pointing back at the kills that actually caused it. Tracing that back was the whole reason this system exists now.

## Architecture

**1. Resource ledger — register on acquire, not on release**

Every sub-agent logs *intent to acquire* before it actually acquires, tagged with its own agent ID. This is deliberately eager: the ledger entry exists before the acquire call even resolves, so a crash mid-acquisition still leaves a trace.

```python
async def acquire_resource(agent_id: str, kind: str, resource_id: str):
    await ledger.register(agent_id, kind, resource_id, state="pending")
    handle = await pool.acquire(resource_id)
    await ledger.mark(agent_id, resource_id, state="held")
    return handle
```

Registering before the acquire succeeds means a crash *during* acquisition still leaves a traceable entry, instead of a resource that's held but invisible. The ledger itself is just a small table keyed on `(agent_id, kind, resource_id)` with a `state` column (`pending` / `held` / `released`) and a `last_seen` timestamp, so it's cheap to write to on every acquire and release.

**2. Reconciliation sweep**

A background task runs every few seconds, diffing the ledger against the live agent registry:

```python
async def reconcile():
    live = await agent_registry.live_ids()
    for entry in await ledger.all_held():
        if entry.agent_id not in live:
            await reclaim(entry)
```

The sweep interval isn't fixed — it tightens automatically when the supervisor is in a high-severity spawn burst (same severity tiers introduced on Day 31), since that's exactly when forced kills, and therefore leaks, cluster.

**3. Per-resource-type reclamation**

Each resource kind gets its own reclaim strategy — force-close for pool connections, explicit unlock for distributed locks, budget credit-back for rate limits, delete for temp checkpoint files:

```python
RECLAIM_STRATEGIES = {
    "db_connection": lambda r: pool.force_close(r.resource_id),
    "distributed_lock": lambda r: lock_manager.unlock(r.resource_id),
    "rate_limit_budget": lambda r: quota.credit_back(r.resource_id),
    "checkpoint_file": lambda r: os.remove(r.resource_id),
}

async def reclaim(entry):
    strategy = RECLAIM_STRATEGIES[entry.kind]
    await strategy(entry)
    await ledger.mark(entry.agent_id, entry.resource_id, state="released")
```

Keeping the strategies in a lookup table rather than a chain of `if/elif` made it trivial to add checkpoint-file cleanup after the fact, once that leak type showed up in production.

**4. Idempotent reclamation**

Graceful teardown's own cleanup and the reconciliation sweep can race — an agent finishes releasing a resource right as the sweep decides it's orphaned. Reclaim operations are written to no-op safely on an already-released resource rather than erroring, by checking the ledger state immediately before acting:

```python
async def reclaim(entry):
    current = await ledger.get(entry.agent_id, entry.resource_id)
    if current.state == "released":
        return  # already cleaned up, nothing to do
    strategy = RECLAIM_STRATEGIES[entry.kind]
    await strategy(entry)
    await ledger.mark(entry.agent_id, entry.resource_id, state="released")
```

**5. Leak metrics feeding back into teardown policy**

Every reclamation increments a counter tagged by teardown type (graceful vs. forced) and resource kind. A rising forced-teardown leak rate is a signal to tighten Day 34's severity-tiered grace periods rather than just reclaiming faster — the ledger is a safety net, not a substitute for cleaner shutdowns.

## Failure Modes

- **Sweep interval too coarse:** a wide reconciliation interval combined with a burst of forced kills during a severe space-weather event let the connection pool starve before the next sweep ran. Fixed by tightening the interval specifically during high-severity spawn bursts.
- **Double-release race:** an agent's own graceful cleanup and the reconciliation sweep both tried to release the same lock within milliseconds of each other. The second call threw before reclamation was made idempotent.
- **Phantom leaks from crash-before-register:** a sub-agent that crashed between deciding to acquire a resource and calling `acquire_resource` left a resource held with no ledger entry at all — invisible to the sweep. Fixed by moving the "pending" registration earlier, before any actual I/O.
- **Locks with no TTL:** an early version of the lock reclaim path assumed the sweep would always catch orphaned locks. It didn't account for the reconciliation task itself being down — so every distributed lock now also carries its own TTL as a fallback, independent of the sweep.
- **Reclaim strategy exceptions killing the whole sweep:** an early version of `reconcile()` let one failed reclaim (a pool that was already closed for unrelated reasons) throw and abort the entire sweep loop, leaving every other orphaned resource that cycle untouched. Fixed by wrapping each reclaim call individually so one failure doesn't block the rest of the batch.

## What's Next

Next up: tying this resource ledger into the agent memory/state persistence layer, so a sub-agent restarted from a checkpoint doesn't just resume its task — it resumes knowing exactly what it still owns.

*Building Orbital Watch, one failure mode at a time.*
