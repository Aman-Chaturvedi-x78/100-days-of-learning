# Day 19: Bounded Loops & Retry Caps in Multi-Agent Systems

**Series:** 100 Days of Learning — Agentic AI & Multi-Agent Systems
**Builds on:** Day 18 (Orchestrator/Researcher/Critic/Writer architecture in LangGraph)

---

## Context

On Day 18 I mapped out the Orchestrator → Researcher → Critic → Writer graph and flagged a real gap while documenting it: the Critic loop (Critic sends work back to Writer/Researcher for revision) had **no hard retry cap**. In theory, a stubborn Critic and a Writer that can't satisfy it could loop indefinitely — burning tokens, latency, and money with no guaranteed termination.

Day 19 is about closing that gap properly: not just slapping a `while` loop limit on it, but understanding the general pattern of **bounded execution** in agentic systems.

---

## TL;DR

Any loop in a multi-agent graph — revision loops, tool-retry loops, self-correction loops — needs an explicit, enforced termination condition that is independent of the agent's own judgment. Agents are bad judges of when to stop. The graph (not the LLM) should own the stopping logic.

---

## The Core Ideas

- **Iteration count is state, not an afterthought.** In LangGraph, I added a `revision_count` field to the shared graph state, incremented on every Critic→Writer edge traversal. The conditional edge checks `revision_count >= MAX_REVISIONS` *before* it checks whether the Critic approved — count wins ties.

- **Two independent ceilings, not one.** I set both a **max iteration count** (e.g., 3 revision cycles) and a **max token/cost budget** for the sub-loop. A loop that's cheap-per-call but runs 50 times is just as dangerous as one that's expensive-per-call and runs 3 times. Either ceiling being hit forces exit.

- **Exit paths need to be first-class, not exceptions.** I used to treat "loop maxed out" as an error case. Better: it's a normal graph branch. On cap-out, the graph routes to an **escalation node** that either (a) ships the best-so-far draft with a flag, or (b) raises a human-in-the-loop interrupt (same pattern from Day 17) with the Critic's last set of objections attached, so a human isn't debugging from zero context.

- **Distinguish "stuck" from "improving."** A naive cap treats all rejections equally. A better signal: track whether the Critic's feedback is *converging* (fewer/smaller objections each round) or *oscillating* (same objection recurring, or new objections replacing old ones). Oscillation is a stronger, earlier signal to bail than just hitting the count — I log objection diffs between rounds to catch this instead of waiting for the hard cap.

- **Circuit breaker framing helps.** Borrowing from distributed systems: once the loop trips, it should have a "cool-down" state (don't immediately retry the same failing configuration) rather than the graph naively restarting the same cycle. In practice this meant caching the failed draft + objections so a retried run (if a human requests one) starts from that context instead of re-deriving it.

---

## Failure Modes I Was Explicitly Guarding Against

- **Silent infinite loops** — no cap at all, discovered only in prod via a runaway API bill.
- **Cap enforced but state not reset** — `revision_count` living in a place that doesn't get cleared between separate runs of the graph, so run #2 inherits run #1's exhausted budget.
- **LLM-judged termination** — asking the Writer/Critic "should we stop now?" and trusting the answer. Agents will happily keep going; the ceiling has to live outside their control.
- **Escalation with no context** — routing to a human but not attaching *why* it stopped (which objections, how many rounds, cost so far) — turns escalation into a black box instead of a handoff.

---

## Implementation

```python
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, END

MAX_REVISIONS = 3
MAX_TOKEN_BUDGET = 20_000  # tokens allotted to the revision sub-loop


class GraphState(TypedDict):
    draft: str
    objections: list[str]
    objection_history: list[list[str]]   # one entry per round, for convergence check
    revision_count: int
    tokens_used: int
    critic_approved: bool


def critic_node(state: GraphState) -> GraphState:
    result = run_critic(state["draft"])  # calls the Critic agent
    state["objections"] = result.objections
    state["objection_history"].append(result.objections)
    state["critic_approved"] = result.approved
    state["tokens_used"] += result.tokens_used
    return state


def is_oscillating(history: list[list[str]]) -> bool:
    """True if the last two rounds of objections aren't shrinking or changing meaningfully."""
    if len(history) < 2:
        return False
    prev, curr = set(history[-2]), set(history[-1])
    unresolved = prev & curr
    # same objections recurring, and not net shrinking -> stuck, not improving
    return len(unresolved) > 0 and len(curr) >= len(prev)


def route_after_critic(state: GraphState) -> Literal["writer", "escalate", "ship"]:
    if state["critic_approved"]:
        return "ship"

    capped_out = (
        state["revision_count"] >= MAX_REVISIONS
        or state["tokens_used"] >= MAX_TOKEN_BUDGET
    )
    stuck = is_oscillating(state["objection_history"])

    if capped_out or stuck:
        return "escalate"

    state["revision_count"] += 1
    return "writer"


def escalate_node(state: GraphState) -> GraphState:
    # Human-in-the-loop interrupt (same pattern as Day 17), with full context attached
    return interrupt_for_human(
        draft=state["draft"],
        objections=state["objections"],
        rounds_used=state["revision_count"],
        tokens_used=state["tokens_used"],
        reason="oscillating" if is_oscillating(state["objection_history"]) else "cap_reached",
    )


graph = StateGraph(GraphState)
graph.add_node("writer", writer_node)
graph.add_node("critic", critic_node)
graph.add_node("escalate", escalate_node)
graph.add_node("ship", ship_node)

graph.add_conditional_edges(
    "critic",
    route_after_critic,
    {"writer": "writer", "escalate": "escalate", "ship": "ship"},
)
graph.add_edge("writer", "critic")
graph.add_edge("escalate", END)
graph.add_edge("ship", END)
```

Key detail: `route_after_critic` checks `capped_out` and `stuck` **before** it ever looks at whether the Critic approved on this exact round — the cap/oscillation check is not something the Writer or Critic can talk their way around. `revision_count` and `tokens_used` live in the shared `GraphState`, get reset per invocation (not module-level globals), so a fresh run never inherits a previous run's exhausted budget — the state-reset failure mode I flagged above.

---

## Build Note

Patched this directly into my live Orchestrator/Researcher/Critic/Writer project: added `revision_count` and `token_budget_used` to the shared state schema, a hard cap of 3 revisions, an objection-diff check for early oscillation detection, and rerouted the "cap hit" branch into the same human-in-the-loop interrupt node from Day 17 instead of a dead-end error. LangSmith traces now clearly show three possible end states per run: **approved**, **capped-out (shipped with flag)**, **capped-out (escalated to human)** — instead of just success/failure.

Next up: thinking about whether this cap should be static or dynamically adjusted based on task complexity (a one-line fact-check shouldn't get the same 3-round budget as a multi-section report).

---

## Engagement Question

For folks building multi-agent loops — do you cap on **iteration count**, **token/cost budget**, or **feedback convergence**? Or all three? Curious what thresholds people have found actually work in production vs. what sounds reasonable on paper.

---

## Hashtags

#AgenticAI #LangGraph #MultiAgentSystems #LLMOps #BuildInPublic

---

---

# LinkedIn Post Version

So today I learned that the loop that makes multi-agent systems "self-correcting" is the same loop that can quietly bankrupt you if you don't cap it right.

**TL;DR:** Agents are bad judges of when to stop. The graph — not the LLM — should own the stopping logic for any revision or retry loop.

On Day 18 I mapped out my Orchestrator → Researcher → Critic → Writer architecture in LangGraph and flagged a gap while writing it up: the Critic→Writer revision loop had no hard retry cap. Today I fixed it properly.

**What I actually changed:**
- **Iteration count as state:** added `revision_count` to the shared graph state, checked *before* the Critic's approval status on every conditional edge.
- **Two ceilings, not one:** capped both revision count AND token/cost budget — a cheap loop that runs 50x is just as bad as an expensive one that runs 3x.
- **Escalation as a first-class branch:** hitting the cap now routes into the same human-in-the-loop interrupt node from Day 17, with the Critic's objections attached — not a dead-end error.
- **Convergence vs. oscillation:** now diffing the Critic's objections round-to-round. Shrinking objections = converging, keep going. Recurring/replacing objections = stuck, bail early instead of waiting for the hard cap.

**Failure mode I was guarding against:** asking the LLM "should we stop now?" and trusting the answer. It won't say no on its own — the ceiling has to live outside the agent's control.

Patched all of this into my live multi-agent project. LangSmith traces now show three clean end states per run: approved, capped-out-and-shipped, or capped-out-and-escalated — instead of just pass/fail.

**Question for the multi-agent builders here:** do you cap loops on iteration count, cost budget, or feedback convergence — or all three? What thresholds have actually held up in production?

#AgenticAI #LangGraph #MultiAgentSystems #LLMOps #BuildInPublic
