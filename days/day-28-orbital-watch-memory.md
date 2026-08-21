# Giving Orbital Watch a Memory

## TL;DR
Every Orbital Watch run used to start from zero — the Orchestrator/Researcher/Critic/Writer pipeline had no idea what it flagged yesterday, last week, or five minutes ago in a different thread. Added a two-tier memory layer (thread-scoped checkpoints + cross-thread long-term store) so the system can tell the difference between "new NEO close approach" and "same one already flagged," and between "solar storm just started" and "solar storm just escalated from G1 to G3."

## The Problem

Orbital Watch ingests NASA NeoWs, NOAA SWPC, and CelesTrak on a recurring cycle. Without memory, every run treats every data point as brand new:

- A NEO flagged as a close-approach risk gets re-flagged, re-researched, and re-written into an alert every single run — pure noise, and it burns the token budget the orchestrator works hard to conserve for nothing.
- A NOAA SWPC geomagnetic storm doesn't have a "before" to compare against, so the Critic agent can't distinguish escalation (G1 → G3, genuinely alert-worthy) from a flat continuation (still G1, not news).
- Multi-day patterns — an object's approach trajectory tightening over a week — are invisible if each run only sees a single snapshot.

Statelessness was fine for proving out fan-out/fan-in. It's not fine for a system meant to actually watch something over time.

## Architecture: Two Tiers, Not One

**Tier 1 — Thread-scoped checkpointing.** LangGraph's built-in checkpointer (running it against SQLite for now, Postgres is the eventual target) persists the graph state within a single run/thread — retry state, in-flight budget reservations, partial fan-out results. This is what already made the bounded-retry logic durable across interruptions. Nothing new here, just making sure it's backing every node, not just the ones under active debugging.

```python
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver.from_conn_string("orbital_watch.db")

graph = builder.compile(checkpointer=checkpointer)

# every invocation is pinned to a thread_id so retries/resumes
# reattach to the same in-flight state instead of starting cold
config = {"configurable": {"thread_id": "cycle-2026-08-21T00:00"}}
result = graph.invoke(input_state, config=config)
```

**Tier 2 — Cross-thread long-term store.** This is the actual new piece. LangGraph's `Store` interface gives me a namespaced key-value store that survives across independent runs. I namespace by entity, not by run:

```python
# Namespace: ("neo", designation) or ("storm", event_id)
store.put(
    namespace=("neo", "2024-XY3"),
    key="last_assessment",
    value={
        "risk_level": "moderate",
        "miss_distance_km": 4_200_000,
        "assessed_at": "2026-08-20T00:00:00Z",
        "summary": "..."  # Writer agent's prior output, compressed
    }
)
```

Before the Researcher agent does fresh work on an object, it checks the store first:

```python
def researcher_node(state: GraphState, store: BaseStore) -> GraphState:
    designation = state["neo_designation"]
    namespace = ("neo", designation)

    prior = store.get(namespace, "last_assessment")

    if prior and is_fresh(prior.value["assessed_at"], max_age_hours=24):
        # skip the fresh NeoWs lookup entirely, diff against what we have
        state["prior_assessment"] = prior.value
        state["skip_reason"] = "cache_hit_fresh"
        return state

    # stale or missing — do the real work
    fresh_data = fetch_neows(designation)
    state["fresh_data"] = fresh_data
    return state
```

The Critic's job changed from "is this concerning in isolation" to "is this concerning relative to what we already know":

```python
def critic_node(state: GraphState, store: BaseStore) -> GraphState:
    prior = state.get("prior_assessment")
    current = state["current_assessment"]

    if prior is None:
        state["verdict"] = "new" if current["risk_level"] != "negligible" else "no_alert"
    elif prior["risk_level"] != current["risk_level"]:
        state["verdict"] = "escalation" if _rank(current) > _rank(prior) else "de-escalation"
    else:
        state["verdict"] = "no_change"  # suppress alert, still write updated timestamp

    return state
```

That escalation/no-change split is the actual unlock. Diffing against memory is what turns a monitoring system from a scanner into a watcher.

## Failure Modes (the reason this took longer than expected)

- **Staleness risk.** If the store says "moderate risk, assessed 3 days ago" and the agent trusts it too far, it can under-react to something that's since escalated. Every stored assessment carries a freshness field, and anything past threshold forces a re-assessment instead of a cache read:

```python
def is_fresh(assessed_at: str, max_age_hours: int) -> bool:
    assessed = datetime.fromisoformat(assessed_at)
    return (datetime.utcnow() - assessed) < timedelta(hours=max_age_hours)
```

- **Unbounded growth.** Raw per-run writes for every object CelesTrak has ever mentioned would blow past reasonable storage and, worse, reasonable context size once memory gets pulled back into a prompt. Keeping the last N raw assessments per entity and summarizing anything older into a single rolling summary:

```python
def append_and_compact(store: BaseStore, namespace, new_entry, keep_raw=5):
    history = store.get(namespace, "history")
    entries = (history.value if history else []) + [new_entry]

    if len(entries) > keep_raw:
        to_summarize, keep = entries[:-keep_raw], entries[-keep_raw:]
        rolled_up = summarize_with_llm(to_summarize)  # cheap model, short prompt
        entries = [{"type": "summary", "content": rolled_up}] + keep

    store.put(namespace, "history", entries)
```

- **Memory poisoning.** Stored memory gets read back into agent context on future runs — which means anything that made it into the store unsanitized becomes a second-order injection vector, arguably worse than a live one because it's trusted as "our own prior conclusion." Every write now goes through the same sanitization pass as external feed ingestion, not just reads from NASA/NOAA/CelesTrak:

```python
def safe_store_write(store: BaseStore, namespace, key, value: dict):
    sanitized = {
        k: sanitize_for_injection(v) if isinstance(v, str) else v
        for k, v in value.items()
    }
    store.put(namespace, key, sanitized)
```

- **Concurrent write races.** Fan-out means multiple branches can finish and want to write to the same entity's memory key at once (two branches both researching the same NEO because it showed up in two different query windows). Last-write-wins with a version check for now:

```python
def versioned_write(store: BaseStore, namespace, key, value: dict):
    existing = store.get(namespace, key)
    incoming_version = value.get("source_run_ts")

    if existing and existing.value.get("source_run_ts", "") > incoming_version:
        return  # a newer write already landed, drop this one
    store.put(namespace, key, value)
```

  A proper CRDT-style merge is overkill at this scale — noting it as a "revisit if this becomes real" item rather than building it now.

## What's Next

Memory is in but read-only from the Critic/Writer side right now — next step is letting Researcher use it to skip redundant API calls entirely (not just skip redundant conclusions), which should meaningfully cut the NeoWs/SWPC call volume on repeat cycles.

---
*Building Orbital Watch, a space situational awareness agent, in public.*
