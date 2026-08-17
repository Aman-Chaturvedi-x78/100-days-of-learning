---
day: 24
title: "Budget Reservations: Fixing the Race Condition in Concurrent Token Spend"
tags: [langgraph, multi-agent, cost-optimization, orchestration, concurrency]
series: "100 Days of Learning"
date: 2026-08-17
prev: "Day 23 — Token Budgets & Cost-Aware Orchestration"
---

## Why This Matters

Day 23 built a budget-aware orchestrator: nodes check `remaining_budget()` before deciding how much work to do. That works cleanly in a sequential graph. It silently breaks the moment fan-out (Day 22) enters the picture.

The bug: two parallel Researcher branches both call `remaining_budget()` at effectively the same instant, both see 800 tokens available, both proceed to spend up to 800 — and the ledger ends up 800 tokens over ceiling with no error raised anywhere. This is a classic check-then-act race condition, just wearing a token-budget costume instead of a database-transaction costume.

Today's focus: replacing the "check, then hope" pattern with **reserve, then reconcile**.

## The Core Idea

The Day 23 ledger had one writer path: nodes report spend after the fact. That's the problem. Fan-out needs two writer paths with different timing:

1. **Reserve at dispatch time** — before the orchestrator sends work to a branch, it deducts that branch's max possible spend from the shared budget immediately, synchronously, before any branch starts running
2. **Reconcile at completion time** — when a branch finishes and reports actual spend (almost always less than its reservation), the orchestrator refunds the unused difference back to the pool

Critically, only the **orchestrator** node writes to `reserved`. Parallel branches never touch it directly — they only ever write to `spend_log`, using the same `Annotated[list, add]` reducer from Day 23. Single writer, no collision, no race.

```
Orchestrator (before dispatch):
  reserved += branch.max_tokens   ← synchronous, happens before fan-out fires

Branch (during execution):
  spend_log += [{node, actual_tokens}]   ← reducer-accumulated, safe under concurrency

Orchestrator (after fan-in):
  refund = reserved_for_branch - actual_spent
  reserved -= refund
```

## Implementation: Reserve/Reconcile State

```python
from typing import TypedDict, Annotated
from operator import add

class BudgetState(TypedDict):
    total_budget: int
    reserved: int                          # single-writer: orchestrator only
    spend_log: Annotated[list[dict], add]  # multi-writer: reducer-accumulated
    task: str
    results: Annotated[list[str], add]

def available_budget(state: BudgetState) -> int:
    spent = sum(entry["tokens"] for entry in state["spend_log"])
    # what's left after both actual spend AND outstanding reservations
    return state["total_budget"] - spent - state["reserved"]

def orchestrator_dispatch(state: BudgetState, branches: list[str]) -> dict:
    """Runs BEFORE fan-out. Reserves budget synchronously, one branch at a time,
    so no two branches can ever see the same 'available' number."""
    per_branch_ceiling = 800
    total_reservation = 0

    for _ in branches:
        avail = state["total_budget"] - sum(
            e["tokens"] for e in state["spend_log"]
        ) - state["reserved"] - total_reservation

        if avail < 200:
            break  # stop reserving for branches we can't afford
        total_reservation += min(per_branch_ceiling, avail)

    return {"reserved": state["reserved"] + total_reservation}

def researcher_node(state: BudgetState) -> dict:
    # branch trusts its reservation exists; it just spends up to its slice
    max_tokens = min(800, state["total_budget"] - state["reserved"])
    response = call_model(state["task"], max_tokens=max_tokens)

    return {
        "results": [response.text],
        "spend_log": [{"node": "researcher", "tokens": response.usage.total_tokens}],
    }

def orchestrator_reconcile(state: BudgetState, branch_reservation: int) -> dict:
    """Runs AFTER fan-in. Refunds unused reservation back to the pool."""
    actual = sum(e["tokens"] for e in state["spend_log"][-1:])  # this branch's entry
    refund = max(0, branch_reservation - actual)
    return {"reserved": state["reserved"] - refund}
```

The key shift from Day 23: `available_budget()` now subtracts both **actual spend** and **outstanding reservations**, so a second branch dispatched a millisecond later sees the honest remaining number — not a stale one.

## Failure Modes

| Failure Mode | Symptom | Fix |
|---|---|---|
| **Reservation without reconciliation** | Budget appears to shrink permanently even when branches under-spend their allocation | Always refund `reservation - actual` after each branch completes, not just on overspend |
| **Branches writing to `reserved` directly** | Same race condition as Day 23, just moved one field over | Keep `reserved` single-writer (orchestrator only); branches only ever touch `spend_log` |
| **Reserving all branches in parallel** | If reservation itself isn't sequential, you've just relocated the race, not fixed it | The reservation loop in `orchestrator_dispatch` must run synchronously, branch by branch, before any branch starts executing |
| **Forgetting retries need reservations too** | A Day 19-style retry re-enters a branch without re-reserving, spending against a stale ceiling | Retry should re-check `available_budget()` and re-reserve before each attempt, not reuse the original reservation |
| **No timeout on stuck branches** | A hung branch holds its reservation forever, starving the rest of the fan-out | Pair reservations with a hard per-branch timeout that force-reconciles (refunds) on expiry |

## Where This Lands: Orbital Watch

This is the piece that makes Orbital Watch's priority-weighted allocator actually safe to run concurrently. When the orchestrator fans out across dozens of tracked objects — some getting full-depth analysis, most getting a cheap triage pass — the reservation step is what prevents a burst of high-priority objects from collectively overspending the run's budget before any of them finish and report back. Without reserve/reconcile, the priority weighting from Day 23 is just a suggestion; with it, it's an enforced ceiling.

## Next Steps

- [ ] Add a hard per-branch timeout that force-triggers reconciliation (refund) if a branch hangs
- [ ] Extend the retry path (Day 19) to re-reserve before each attempt instead of reusing the original slice
- [ ] Write a concurrency test: fire N parallel branches against a tight budget, assert total spend never exceeds ceiling
- [ ] Wire reservation/reconciliation events into the Day 21 tracing layer so budget state is inspectable mid-run, not just spend
- [ ] Apply this pattern directly to Orbital Watch's priority-weighted scheduler as the first real concurrent workload
