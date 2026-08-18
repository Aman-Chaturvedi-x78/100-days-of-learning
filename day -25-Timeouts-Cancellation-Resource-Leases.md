---

## day: 25 title: "Timeouts, Cancellation & Resource Leases in Multi-Agent Systems" tags: [langgraph, multi-agent, reliability, orchestration, concurrency, resource-management] series: "100 Days of Learning" date: 2026-08-18 prev: "Day 24 — Budget Reservations: Fixing the Race Condition in Concurrent Token Spend"

## Why This Matters

Day 24 solved the concurrent token-spending race condition with **reserve, then reconcile**. Before a branch starts, the orchestrator reserves its maximum possible spend. When the branch finishes, the unused portion is refunded.

That works as long as the branch eventually finishes.

The problem: a branch can hang indefinitely after receiving its reservation.

A Researcher branch might reserve 800 tokens, call an external model, and then get stuck waiting for a response. The orchestrator has no reason to release those 800 tokens because it never receives a completion event. Other branches now see less available capacity even though the original branch may never actually spend its allocation.

This is a **resource leak**.

Today's focus: putting a bounded lifetime around every reservation using **timeouts, cancellation, and resource leases**.

The principle is:

> **Every resource reservation needs an owner, an expiry time, and a guaranteed cleanup path.**

## The Core Idea

Day 24 established:

```
reserve → execute → reconcile
```

Day 25 turns that into:

```
reserve → lease → execute → reconcile
                 │
                 ├── complete → reconcile
                 ├── cancel   → reconcile
                 └── timeout  → force reconcile
```

A **lease** is a temporary claim on a resource. Instead of saying "this branch owns 800 tokens", the orchestrator says "this branch owns up to 800 tokens for the next 30 seconds."

Every reservation now carries an expiration deadline:

```
reservation:
  id: res-42
  branch: researcher-2
  tokens: 800
  created_at: 12:00:00
  expires_at: 12:00:30
  status: ACTIVE
```

The branch can finish normally:

```
ACTIVE
  ↓
COMPLETED
  ↓
reconcile(actual_spend)
```

Or it can exceed its deadline:

```
ACTIVE
  ↓
expires_at reached
  ↓
EXPIRED
  ↓
force reconcile
  ↓
release unused reservation
```

The important invariant is:

```
Every reservation must eventually reach a terminal state.

ACTIVE → COMPLETED
ACTIVE → CANCELLED
ACTIVE → EXPIRED
```

It must never remain:

```
ACTIVE forever
```

## Implementation: Reservation Leases

```
from typing import TypedDict
from datetime import datetime, timedelta
import uuid

class Reservation(TypedDict):
    id: str
    branch_id: str
    tokens: int
    created_at: str
    expires_at: str
    status: str

def create_reservation(
    branch_id: str,
    tokens: int,
    timeout_seconds: int = 30,
) -> Reservation:

    now = datetime.utcnow()

    return {
        "id": str(uuid.uuid4()),
        "branch_id": branch_id,
        "tokens": tokens,
        "created_at": now.isoformat(),
        "expires_at": (
            now + timedelta(seconds=timeout_seconds)
        ).isoformat(),
        "status": "ACTIVE",
    }
```

The reservation is no longer just an integer.

It has an identity, an owner, a lifetime, and a state.

That makes it possible for the orchestrator to answer four questions:

```
Who owns this reservation?
How much did they reserve?
When does it expire?
Has it already been reconciled?
```

## Detecting Expired Leases

```
def lease_expired(reservation: Reservation) -> bool:
    expires_at = datetime.fromisoformat(
        reservation["expires_at"]
    )

    return datetime.utcnow() >= expires_at


def find_expired_reservations(
    reservations: list[Reservation],
) -> list[Reservation]:

    return [
        reservation
        for reservation in reservations
        if reservation["status"] == "ACTIVE"
        and lease_expired(reservation)
    ]
```

The orchestrator can periodically inspect active reservations.

This creates a simple recovery mechanism for branches that disappear, hang, or otherwise fail to produce a completion event.

The important point is that **the branch does not have to be alive for the reservation to eventually be released**.

## Implementation: Hard Branch Timeout

A lease protects the budget, but it should also be paired with a timeout on the actual work.

```
import asyncio

async def researcher_node(state, reservation):

    try:
        response = await asyncio.wait_for(
            call_model(
                state["task"],
                max_tokens=reservation["tokens"],
            ),
            timeout=30,
        )

        return {
            "results": [response.text],
            "spend_log": [{
                "reservation_id": reservation["id"],
                "node": reservation["branch_id"],
                "tokens": response.usage.total_tokens,
            }],
        }

    except asyncio.TimeoutError:
        return {
            "timeout": True,
            "reservation_id": reservation["id"],
        }
```

The critical distinction is:

```
timeout
   ↓
does NOT mean
   ↓
ignore the reservation
```

It means:

```
timeout
   ↓
cancel/stop the work
   ↓
mark reservation expired
   ↓
reconcile
   ↓
return unused capacity
```

The timeout protects both sides of the system:

* the **worker** cannot run forever
* the **budget** cannot remain locked forever

## Cancellation vs Timeout

Timeout and cancellation are related but they are not the same event.

### Timeout

The system waited longer than the allowed execution window:

```
30 seconds exceeded
        ↓
TIMEOUT
```

### Cancellation

Something intentionally stopped the branch:

```
User cancels
      OR
Parent workflow fails
      OR
Higher-priority work requires capacity
      ↓
CANCELLED
```

Both paths must eventually reach cleanup.

```
                ┌── COMPLETED
                │
ACTIVE ─────────┼── TIMEOUT
                │
                └── CANCELLED
                         │
                         ▼
                    RECONCILE
```

This is important because cleanup should not exist only in the successful execution path.

## Force Reconciliation

Suppose a branch reserves 800 tokens but times out after consuming only 120.

The orchestrator cannot wait indefinitely for the branch to report its final usage.

Instead:

```
reservation = 800
actual spend = 120

refund = 800 - 120
       = 680
```

The 680 unused tokens return to the available pool.

A simple reconciliation function:

```
def force_reconcile(
    reserved_tokens: int,
    known_actual_spend: int,
) -> int:

    unused = max(
        0,
        reserved_tokens - known_actual_spend
    )

    return unused
```

The difficult case is when the branch times out before its actual usage is known.

That requires an explicit accounting policy.

The system should never silently assume that a timed-out branch spent zero tokens, because the underlying model or external service may have already consumed resources.

A production implementation should therefore distinguish:

```
known_actual_spend
unknown_actual_spend
```

and reconcile late usage when it eventually becomes available.

## The Double-Reconciliation Problem

Timeout handling creates another concurrency race.

Consider:

```
12:00:29 → branch finishes
12:00:30 → watchdog marks it expired
```

The normal completion path and timeout path can both attempt reconciliation.

Without protection:

```
normal completion → refund 500
timeout handler   → refund 500

total refund = 1,000
```

The budget is now corrupted.

The fix is **idempotent terminal state transitions**.

```
def transition_to_terminal(
    reservation: Reservation,
    new_status: str,
) -> bool:

    if reservation["status"] != "ACTIVE":
        return False

    reservation["status"] = new_status
    return True
```

Now only the first terminal transition wins.

```
ACTIVE
  │
  ├── completion wins → COMPLETED
  │
  └── timeout loses   → ignored
```

Or:

```
ACTIVE
  │
  ├── timeout wins    → EXPIRED
  │
  └── completion loses → ignored
```

Exactly one path owns reconciliation.

This is the same basic principle used in reliable distributed systems: **a resource should be released exactly once, regardless of how many observers notice the failure.**

## Failure Modes

| Failure Mode                                | Symptom                                                                      | Fix                                                                    |
| ------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| **Reservation without expiry**              | A hung branch locks budget indefinitely                                      | Give every reservation a hard expiry/lease duration                    |
| **Timeout without cancellation**            | The orchestrator reports a timeout but the underlying task continues running | Propagate cancellation to the actual worker/API call                   |
| **Cancellation without reconciliation**     | Cancelled branches continue holding reserved capacity                        | Send cancellation through the same cleanup/reconciliation path         |
| **Double reconciliation**                   | Completion and timeout both refund the same reservation                      | Use an idempotent terminal state transition                            |
| **Unknown actual spend**                    | A branch times out before usage information arrives                          | Use explicit accounting states and reconcile late usage when available |
| **Very long leases**                        | Failed work occupies resources for too long                                  | Set a hard maximum lease duration                                      |
| **Very short leases**                       | Healthy branches are killed before they can finish                           | Set timeouts based on realistic execution latency                      |
| **Zombie workers**                          | A branch is marked timed out but continues consuming resources               | Ensure cancellation reaches the actual worker/process                  |
| **Retry after timeout without reservation** | A retry spends against capacity already allocated elsewhere                  | Treat every retry as a fresh admission and reservation decision        |

## Where This Lands: Orbital Watch

Day 24 made Orbital Watch's priority-weighted allocator safe against **concurrent overspending**.

Day 25 makes it safe against **stuck work**.

Consider a high-priority satellite requiring deep analysis:

```
Priority = HIGH
Reservation = 800 tokens
Lease = 30 seconds
```

The branch starts successfully, but the external model becomes slow.

Without Day 25:

```
800 tokens reserved
       ↓
branch hangs
       ↓
reservation remains locked
       ↓
available budget decreases
       ↓
other objects are rejected
```

With Day 25:

```
800 tokens reserved
       ↓
30-second lease created
       ↓
branch starts
       ↓
execution exceeds deadline
       ↓
branch cancelled
       ↓
reservation expires
       ↓
unused capacity released
       ↓
scheduler can admit another object
```

The scheduler is no longer just **budget-aware**.

It is now **failure-aware**.

That distinction becomes increasingly important as Orbital Watch moves from a small concurrent graph toward a production workload where dozens or hundreds of objects may be analyzed simultaneously.

## The Bigger Pattern

Day 25 establishes a general resource-management pattern:

```
             RESOURCE REQUEST
                    │
                    ▼
              ADMISSION CHECK
                    │
                    ▼
                 RESERVE
                    │
                    ▼
                  LEASE
                    │
              ┌─────┴─────┐
              │           │
              ▼           ▼
          EXECUTION     EXPIRY
              │           │
        ┌─────┴─────┐     │
        ▼           ▼     │
    COMPLETE    CANCEL    │
        │           │     │
        └─────┬─────┘     │
              │           │
              ▼           ▼
           RECONCILE ←────┘
              │
              ▼
            RELEASE
```

The same pattern applies beyond LLM token budgets:

```
LLM tokens
API rate limits
database connections
GPU slots
worker capacity
memory allocations
external service quotas
```

A production agent should rarely receive a resource with no expiry and no cleanup path.

## Testing the Invariant

The most important test is no longer simply:

```
Does the branch return the expected answer?
```

We also need:

```
Does every reservation eventually get released?
```

For example:

```
def test_timeout_releases_reservation():

    reservation = create_reservation(
        branch_id="researcher-1",
        tokens=800,
        timeout_seconds=1,
    )

    refund = force_reconcile(
        reserved_tokens=reservation["tokens"],
        known_actual_spend=100,
    )

    assert refund == 700
```

And the concurrency invariant:

```
def test_terminal_transition_is_idempotent():

    reservation = create_reservation(
        branch_id="researcher-1",
        tokens=800,
    )

    assert transition_to_terminal(
        reservation,
        "COMPLETED",
    )

    assert not transition_to_terminal(
        reservation,
        "EXPIRED",
    )

    assert reservation["status"] == "COMPLETED"
```

The second transition must not be allowed to reconcile the reservation again.

## What Day 25 Actually Adds

The progression is now:

```
Day 22
Fan-out / Fan-in
        ↓
Parallel work creates concurrency
        ↓
Day 23
Token budgets
        ↓
Parallel work needs spending limits
        ↓
Day 24
Reservations
        ↓
Concurrent work needs safe admission
        ↓
Day 25
Timeouts + Cancellation + Leases
        ↓
Admitted work needs bounded lifetime
```

Day 24 answered:

> **Can I safely admit this branch?**

Day 25 answers:

> **How long can this branch hold the resources I gave it?**

Together, they turn budget management from a static limit into a **lifecycle-aware resource-management system**.

## Next Steps

* [ ] Add a hard per-branch timeout around model/API execution
* [ ] Propagate cancellation to the underlying async worker instead of merely marking the branch as timed out
* [ ] Add reservation leases with `created_at` and `expires_at`
* [ ] Make reservation reconciliation idempotent so timeout and completion cannot refund the same reservation twice
* [ ] Add a watchdog that scans for expired reservations and force-reconciles them
* [ ] Add concurrency tests where completion and timeout happen at nearly the same time
* [ ] Test retries after timeout to ensure every retry performs a fresh admission/reservation check
* [ ] Wire timeout, cancellation, and lease events into the Day 21 tracing layer
* [ ] Apply the lease mechanism to Orbital Watch's priority-weighted scheduler
* [ ] Next: introduce **priority-aware scheduling** so limited resources are allocated to the most valuable work first
