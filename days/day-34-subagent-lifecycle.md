# Day 34: Sub-Agent Lifecycle Management in Orbital Watch

## TL;DR

Dynamic agent topology (Day 31) solved the wrong-shaped-pipeline problem, but it created a new one: nothing was killing sub-agents once their work was done. Severity-tiered sub-agents spawned faster than they resolved, orphaned agents kept holding LangGraph checkpoints, and memory usage climbed steadily over multi-hour runs. This post covers the lifecycle state machine, TTL-based reaping, and graceful-vs-forced teardown I added to fix it.

## The Problem

- Sub-agents spawned by the supervisor (Day 31) had no defined end state — they either finished their task and returned, or silently hung
- Hung sub-agents kept their LangGraph checkpoint entries alive indefinitely, growing the state store on every run
- No distinction between "sub-agent finished but hasn't reported back" and "sub-agent is dead" — the supervisor couldn't tell the difference
- Under bursty CelesTrak conjunction alerts, high-severity sub-agents spawned faster than low-priority ones drained, causing a backlog that looked like a leak but was actually starvation
- Teardown had no handoff step, so partial analysis state (e.g. a threat-tier sub-agent halfway through a trajectory recheck) was just discarded on kill

## Architecture

### 1. Explicit lifecycle states

Every sub-agent now carries a state enum instead of implicit "running or not":

```python
class SubAgentState(Enum):
    SPAWNED = "spawned"
    ACTIVE = "active"
    REPORTING = "reporting"
    COMPLETE = "complete"
    ORPHANED = "orphaned"
    REAPED = "reaped"
```

### 2. Heartbeat + TTL watchdog

Each sub-agent writes a heartbeat timestamp on every LangGraph node transition. A watchdog task, running alongside the supervisor, scans for stale heartbeats:

```python
async def watchdog_sweep(registry, ttl_seconds=90):
    now = time.monotonic()
    for agent_id, meta in registry.items():
        if meta.state == SubAgentState.ACTIVE and now - meta.last_heartbeat > ttl_seconds:
            meta.state = SubAgentState.ORPHANED
            await reap(agent_id, graceful=True)
```

### 3. Graceful vs. forced teardown

Graceful teardown gives an orphaned agent one final window to flush partial state back to the supervisor before it's removed. Forced teardown skips that window — used only when the checkpoint store itself is unreachable:

```python
async def reap(agent_id, graceful=True):
    if graceful:
        partial = await try_flush_state(agent_id, timeout=5)
        if partial:
            supervisor.merge_partial(agent_id, partial)
    checkpoint_store.delete(agent_id)
    registry[agent_id].state = SubAgentState.REAPED
```

### 4. Severity-aware spawn throttling

To stop high-severity spawn bursts from starving low-priority sub-agents, the supervisor now caps concurrent sub-agents per tier and queues overflow instead of spawning unbounded:

```python
MAX_CONCURRENT = {"critical": 8, "elevated": 4, "routine": 2}
```

## Failure Modes

- **Watchdog false positives on slow trajectory recomputation** — a legitimate high-precision orbit recheck can take longer than the default 90s TTL, and the watchdog was reaping agents that were still working, not stuck. Fixed by making TTL tier-dependent instead of a single global constant.
- **Partial-state merge race** — if the watchdog reaps an agent at the same instant it finishes normally, both the graceful flush and the normal completion path tried to write to the supervisor's state, corrupting the merge. Added a compare-and-swap on agent state before either path is allowed to write.
- **Checkpoint store growth under forced teardown** — forced reaps skip the flush, but I hadn't made them skip the checkpoint delete too under a specific error path, so checkpoints briefly doubled up before being cleaned by a separate GC pass. Fixed by moving the delete into the same transaction as the state flip.

## What's Next

Day 35 will likely look at cross-agent consensus — what happens when two severity-tiered sub-agents that survived this lifecycle rework disagree on a threat assessment for the same object.

*Orbital Watch: watching the sky so the on-call human doesn't have to.*
