---
date: 2026-08-28
day: 33
title: "Provenance for Breaker Decisions – Why Did the Chain Stop?"
tags: [circuit-breaker, provenance, observability, multi-agent, orbital-watch]
---

TL;DR
- **Problem**: Day 32's circuit breaker tripped silently – dashboard only showed `breaker_tripped: true` without explaining *which* limit was hit (depth, fan‑out, budget, or cooldown).
- **Solution**: Built a provenance layer that logs every `can_spawn()` decision with timestamp, agent ID, reason, and context.
- **Outcome**: Dashboard now shows a decision timeline with colour‑coded nodes and links to raw logs; automated alerts flag oscillation or low‑token depth hits.
- **Key lessons**: Root‑event keying is essential; decision logs can grow fast – rotate older entries; half‑open probes must be in the same decision stream for chronological sorting.

---

## The Problem

- The circuit breaker (Day 32) tripped silently — operators saw "summarize-only" outputs but had no way to distinguish between:
  - **Budget exhaustion:** legitimate heavy analysis hitting the 50k token cap
  - **Depth limit:** a deep recursion chain that genuinely needed more layers
  - **Fan‑out explosion:** noisy sibling agents fighting over the same object
  - **Cooldown retrip:** the probe agent re‑tripping on the same noisy data
- Without provenance, the only debugging option was to replay the entire event chain from logs — expensive and slow.
- The ledger stored *aggregates* (total agents, total tokens) but not *decisions* — so there was no way to ask "why did `can_spawn()` return False for agent‑42 at 14:23:05?"

---

## Architecture

### 1. Decision Logging in the Spawn Ledger

Every `can_spawn()` call now writes a structured decision record, not just a boolean.

```python
class SpawnDecision:
    def __init__(self, agent_id, root_event_id, depth, reason, context):
        self.timestamp = datetime.utcnow().isoformat()
        self.agent_id = agent_id
        self.root_event_id = root_event_id
        self.depth = depth
        self.reason = reason  # "depth_exceeded" | "fanout_exceeded" | "budget_exceeded" | "allowed"
        self.context = context  # e.g., {"current_agents": 7, "max_agents": 8, "tokens_used": 48000}

class SpawnLedger:
    def __init__(self, ...):
        self.chains = {}
        self.decisions = []  # flat list for easy querying

    def can_spawn(self, root_event_id, agent_id, depth):
        chain = self.chains.setdefault(root_event_id, ChainState())
        allowed, reason = self._check_limits(chain, depth)
        decision = SpawnDecision(agent_id, root_event_id, depth, reason, self._snapshot(chain))
        self.decisions.append(decision)
        if not allowed:
            chain.tripped = True
            chain.trip_reason = reason
            chain.trip_decision_id = decision.id
        return allowed, reason
2. Provenance API for the Dashboard
A new query endpoint aggregates decisions by root event, so the dashboard can show a timeline:

python
def get_chain_provenance(root_event_id):
    chain = ledger.chains.get(root_event_id)
    if not chain:
        return None
    decisions = [d for d in ledger.decisions if d.root_event_id == root_event_id]
    return {
        "root_event_id": root_event_id,
        "trip_reason": chain.trip_reason,
        "trip_decision_id": chain.trip_decision_id,
        "total_agents": chain.agent_count,
        "total_tokens": chain.tokens_used,
        "timeline": [
            {
                "timestamp": d.timestamp,
                "agent_id": d.agent_id,
                "depth": d.depth,
                "decision": d.reason,
                "context": d.context,
            }
            for d in sorted(decisions, key=lambda x: x.timestamp)
        ],
        "half_open_history": chain.probe_attempts,  # from Day 32
    }
3. Visualizing the Breaker Trip
The dashboard now renders a decision tree:

Green nodes: allowed spawns

Red nodes: denied spawns (with reason)

Orange nodes: probe attempts during half‑open state

Grey nodes: degraded "summarize‑only" mode

Each node links to the raw decision context (token count, depth, sibling agent count) so operators can click through to the exact log line.

4. Automated Alerting on Trip Patterns
The provenance layer also feeds a lightweight rule engine:

python
def evaluate_trip_patterns(root_event_id):
    provenance = get_chain_provenance(root_event_id)
    if not provenance:
        return
    # Alert if the same chain trips more than 3 times in 1 hour
    if len(provenance["half_open_history"]) >= 3:
        alert_ops("Chain oscillating – possible noisy data loop", root_event_id)
    # Alert if a depth limit is hit with low token usage (suggests recursive logic bug)
    if provenance["trip_reason"] == "depth_exceeded" and provenance["total_tokens"] < 5000:
        alert_dev("Depth limit hit with low token spend – check for infinite recursion", root_event_id)
Failure Modes
Storing decisions as a flat list grew faster than expected. After 24 hours of test traffic, the decision list hit 10k entries — fine for a single chain, but not for production. Fixed by rotating decisions older than 7 days into cold storage (S3) and keeping only the last 100 per root event in the hot ledger.

Linking decisions to the wrong root event caused misleading provenance. First pass used the immediate parent's event ID, not the root — so a deep chain's decisions were scattered across multiple parent IDs, and the dashboard showed incomplete timelines. Root‑event keying (from Day 32) was the fix; this just extended it to decisions.

The provenance API returned too much data for long‑running chains. A chain with 40 agents and 40 decisions returned a 200KB JSON payload, slowing the dashboard. Added pagination and a ?fields=summary parameter to fetch only trip reason and total counts.

Half‑open probe attempts were logged separately from the main decision log. The first version stored probe attempts in a separate list, so the timeline wasn't sorted chronologically without extra joins. Moved probe attempts into the same decision stream with a decision: "probe_allowed" | "probe_denied" reason.

What's Next
Day 34: with provenance in place, the next step is to build a retrospective view — after a chain trips and recovers, can the system generate a post‑mortem report that explains why the breaker tripped, what changed during the cooldown, and whether the probe's re‑assessment was accurate? Essentially, a self‑documenting audit trail for every escalation decision.

Links & Resources
Day 32: Circuit Breakers for Runaway Agent Spawning – previous day's implementation

Martin Fowler – Circuit Breaker pattern

Observability for Microservices – Distributed Tracing concepts
