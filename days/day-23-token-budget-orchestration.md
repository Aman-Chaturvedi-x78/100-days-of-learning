---
day: 23
title: "Token Budgets & Cost-Aware Orchestration in Multi-Agent Systems"
tags: [langgraph, multi-agent, cost-optimization, orchestration, token-budgets]
series: "100 Days of Learning"
date: 2026-08-15
prev: "Day 22 — MCP Integration + Fan-Out/Fan-In Parallel Execution"
---

## Why This Matters

Days 17–22 built the shape of a multi-agent system: roles (Orchestrator/Researcher/Critic/Writer), retry loops, HITL checkpoints, observability, MCP as the tool-integration layer, and parallel fan-out/fan-in execution. None of that shape matters if the system is economically unviable — a fan-out step that spins up 6 parallel researcher calls on GPT-4-class models can burn a token budget in seconds with no upper bound.

Today's focus: treating **token budget as a first-class constraint** the orchestrator reasons about, not an afterthought caught in a billing dashboard.

## The Core Idea

A cost-aware orchestrator does three things a naive one doesn't:

1. **Allocates budget before dispatch** — each sub-agent gets a token ceiling, not just a task
2. **Tracks spend against budget mid-flight** — not just after the run completes
3. **Degrades gracefully** — cheaper model / shorter context / early-exit, instead of hard-failing when budget runs low

```
                    ┌─────────────────────┐
                    │   Orchestrator       │
                    │  (holds total budget)│
                    └──────────┬───────────┘
                               │ allocate sub-budgets
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        ┌───────────┐   ┌───────────┐   ┌───────────┐
        │Researcher │   │Researcher │   │  Critic   │
        │ budget:800│   │ budget:800│   │budget:400 │
        └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
              │                │                │
              └────────┬───────┴────────┬───────┘
                        ▼                ▼
                 spend reported    spend reported
                        │                │
                        ▼                ▼
                 ┌──────────────────────────┐
                 │  Budget Ledger (shared)   │
                 │  remaining = total - Σspend│
                 └──────────────────────────┘
```

## Implementation: A Budget-Aware State + Node

LangGraph's shared state is the natural place to hold a ledger. Each node reads remaining budget before deciding how much work to do, and writes actual spend back.

```python
from typing import TypedDict, Annotated
from operator import add

class BudgetState(TypedDict):
    total_budget: int
    spend_log: Annotated[list[dict], add]  # reducer accumulates across parallel branches
    task: str
    results: Annotated[list[str], add]

def remaining_budget(state: BudgetState) -> int:
    spent = sum(entry["tokens"] for entry in state["spend_log"])
    return state["total_budget"] - spent

def researcher_node(state: BudgetState) -> dict:
    budget = remaining_budget(state)
    if budget < 200:
        # degrade instead of failing
        return {
            "results": [f"[skipped: insufficient budget, {budget} left]"],
            "spend_log": [{"node": "researcher", "tokens": 0}],
        }

    # scale request size to what's actually available
    max_tokens = min(800, budget // 2)
    response = call_model(state["task"], max_tokens=max_tokens)

    return {
        "results": [response.text],
        "spend_log": [{"node": "researcher", "tokens": response.usage.total_tokens}],
    }
```

The key move: `max_tokens = min(800, budget // 2)` — the node never assumes it has its full allocation. It checks what's actually left and reserves headroom for whatever runs after it.

## Degradation Ladder

Rather than a binary "proceed / hard-fail," define tiers:

| Remaining budget | Behavior |
|---|---|
| > 80% | Full quality — largest model, full context window |
| 40–80% | Reduce context (trim retrieved docs, shorter history) |
| 15–40% | Switch to cheaper/faster model tier |
| < 15% | Skip non-critical branches (e.g., secondary researcher), Critic does lightweight pass only |
| 0% | Orchestrator short-circuits, returns best-effort partial result with a flag |

This ladder is itself a config object the orchestrator consults — not hardcoded per node — so it's tunable without touching agent logic.

## Failure Modes

| Failure Mode | Symptom | Fix |
|---|---|---|
| **Budget checked, not reserved** | Two parallel branches both see "budget available," both spend, total blows past ceiling | Reserve budget optimistically before dispatch (pessimistic locking), reconcile actual spend after |
| **All-or-nothing degradation** | System has no middle tier between "full run" and "hard fail" | Build the tiered ladder above; test each tier independently |
| **Budget doesn't account for retries** | A bounded retry loop (Day 19–20 pattern) silently re-spends full budget per retry | Retry budget should be a *fraction* of remaining, not a fresh allocation |
| **Ledger reducer collisions in fan-out** | Parallel branches overwrite each other's spend instead of accumulating | Use LangGraph's `Annotated[list, add]` reducer pattern, never plain dict overwrite, for concurrent writes |
| **No visibility until the run ends** | Budget exhaustion discovered only in the final billing log, not during execution | Emit spend to your Day 21 observability/tracing layer in real time, not just at completion |

## Where This Lands: Orbital Watch

This is the direct prerequisite for **Orbital Watch** — a space-situational-awareness agent where token-budget awareness isn't an optimization, it's the premise: bounded compute, prioritizing which objects/events get full-depth analysis vs. a cheap triage pass. Day 23's ledger pattern becomes Orbital Watch's core scheduling primitive: allocate budget by priority score, not equally across all fan-out branches.

## Next Steps

- [ ] Extend `BudgetState` with per-node budget *reservations* (not just post-hoc spend logging) to fix the race condition in the failure-mode table
- [ ] Wire spend_log into the Day 21 tracing setup so budget burn is visible mid-run, not just at the end
- [ ] Prototype the degradation ladder as a standalone config, test each tier in isolation before wiring into the fan-out graph
- [ ] Sketch Orbital Watch's priority-weighted budget allocator as the first concrete application of this pattern
