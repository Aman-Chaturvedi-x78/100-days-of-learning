---
date: 2026-08-14
day: 22
title: "Fan-Out/Fan-In: Parallel Agent Execution in LangGraph"
tags: [agents, langgraph, parallelism, orchestration, performance]
---

TL;DR
- Sequential agent calls waste latency and money when steps don't actually depend on each other.
- **Fan-out/fan-in** lets independent agents run concurrently, then merges their outputs before the next stage — same LangGraph state machine, just wired differently.
- The mechanism is LangGraph's **`Send` API** for dispatch plus a **reducer function** on shared state keys for merge — no new framework, just a different edge topology.
- The real risk isn't the parallelism itself, it's that **retry caps and concurrency limits from Day 19 now multiply across branches** instead of applying once.

---

## 1. Where This Fits

Stack so far:

```
LangGraph agent (7) → Tool calling (10) → Memory (11) → Eval harness (12) →
Retry caps + HITL (19) → Observability/tracing (21) → Fan-out/fan-in (22)
```

Every prior layer assumed one agent runs, finishes, then the next one starts. That assumption is fine until you have multiple specialist agents (Researcher, Critic) that don't actually depend on each other's output — at that point sequential execution is just wasted wall-clock time. This is the layer that turns "agents that run one after another because that's how I wired the graph" into "agents that run one after another only when they actually have to."

---

## 2. Why Sequential Doesn't Scale

```python
# What most multi-agent graphs look like by default
orchestrator → vector_researcher → web_researcher → critic → writer
# Each node waits on the previous one, even when vector_researcher
# and web_researcher share zero dependencies.
```

If Researcher pulls from N independent sources, running them one at a time means total latency is the *sum* of every call instead of the *max* of the slowest one. N independent branches × sequential wiring = paying for parallelism you never use.

---

## 3. Fan-Out: Dispatching in Parallel

**Fan-out** — Orchestrator dispatches to N agents at once instead of one at a time, using LangGraph's `Send` API to return multiple destinations from a single node.

```python
from langgraph.types import Send

def orchestrator(state: AgentState):
    # fan-out: dispatch to N parallel branches
    return [
        Send("vector_researcher", {"query": state["query"]}),
        Send("web_researcher", {"query": state["query"]}),
    ]
```

Each `Send` target runs as its own branch of the graph, concurrently, with its own slice of state to work on.

---

## 4. Fan-In: Merging Without Clobbering

**Fan-in** — a merge point waits for all parallel branches to complete, then reduces their outputs into one state object. The mechanism is a **reducer** attached to the shared state key:

```python
from typing import TypedDict, Annotated
import operator

class AgentState(TypedDict):
    query: str
    # reducer: parallel writes append instead of overwriting each other
    research_context: Annotated[list[str], operator.add]
    critique: str

def vector_researcher(state: dict):
    result = query_vector_store(state["query"])
    return {"research_context": [f"[vector] {result}"]}

def web_researcher(state: dict):
    result = query_web_search(state["query"])
    return {"research_context": [f"[web] {result}"]}

def critic(state: AgentState):
    # fan-in: only fires once both branches have written to research_context
    combined = "\n".join(state["research_context"])
    return {"critique": run_critic_llm(combined)}
```

Without `Annotated[list[str], operator.add]` on `research_context`, both branches try to overwrite the same key on the same update and LangGraph raises `InvalidUpdateError` at runtime — not at graph-definition time, so this fails silently until you actually run it.

---

## 5. Wiring It Together

```python
from langgraph.graph import StateGraph, START, END

graph = StateGraph(AgentState)
graph.add_node("vector_researcher", vector_researcher)
graph.add_node("web_researcher", web_researcher)
graph.add_node("critic", critic)

# orchestrator is a conditional entry point that returns Send objects
graph.add_conditional_edges(START, orchestrator, ["vector_researcher", "web_researcher"])
graph.add_edge("vector_researcher", "critic")
graph.add_edge("web_researcher", "critic")
graph.add_edge("critic", END)

app = graph.compile()
```

---

## 6. Bounding Concurrency (Tying Back to Day 19)

Parallel fan-out means N simultaneous LLM/tool calls instead of one — the retry caps from Day 19 now apply *per branch*, so uncapped fan-out multiplies retries instead of bounding them:

```python
import asyncio

MAX_CONCURRENT_AGENTS = 4
semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

async def bounded_researcher(state: dict, fn):
    async with semaphore:
        return await fn(state)  # fn still wrapped in Day 19's retry logic underneath
```

Cap total concurrent branches, not just per-branch retries — otherwise a fan-out of 10 with a retry cap of 3 each is 30 possible LLM calls for what looked like one dispatch.

---

## 7. Failure Modes

| Failure mode | Cause | Fix |
|---|---|---|
| Silent races | Two branches write to the same state key with no reducer | Add `Annotated[..., reducer_fn]` on every key touched by more than one branch |
| Cost amplification | Retry caps apply per branch, not globally | Cap total concurrent agent calls with a semaphore, not just per-agent retries |
| Fan-in deadlock | Merge node waits unconditionally for all branches | Pair fan-in with a timeout or partial-completion policy |
| False parallelism | Branches look independent but share hidden state (e.g. same vector index) | Audit for shared reads/writes before parallelizing — race just moves, doesn't disappear |
| No tracing across branches | Day 21's span tree assumes one linear execution path | Tag spans with branch id so parallel calls are distinguishable in the trace |

---

## 8. Full Pipeline (Tying Days 7, 19, 21, 22 Together)

```python
async def researcher_stage(state: AgentState):
    # Day 7: this is still just nodes in the LangGraph state machine

    # Day 22: fan-out — dispatch both researchers concurrently, bounded
    async def run_bounded(fn, s):
        async with semaphore:
            return await call_with_retry_cap(fn, s, retry_cap=3)  # Day 19

    results = await asyncio.gather(
        run_bounded(vector_researcher, state),
        run_bounded(web_researcher, state),
    )

    # Day 22: fan-in — merge via reducer, not manual overwrite
    merged_context = [r["research_context"][0] for r in results]

    # Day 21: log one span per branch, correlated to the run's trace id
    for r in results:
        log_span(node="researcher_branch", result=r)

    return {"research_context": merged_context}
```

---

## Key Takeaways

1. Fan-out/fan-in is a topology change, not a new framework — `Send` for dispatch, a reducer for merge.
2. The reducer is the whole trick: without it, parallel writes to the same key fail at runtime, not at definition time.
3. Retry caps and concurrency limits from Day 19 need to apply *across* parallel branches, not just within one — otherwise cost scales with branch count.
4. Fan-in needs an explicit partial-completion or timeout policy, or one hung branch deadlocks the entire graph.
5. Tracing (Day 21) needs branch-level tags once execution stops being linear, or the span tree stops being readable.

---

## Links & Resources

- [LangGraph — Send API and map-reduce patterns](https://langchain-ai.github.io/langgraph/)
- [LangGraph — how-to: map-reduce branching for parallel execution](https://langchain-ai.github.io/langgraph/how-tos/)

---

## Next Steps / Reflections

- [ ] Swap the Researcher node in the Orchestrator/Researcher/Critic/Writer graph to fan out into vector + web branches
- [ ] Add branch-id tags to Day 21's span logging so parallel calls show up distinctly in the trace
- [ ] Set a concrete `MAX_CONCURRENT_AGENTS` value and measure actual latency improvement vs. sequential
- [ ] Define a partial-completion policy for fan-in (wait-for-all vs. timeout) and test the timeout path deliberately
- [ ] Check for hidden shared state between "independent" branches before trusting the parallelism
