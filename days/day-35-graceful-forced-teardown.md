# Day 35: Graceful vs. Forced Teardown for Sub-Agents

**TL;DR:** TTL watchdogs (Day 34) tell you *when* a sub-agent should die. They don't tell you *how* to kill it safely. Today I split teardown into two paths — graceful (drain, checkpoint, exit) and forced (hard kill, salvage what you can) — built the escalation logic that decides which one a given sub-agent gets, and instrumented the whole thing so I could actually measure how often each path fires and what it costs.

## The Problem

- A TTL watchdog firing mid-tool-call was silently dropping partial results — a sub-agent investigating a CME alert would get killed while waiting on a NOAA SWPC response, and that in-flight context just vanished.
- Some sub-agents were holding resources (open HTTP sessions to NASA NeoWs, in-memory diff buffers against the last known TLE set) that a hard kill left dangling until the next GC pass.
- Not all teardowns are equal: a low-severity monitoring sub-agent going stale is a different problem than a high-severity sub-agent that's hung and blocking the supervisor's fan-in.
- Treating every teardown as "just cancel the task" meant losing state that the supervisor actually needed for its final report — even when the sub-agent's *conclusion* was already 90% formed.
- There was no way to tell, after the fact, whether a given kill was "clean" or "messy" — teardown was a black box that either worked or didn't, with no visibility into which path fired or why.
- Retrospectively debugging a bad fan-in report meant grepping logs for the sub-agent's ID and hoping something useful got written before it died, rather than having a structured record of exactly how the teardown went.

## Architecture

### 1. Two teardown paths, one decision point

Every sub-agent now exits through a `teardown(agent, reason)` call that first asks whether graceful teardown is viable, and only escalates if it isn't.

```python
def teardown(agent: SubAgent, reason: TeardownReason) -> TeardownResult:
    if agent.is_gracefully_terminable(reason):
        result = graceful_teardown(agent, timeout=agent.grace_period)
        if result.completed:
            return result
        # grace period expired without a clean exit
    return forced_teardown(agent)
```

`is_gracefully_terminable` checks whether the agent is mid-tool-call on a cancellable operation, whether it holds locks the supervisor is waiting on, and whether its severity tier allows a grace period at all (P0 sub-agents get none).

```python
def is_gracefully_terminable(agent: SubAgent, reason: TeardownReason) -> bool:
    if agent.severity_tier == "P0":
        return False  # no grace period, ever
    if reason == TeardownReason.SUPERVISOR_BLOCKED:
        return False  # supervisor is stalled on this agent's fan-in slot
    if agent.current_operation and not agent.current_operation.cancellable:
        return False  # e.g. mid-write to a shared buffer
    return True
```

The `reason` matters as much as the agent's own state. A sub-agent going stale on its own TTL is a very different situation from a sub-agent the supervisor is actively blocked on — the latter always forces regardless of severity tier, because the cost of waiting compounds across the whole fan-in.

### 2. Graceful teardown: drain, checkpoint, exit

```python
def graceful_teardown(agent: SubAgent, timeout: float) -> TeardownResult:
    agent.stop_accepting_new_subtasks()
    partial = agent.flush_partial_conclusion()  # best-effort synthesis
    checkpoint_agent_state(agent.id, partial, status="drained")
    close_agent_resources(agent)  # HTTP sessions, buffers
    return TeardownResult(completed=True, salvaged=partial)
```

The key move is `flush_partial_conclusion()` — instead of discarding whatever the sub-agent had figured out, it forces a synthesis step against whatever evidence it's already gathered, and that partial conclusion gets checkpointed so the supervisor's fan-in can still use it, tagged as low-confidence.

`flush_partial_conclusion` is intentionally constrained — it's not allowed to call any tools, only to summarize what it already has in context:

```python
def flush_partial_conclusion(agent: SubAgent) -> PartialConclusion:
    evidence = agent.context.gathered_evidence
    if not evidence:
        return PartialConclusion(confidence=0.0, summary=None)
    synthesis_prompt = build_synthesis_only_prompt(evidence)
    result = agent.model.invoke(synthesis_prompt, tools=[])  # tools disabled
    return PartialConclusion(
        confidence=estimate_confidence(evidence, result),
        summary=result.text,
        evidence_count=len(evidence),
    )
```

Disabling tools during the flush was the fix for the timeout bug described below — without it, a sub-agent could decide it wants "just one more data point" before wrapping up, and that alone can blow the grace period.

### 3. Forced teardown: hard kill, salvage what you can

```python
def forced_teardown(agent: SubAgent) -> TeardownResult:
    last_checkpoint = get_last_checkpoint(agent.id)  # may be stale or None
    orphaned = force_close_agent_resources(agent)     # no cooperation from the agent
    agent.task.cancel()
    log_orphaned_resources(agent.id, orphaned)
    return TeardownResult(completed=True, salvaged=last_checkpoint, forced=True)
```

Forced teardown doesn't wait on the sub-agent's cooperation at all — it goes straight for whatever was last checkpointed (from a periodic autosave, not from the agent itself) and force-closes resources, logging anything that couldn't be cleanly released so it shows up in the next resource-leak sweep rather than silently accumulating.

`force_close_agent_resources` walks the agent's resource table rather than relying on the agent's own cleanup code (which we can't trust to run at all in a forced kill):

```python
def force_close_agent_resources(agent: SubAgent) -> list[str]:
    orphaned = []
    for resource in agent.resource_table.all():
        try:
            resource.close(timeout=0.1)
        except (TimeoutError, ConnectionError):
            orphaned.append(resource.id)
            resource.mark_leaked()
    return orphaned
```

### 4. Severity-aware grace periods

Grace periods aren't fixed — they scale inversely with the severity tier that spawned the sub-agent in the first place (Day 31), since a P0 sub-agent blocking the supervisor is worse than losing its partial state.

```python
GRACE_PERIODS = {
    "P0": 0.0,      # forced only — no grace period
    "P1": 2.0,
    "P2": 5.0,
    "P3": 10.0,
}
```

### 5. Spawn-time checkpointing

The fix for the "P0 killed within one tick" failure mode (below) was to move checkpointing to spawn time instead of teardown time, so there's always *something* to fall back on even for agents that never got far enough to produce a partial conclusion:

```python
def spawn_sub_agent(task: Subtask, severity: str) -> SubAgent:
    agent = SubAgent(task=task, severity_tier=severity)
    checkpoint_agent_state(
        agent.id,
        PartialConclusion(confidence=0.0, summary=None),
        status="spawned",
    )
    agent.start()
    return agent
```

### 6. Teardown telemetry

Every teardown now emits a structured record — path taken, elapsed time, salvage confidence, and orphaned resource count — so I can actually see the shape of this in aggregate instead of inferring it from scattered logs:

```python
def record_teardown(agent_id: str, result: TeardownResult, elapsed: float) -> None:
    emit_metric("teardown.path", "graceful" if not result.forced else "forced")
    emit_metric("teardown.elapsed_seconds", elapsed)
    emit_metric("teardown.salvage_confidence", result.salvaged.confidence if result.salvaged else 0.0)
    emit_metric("teardown.orphaned_resources", len(result.orphaned or []))
```

Over the first day of running this in the paper-trading pipeline, roughly 78% of teardowns resolved gracefully, 22% escalated to forced — and of the forced ones, about a third had zero salvageable state, which is exactly the population the spawn-time checkpoint fix was meant to shrink.

## Failure Modes

- **Checkpointing during graceful teardown itself timed out.** If `flush_partial_conclusion()` triggers another tool call (e.g., the agent wants one more data point before synthesizing), it can blow through the grace period on its own. Fixed by making the flush synthesis-only — no new tool calls allowed once teardown starts.
- **Forced teardown on a P0 that never got a checkpoint.** A sub-agent spawned and killed within the same tick had no autosave to fall back on, so the supervisor's fan-in got a hole in its evidence set instead of a labeled "no data" entry. Fixed by checkpointing immediately on spawn, before any work happens.
- **Resource leaks masquerading as slow drains.** An HTTP session held by a "gracefully" draining agent kept the connection pool from recycling because `close_agent_resources` was called after the timeout check instead of unconditionally in a `finally`. Moved it to a `finally` block so resources close regardless of how teardown exits.
- **Grace period races with the supervisor's own timeout.** If a P3 sub-agent's 10-second grace period is longer than the supervisor's own patience for that fan-in slot, the supervisor moves on and then the graceful teardown result arrives to a slot that no longer exists. Fixed by having the supervisor's fan-in timeout always be strictly greater than the longest possible grace period in play.
- **Double-teardown on retry logic.** Under load, the watchdog would sometimes fire a second `teardown()` call on an agent already mid-teardown, because the agent hadn't been marked as "tearing down" fast enough. Fixed with a status flag set synchronously the moment `teardown()` is entered, before any of the actual draining logic runs.
- **Confidence-tagging was too coarse.** Early on, every graceful salvage got the same confidence score regardless of how much evidence the sub-agent had actually gathered. A sub-agent killed one tool-call in looked identical, in the fan-in, to one killed after gathering most of what it needed. Fixed by scaling `estimate_confidence` with `evidence_count` and the fraction of the sub-agent's planned subtasks it had completed.

## What's Next

Day 36 will look at how the supervisor's fan-in step weights salvaged partial conclusions (graceful) differently from stale checkpoints (forced) when synthesizing the final report — right now they're treated identically, which is probably wrong given the confidence-tagging work above. I also want to dig into whether the 22% forced-teardown rate is actually a problem or just the expected cost of running P0/P1 sub-agents aggressively.

*Orbital Watch: a multi-agent LangGraph system watching the sky so you don't have to.*
