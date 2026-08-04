---
date: 2026-08-04
day: 13
title: "Observability & Tracing: Debugging Agents in Production"
tags: [agents, observability, tracing, langgraph, monitoring]
---

TL;DR
- Evals (Day 12) catch known failure modes on a golden set, offline, before you ship. **Observability catches unknown failure modes on real traffic, in prod, after you ship.** You need both — they answer different questions.
- The unit of observability for an agent isn't a log line, it's a **trace**: the full tree of LLM calls, tool calls, and retrieval steps for one user turn, with timing and token cost at each node.
- Structured tracing (spans with parent/child IDs) beats print statements the same way structured logging beat print statements a decade ago — except here the "stack trace" is a graph, not a call stack.
- The three things you actually look at day to day: **latency breakdown** (which node is slow), **cost breakdown** (which node is expensive), and **failure traces** (what did the agent actually do right before it went wrong).

---

## 1. Where This Fits

Stack so far:

```
Chunking (6) → Vector DB (9) → LangGraph agent (7) → Tool calling (10) → Memory (11) → Evals (12) → Observability (13)
```

Day 12 answers "did my change make this better on the cases I thought to test." Day 13 answers "what is actually happening to the 1000 users who aren't in my golden set." Evals are a pre-deployment gate; observability is what runs forever, on everything, whether or not anything looks wrong.

---

## 2. Why Logs Aren't Enough

```python
# This is what most people start with
print(f"Calling tool: {tool_name} with args: {args}")
print(f"Got response: {response}")
```

Works for one call. Falls apart the moment there's a graph: which memory retrieval belongs to which tool call, which of the 4 LLM calls in this turn actually mattered, why did this turn cost 3x the usual — none of that is answerable from a flat log stream. You need the calls linked into a tree.

---

## 3. Traces and Spans

A **trace** is one end-to-end unit of work (one user turn). A **span** is one step inside it (one LLM call, one tool call, one retrieval). Spans nest — a span can have child spans — which is what lets you reconstruct the actual execution tree afterward.

```python
import time, uuid

class Span:
    def __init__(self, name, parent=None, **metadata):
        self.id = str(uuid.uuid4())
        self.name = name
        self.parent_id = parent.id if parent else None
        self.metadata = metadata
        self.start = time.time()
        self.end = None
        self.error = None

    def close(self, **result_metadata):
        self.end = time.time()
        self.metadata.update(result_metadata)

    @property
    def duration_ms(self):
        return (self.end - self.start) * 1000 if self.end else None

def traced_llm_call(prompt, parent_span, model="claude-sonnet-4-6"):
    span = Span("llm_call", parent=parent_span, model=model, prompt_tokens=len(prompt) // 4)
    try:
        response = llm.invoke(prompt)
        span.close(completion_tokens=len(response.content) // 4, status="ok")
        return response
    except Exception as e:
        span.error = str(e)
        span.close(status="error")
        raise
```

Every node in the Day 7 LangGraph agent — LLM call, tool call (Day 10), memory retrieval (Day 11), vector search (Day 9) — gets wrapped the same way. The parent_id chain is what turns a flat list of spans back into a tree at query time.

---

## 4. Wiring It Into the LangGraph Agent

```python
def agent_turn(state, trace_span):
    memory_span = Span("memory_retrieval", parent=trace_span)
    memories = retrieve_memories(state["user_id"], state["messages"][-1].content)
    memory_span.close(num_results=len(memories))

    rag_span = Span("rag_retrieval", parent=trace_span)
    doc_context = retrieve_node(state)["retrieved_context"]
    rag_span.close(num_chunks=len(doc_context))

    llm_span = Span("llm_call", parent=trace_span)
    response = llm.invoke(
        tools=tools,
        messages=state["messages"] + [
            {"role": "system", "content": f"Known about user: {memories}\nRelevant docs: {doc_context}"}
        ]
    )
    llm_span.close(tokens=response.usage.output_tokens)

    return {"messages": state["messages"] + [response]}
```

One turn now produces a small tree: `trace → [memory_retrieval, rag_retrieval, llm_call]`. If the turn is slow, the trace tells you exactly which span ate the time instead of you guessing.

---

## 5. What You Actually Look At

**Latency breakdown** — which span is the bottleneck, per turn and aggregated over time:

```python
def latency_breakdown(spans):
    by_name = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s.duration_ms)
    return {name: {"p50": percentile(durs, 50), "p95": percentile(durs, 95)} for name, durs in by_name.items()}
```

**Cost breakdown** — same idea, but summing token cost per span type instead of duration. Usually surprising: retrieval nodes are near-free, LLM calls dominate, and the memory-extraction side-effect from Day 11 quietly adds up if it fires on every turn.

**Failure traces** — when something goes wrong, pull the full tree for that trace_id and read it top to bottom. This is the actual debugging workflow: a user reports a bad answer, you look up their trace, you see the memory retrieval returned stale facts, tool calling picked the wrong tool because of that, and the final LLM call just inherited the mistake.

---

## 6. Failure Modes

| Failure mode | Cause | Fix |
|---|---|---|
| Can't reconstruct what happened | Flat print/log statements, no parent-child linking | Structured spans with trace_id + parent_id on every call |
| Tracing overhead slows the agent | Synchronous export of every span to a backend | Batch/async export, don't block the response on logging |
| Can't find the trace for a bad user report | No trace_id surfaced to the user-facing layer | Return trace_id in the response metadata (even if hidden from UI) |
| Cost/latency numbers look fine in aggregate but users complain | p50 hides p95/p99 tail latency | Always look at percentiles, not just averages |
| Traces exist but nobody looks at them until something breaks | No dashboard, only ad-hoc queries | Wire up even a minimal dashboard (Day 12's pass-rate tracking + this) so drift is visible before a user reports it |
| Sensitive data (user PII, memory contents) leaking into trace storage | Logging full prompts/responses unfiltered | Redact or hash sensitive fields before spans are persisted |

---

## 7. Full Pipeline (Tying Days 6, 7, 9, 10, 11, 12, 13 Together)

```python
def run_agent_turn_observed(state):
    trace = Span("agent_turn", user_id=state["user_id"])
    try:
        result = agent_turn(state, trace_span=trace)  # Days 6/7/9/10/11 all emit child spans
        trace.close(status="ok")
    except Exception as e:
        trace.error = str(e)
        trace.close(status="error")
        raise
    finally:
        export_trace(trace)  # async, non-blocking
    return result
```

Evals (12) tell you the agent is good *before* you ship. Observability (13) tells you whether it's still good *after* you ship, on traffic you never wrote a test case for. Neither replaces the other.

---

## Key Takeaways

1. Evals are pre-deployment and cover known cases; observability is post-deployment and covers everything, including cases nobody thought to write a golden example for.
2. The right unit is a trace (a tree of spans), not a log line — you need parent-child structure to reconstruct what actually happened in a multi-step agent.
3. Three views matter day to day: latency breakdown, cost breakdown, and full failure traces — build for these three, not generic logging.
4. Tail latency (p95/p99) is where user pain lives; averages hide it.
5. Trace export should never block the user-facing response — async/batch it, same instinct as Day 11's memory extraction running as a side effect.

---

## Links & Resources

- [LangSmith Tracing docs](https://docs.smith.langchain.com/observability)
- [OpenTelemetry — Traces concept](https://opentelemetry.io/docs/concepts/signals/traces/)
- [Anthropic — Building Effective Agents (observability considerations)](https://www.anthropic.com/research/building-effective-agents)

---

## Next Steps / Reflections

- [ ] Add span wrapping to the Day 7 LangGraph agent for real, not just pseudocode — start with LLM calls and tool calls
- [ ] Stand up a minimal trace store (SQLite is enough at this scale) and a 3-panel view: latency, cost, recent errors
- [ ] Pull a failure trace end-to-end for a deliberately broken input and confirm I can actually diagnose it from the trace alone, no extra debugging
- [ ] Redact memory contents and user PII before persisting spans — do this before wiring up any real traffic, not after
