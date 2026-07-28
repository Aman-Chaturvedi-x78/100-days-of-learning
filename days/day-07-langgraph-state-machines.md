# Day 7: LangGraph — State Machines for Agent Workflows

## TL;DR
LangChain chains are linear; real agents need branches, loops, and retries. LangGraph models an agent as a **graph of nodes and edges** over a shared state object, which makes multi-step, conditional, and self-correcting agent behavior much easier to reason about than chaining prompts by hand.

## What I learned

**1. State is the core abstraction**
Every LangGraph app defines a `State` (typically a `TypedDict` or Pydantic model) that gets passed between nodes and updated as it flows through the graph — instead of threading context manually between chain calls.

```python
from typing import TypedDict, Annotated
import operator

class AgentState(TypedDict):
    input: str
    steps: Annotated[list, operator.add]
    result: str
```

**2. Nodes are just functions**
A node is any function that takes the state and returns a partial update to it. This makes each step testable in isolation — no need to spin up the whole graph to check one function's logic.

**3. Edges can be conditional**
This is the big upgrade over a plain LangChain sequence: `add_conditional_edges` lets the graph route to different nodes based on the current state — e.g., "if the tool call failed, retry" or "if confidence is low, ask a clarifying question" instead of always moving forward.

```python
graph.add_conditional_edges(
    "validate",
    lambda state: "retry" if state["errors"] else "finish",
    {"retry": "call_tool", "finish": END}
)
```

**4. Cycles are allowed (and the point)**
Unlike a DAG-only chain, LangGraph graphs can loop — a node can route back to an earlier node. That's what makes patterns like "plan → act → observe → replan" (ReAct-style agents) natural to express instead of hacking retries with a while loop around a chain.

**5. Checkpointing = memory + resumability**
LangGraph supports checkpointers (e.g., in-memory or SQLite) that persist state between runs. That means an agent can pause mid-task, get human input, and resume — useful for anything with a human-in-the-loop approval step.

## Why this matters for my work
This maps directly onto the agentic workflows I want to build going forward — a RAG pipeline that needs to *retry retrieval* when the first pass has low relevance, or an agent that needs a human approval step before taking an action, is much cleaner as a graph than as a linear chain with a bunch of if/else glue code.

## References
- LangGraph official docs — Concepts: Low Level
- LangChain blog: "LangGraph: Multi-Agent Workflows"
