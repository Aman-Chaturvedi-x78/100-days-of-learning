# Day 32: Circuit Breakers for Runaway Agent Spawning

**TL;DR:** Dynamic topology (Day 31) let the supervisor spawn severity-tiered sub-agents at runtime instead of running a fixed pipeline. That's great — until a sub-agent's own severity assessment triggers *another* spawn, which triggers another, and a single ambiguous NEO close-approach alert turns into a 40-agent fan-out that burns the session's token budget in three minutes. Today I built a circuit breaker layer that caps spawn depth, tracks a live cost ledger per root event, and trips into a degraded "summarize-only" mode before the system self-DoSes.

## The Problem

- Severity-tiered spawning (Day 31) has no ceiling — a sub-agent flagging "escalate" can itself spawn children, with no structural limit on recursion depth
- A noisy signal (e.g. conflicting NOAA/CelesTrak state vectors for the same object) can trigger repeated re-escalation loops between sibling agents
- Token budget management (earlier in the series) tracked cost per *task*, not per *event chain* — so a recursive spawn storm looked like many small compliant tasks, not one runaway chain
- No circuit breaker meant the only kill switch was a human noticing the bill or the timeout

## Architecture

### 1. Spawn ledger keyed by root event

Every spawn call now writes to a ledger keyed by the originating event ID, not the immediate parent — so cost and depth are tracked against the whole chain, not each hop.

```python
class SpawnLedger:
    def __init__(self, max_depth=3, max_agents_per_root=8, token_ceiling=50_000):
        self.chains = {}  # root_event_id -> ChainState
        self.max_depth = max_depth
        self.max_agents_per_root = max_agents_per_root
        self.token_ceiling = token_ceiling

    def can_spawn(self, root_event_id, depth):
        chain = self.chains.setdefault(root_event_id, ChainState())
        if depth > self.max_depth:
            return False, "depth_exceeded"
        if chain.agent_count >= self.max_agents_per_root:
            return False, "fanout_exceeded"
        if chain.tokens_used >= self.token_ceiling:
            return False, "budget_exceeded"
        return True, None
```

### 2. Trip condition and degraded mode

When `can_spawn` returns `False`, the supervisor doesn't just block the one spawn — it trips the breaker for the whole root event and switches remaining in-flight agents to a cheap "summarize what you have, don't re-escalate" mode.

```python
def on_spawn_denied(root_event_id, reason):
    ledger.chains[root_event_id].tripped = True
    ledger.chains[root_event_id].trip_reason = reason
    broadcast_to_chain(root_event_id, mode="summarize_only")
    log_breaker_trip(root_event_id, reason)
```

### 3. Half-open recovery

Rather than a permanent kill, tripped chains go "half-open" after a cooldown: one probe agent is allowed to re-assess severity with a hard token cap, and only if it independently confirms escalation does the breaker reset.

```python
def probe_reset(root_event_id):
    if not ledger.chains[root_event_id].tripped:
        return
    if time_since_trip(root_event_id) < COOLDOWN_SECONDS:
        return
    result = run_probe_agent(root_event_id, token_cap=2_000)
    if result.confirms_escalation:
        ledger.chains[root_event_id].reset(keep_history=True)
```

## Failure Modes

- **Ledger keyed on the wrong ID silently defeats the breaker.** First pass keyed spawns by immediate parent agent, not root event — a 3-deep chain looked like three independent 1-deep chains and never tripped. Root-event keying was the actual fix, not deeper recursion checks.
- **Degraded mode without a "why" note confused downstream consumers.** Summarize-only outputs looked identical to normal low-severity outputs on the dashboard until a `breaker_tripped: true` flag was added to the response schema.
- **Probe agent itself needs a spawn cap, or the half-open state can retrip immediately.** First version let the probe agent spawn normally; a genuinely severe event just re-tripped the breaker one step later. Capping the probe to a fixed token budget with no further spawning permission fixed this.
- **Cooldown too short re-tripped on transient data noise.** Tuned from 30s to 5 minutes after conflicting NOAA/CelesTrak vectors kept the chain oscillating between tripped/reset every probe cycle.

## What's Next

Day 33: now that chains can trip into degraded mode, the dashboard needs to surface *why* a chain stopped escalating — provenance for the breaker decision itself, not just the underlying anomaly.

*Orbital Watch: a multi-agent system learning to watch the sky without burning down the budget doing it.*
