# Day 31: Dynamic Agent Topology — Letting Severity Decide the Graph

## TL;DR
Orbital Watch's LangGraph pipeline has been a fixed graph since Day 1: same nodes, same fan-out, every run — whether the Isolation Forest flags a borderline drift or a five-sigma solar wind spike. Today I ripped out the static topology and replaced it with a supervisor that builds the graph *at runtime*, spawning only the sub-agents a given anomaly's severity actually warrants.

## The Problem
- The static graph ran the full pipeline (trajectory re-check, conjunction assessment, human escalation prep) on every anomaly, including ones the Isolation Forest scored as barely-above-threshold.
- Low-severity anomalies were burning the same token budget as genuine five-sigma events — no proportionality between signal strength and compute spent.
- Adding a new specialist agent (e.g. a debris-cloud correlation checker) meant editing the shared graph definition, so the blast radius of every change touched unrelated anomaly paths.
- There was no way to short-circuit early: even a "this is almost certainly sensor noise" verdict from the first node still walked the rest of the fixed pipeline.

## Architecture

### 1. Severity-tiered supervisor node
The supervisor is now the only fixed node. It takes the Isolation Forest anomaly score (from Day 30's drift-triggered retraining) plus a rolling confidence estimate and buckets the event into one of three tiers before anything else runs.

```python
def classify_severity(anomaly_score: float, confidence: float) -> str:
    if anomaly_score > 0.85 and confidence > 0.7:
        return "critical"
    elif anomaly_score > 0.55:
        return "elevated"
    return "routine"
```

### 2. Runtime subgraph assembly
Instead of a pre-wired `StateGraph`, the supervisor builds the node set for *this run only*, using a registry of available specialist agents keyed by tier.

```python
SPECIALIST_REGISTRY = {
    "critical": ["trajectory_reassessment", "conjunction_assessment", "human_escalation"],
    "elevated": ["trajectory_reassessment", "conjunction_assessment"],
    "routine": ["trajectory_reassessment"],
}

def build_subgraph(tier: str) -> StateGraph:
    graph = StateGraph(OrbitalState)
    for agent_name in SPECIALIST_REGISTRY[tier]:
        graph.add_node(agent_name, SPECIALIST_FNS[agent_name])
    graph.add_edge(START, SPECIALIST_REGISTRY[tier][0])
    for a, b in zip(SPECIALIST_REGISTRY[tier], SPECIALIST_REGISTRY[tier][1:]):
        graph.add_edge(a, b)
    graph.add_edge(SPECIALIST_REGISTRY[tier][-1], END)
    return graph.compile()
```

### 3. Spawn budget guard
Runtime graph construction means runtime cost is no longer statically knowable, so every spawn is checked against a per-incident token/agent-count budget before it's allowed to run.

```python
def spawn_with_budget(tier: str, incident_id: str, budget_tracker: BudgetTracker):
    projected_cost = estimate_cost(SPECIALIST_REGISTRY[tier])
    if not budget_tracker.can_spend(incident_id, projected_cost):
        return fallback_to_routine_tier(incident_id)
    subgraph = build_subgraph(tier)
    return subgraph.invoke({"incident_id": incident_id})
```

## Failure Modes
- **Unbounded spawn chains under score flapping**: an anomaly score oscillating near the 0.85 boundary caused the same incident to be reclassified critical → elevated → critical across consecutive polling cycles, respawning the full specialist set each time. Fixed with a hysteresis band and a per-incident cooldown before a tier can escalate again.
- **Shared-state race conditions between specialists**: `trajectory_reassessment` and `conjunction_assessment` both write to `OrbitalState.risk_estimate` when run concurrently in the critical tier, and the second write was silently clobbering the first.
  ```python
  # before: both agents write directly
  state["risk_estimate"] = new_value  # last writer wins, no merge

  # after: namespaced writes, merged by the supervisor
  state["risk_estimates"][agent_name] = new_value
  ```
- **Supervisor became an untraceable single point of failure**: because the graph shape now depends on a runtime decision, replaying an incident from logs required reconstructing *which* graph ran, not just what the fixed graph did — regular LangSmith traces weren't enough without also logging the tier decision and registry snapshot at spawn time.
- **Debug difficulty from non-deterministic topology**: two incidents with visually similar anomaly scores could produce different graphs if confidence differed, which made "why did this incident get more scrutiny than that one" a much harder support question than it used to be.

## What's Next
Day 32 will likely dig into cost governance for the spawn budget itself — right now `estimate_cost` is a static lookup table per specialist, and it should really be learned from historical run costs instead of hand-tuned.

*Orbital Watch: watching the sky so the on-call engineer doesn't have to.*

---

# LinkedIn Post

today I learned that a fixed pipeline is the wrong default for anomaly response

**TL;DR:**
**The gap:** Orbital Watch ran the same full agent pipeline for every anomaly, whether the Isolation Forest score was barely over threshold or a genuine five-sigma event — no proportionality between signal and spend.
**The fix:** replaced the static LangGraph with a supervisor that classifies severity first, then assembles a subgraph at runtime from a specialist registry — routine anomalies get one agent, critical ones get the full trajectory/conjunction/escalation chain.
**The unlock:** compute now scales with actual risk instead of worst-case-every-time, and adding a new specialist agent no longer means editing a shared graph that every anomaly path runs through.

**Build note:** this builds directly on Day 30's drift-triggered retraining — the anomaly score that used to just flag "retrain the model" now also decides how much of the agent graph gets spun up.

Biggest surprise: the hardest part wasn't the dynamic graph construction, it was debugging it afterward — when the graph shape itself is a runtime decision, you have to log *which* graph ran, not just what it did.

Question for anyone running multi-agent systems: how are you handling replay/debugging when your topology isn't fixed?

#AIAgents #MultiAgentSystems #LangGraph #LangChain #MachineLearning #AgenticAI #MLOps #SpaceSituationalAwareness #BuildInPublic #100DaysOfLearning #SoftwareEngineering #AIEngineering #SystemDesign #Python
