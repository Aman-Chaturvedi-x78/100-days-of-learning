---
date: 2026-08-09
day: 17
title: "Human-in-the-Loop: Checkpoints, Interrupts, and Approval Gates in LangGraph"
tags: [agents, langgraph, human-in-the-loop, checkpoints, safety]
---

TL;DR
- Day 16's guardrails handle the cases a machine *can* decide (schema valid? injection detected?). **Human-in-the-loop (HITL) is for the cases a machine shouldn't decide alone** — irreversible actions, high-stakes outputs, or genuine ambiguity a classifier can't resolve.
- LangGraph supports this natively via **checkpointers + interrupts**: the graph pauses at a named node, persists its full state, and waits — potentially for hours or days — for a human decision before resuming.
- The hard part isn't the interrupt mechanism itself, it's **state design**: what gets shown to the human, what "approve/reject/edit" actually mutates in the graph state, and how resume behaves differently for each.
- This is the exact pattern from my multi-agent research assistant (Orchestrator → Researcher → Critic → Writer) — the Critic node doesn't auto-publish the Writer's draft, it pauses for a human checkpoint before the final write.

---

## 1. Where This Fits

Stack so far:

```
Chunking (6) → Vector DB (9) → LangGraph agent (7) → Tool calling (10) → Memory (11)
→ Evals (12) → Observability (13) → Guardrails (16) → Human-in-the-Loop (17)
```

Guardrails (16) answer "is this output *malformed or unsafe*?" — a yes/no a machine can compute. HITL answers "is this output *the right call*?" — a judgment a machine often can't make confidently, especially for anything irreversible: sending an email, executing a trade, deleting data, publishing content under someone's name.

The rule of thumb I'm using: if a guardrail's fallback for "uncertain" would be "block and lose the work," a human checkpoint is probably the better fallback — pause, don't discard.

---

## 2. The Core Primitive: Checkpointer + Interrupt

LangGraph graphs are normally stateless between calls — you pass state in, get state out. A **checkpointer** persists that state to storage (SQLite, Postgres, Redis) after every node, which is what makes pausing *indefinitely* possible instead of just holding state in memory for the duration of one process.

```python
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, END

checkpointer = SqliteSaver.from_conn_string("checkpoints.db")

graph = StateGraph(AgentState)
graph.add_node("researcher", researcher_node)
graph.add_node("critic", critic_node)
graph.add_node("await_approval", await_approval_node)  # the HITL pause point
graph.add_node("writer", writer_node)

graph.add_edge("researcher", "critic")
graph.add_edge("critic", "await_approval")
graph.add_edge("writer", END)

app = graph.compile(checkpointer=checkpointer, interrupt_before=["writer"])
```

`interrupt_before=["writer"]` means: run everything up to (not including) `writer`, then stop and return control to the caller. The graph doesn't need a special "waiting" node — the interrupt is declared at compile time against a normal node name.

---

## 3. Resuming: Approve, Reject, or Edit

Resuming isn't just "continue" — the human's decision has to actually change state, not just unblock it.

```python
config = {"configurable": {"thread_id": "run-42"}}

# First call — runs until the interrupt, then stops
result = app.invoke({"messages": [...], "topic": "..."}, config)
draft = result["critic_notes"]["draft"]  # show this to the human

# --- human reviews draft, picks one of three paths ---

# Approve as-is: resume with no state changes
app.invoke(None, config)

# Edit: mutate state before resuming
app.update_state(config, {"critic_notes": {"draft": edited_draft, "approved": True}})
app.invoke(None, config)

# Reject: route back to researcher instead of forward to writer
app.update_state(config, {"critic_notes": {"approved": False, "feedback": human_feedback}})
app.invoke(None, config, as_node="critic")  # re-enter at critic with rejection feedback
```

Passing `None` as input on resume is the signal to LangGraph: "don't add a new message, just continue from the persisted checkpoint." `update_state` is what actually lets the human's edit flow into the graph — without it, "approve" and "resume" would be indistinguishable from the graph's point of view.

---

## 4. Wiring This Into the Orchestrator/Researcher/Critic/Writer Project

This is where it stopped being theoretical for me. The Critic node already produces a structured verdict (Day 16's schema validation applies here too — `CriticVerdict{approved: bool, notes: str}`). The missing piece was: what happens when the Critic itself is uncertain, or when the output is going somewhere consequential (published research summary, not a throwaway draft)?

```python
def critic_node(state):
    verdict = run_critic_llm(state["draft"])
    if verdict.confidence < 0.7 or state["publish_target"] == "external":
        # low confidence OR external-facing output → force a human checkpoint
        return {"critic_notes": verdict, "needs_human": True}
    return {"critic_notes": verdict, "needs_human": False}

# conditional edge: only pause if needs_human is True
graph.add_conditional_edges(
    "critic",
    lambda state: "await_approval" if state["needs_human"] else "writer",
    {"await_approval": "await_approval", "writer": "writer"},
)
```

The key design decision: **HITL isn't "always pause," it's conditional** — cheap/reversible/high-confidence paths skip straight to the Writer, and only the risky or uncertain ones cost a human's time. Pausing on every single run defeats the point of having an agent at all.

---

## 5. Failure Modes

| Failure mode | Cause | Fix |
|---|---|---|
| Graph "hangs" forever | No checkpointer configured — interrupt has nothing to persist to, or thread_id lost | Always pair `interrupt_before`/`interrupt_after` with a real checkpointer and a stable `thread_id` |
| Human approves, but nothing changes on resume | Resumed with new input instead of `None` + `update_state` | `None` input = pure resume; state edits must go through `update_state` first |
| Every single run pauses, humans stop reading the queue | No confidence/risk gating — HITL applied unconditionally | Gate the interrupt behind a condition (Section 4), not a blanket pause |
| Rejected drafts silently vanish | No re-entry edge back to the producing node | Route rejection back with `as_node=`, carrying human feedback into the retry |
| Checkpoint state grows unbounded | Every node's full state persisted forever, no cleanup | TTL/prune old threads once a run resolves (approved, rejected-and-abandoned, or timed out) |
| Reviewer sees raw JSON instead of the actual draft | State shown to human wasn't formatted for a human | Build a small render step between "state" and "what the human sees" — don't dump the dict |

---

## 6. Full Pipeline (Tying Days 6, 7, 9, 10, 11, 12, 13, 16, 17 Together)

```python
def run_agent_turn_production(state):
    trace = Span("agent_turn", user_id=state["user_id"])
    try:
        result = agent_turn_guarded(state, trace_span=trace)   # Day 16 guardrails wrap Days 6/7/9/10/11
        if result.get("needs_human"):
            trace.close(status="paused_for_human")             # Day 17 — checkpoint persists, graph pauses
            return result
        trace.close(status="ok")
    except Exception as e:
        trace.error = str(e)
        trace.close(status="error")
        raise
    finally:
        export_trace(trace)  # Day 13
    return result
```

Evals (12) check quality before shipping. Observability (13) checks quality after shipping. Guardrails (16) stop malformed output from reaching anyone. Human-in-the-loop (17) is the layer for the decisions that are correctly shaped and pass every guardrail, but still shouldn't be made by the agent alone.

---

## Key Takeaways

1. HITL and guardrails solve different problems: guardrails catch what's *wrong*, HITL catches what's *uncertain or high-stakes* even when it's technically valid.
2. LangGraph's checkpointer + `interrupt_before`/`interrupt_after` makes pausing durable — the graph can wait far longer than a single process lifetime.
3. Resuming has three real shapes (approve/edit/reject), and each needs different handling via `update_state` and `as_node` — not just "unpause."
4. HITL should be conditional, not blanket — gate it on confidence or blast-radius, or it becomes a bottleneck nobody reads.
5. What a human sees at a checkpoint matters as much as the mechanism — render state into something reviewable, don't hand over raw JSON.

---

## Links & Resources

- [LangGraph — Human-in-the-loop docs](https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/)
- [LangGraph — Persistence & checkpointers](https://langchain-ai.github.io/langgraph/concepts/persistence/)
- [Anthropic — Building Effective Agents (human oversight)](https://www.anthropic.com/research/building-effective-agents)

---

## Next Steps / Reflections

- [ ] Add the confidence-gated conditional edge (Section 4) to the actual Orchestrator/Researcher/Critic/Writer repo, not just this write-up
- [ ] Build the human-facing render step — right now the "review" is just printing the state dict, which fails the failure-mode table above
- [ ] Test the reject-and-retry loop end to end: does feedback actually reach the Researcher node, or just get dropped?
- [ ] Swap SqliteSaver for a Postgres checkpointer to see what changes for a multi-user reviewer setup
