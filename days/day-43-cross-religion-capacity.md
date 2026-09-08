# Day 43: Cross-Region Capacity — When Every Region Wants to Split at Once

## TL;DR
Day 42's global migration admission controller solved split storms *within* a region. But Orbital Watch is now federating across regions, and a single-region controller is blind to what's happening everywhere else. During a genuinely global space weather event, every region's admission controller independently decides it's under contention and starts approving splits at its own local max rate — with no idea whether a neighboring region actually has spare migration capacity sitting idle. Today: a federation layer that lets regions borrow capacity from each other without becoming a single point of failure.

## The Problem
- The Day 42 admission controller is scoped to one region's lease store and one region's migration slot pool — it optimizes locally, not globally.
- A global storm doesn't hit all regions with the same severity at the same time. Region A might be saturated while Region B, further from the event's ground track, has spare migration slots sitting unused.
- Wall-clock priority aging (from Day 42) doesn't compare cleanly across regions — clock skew and independent local queues mean "priority age" in Region A isn't commensurable with Region B's.
- A naive fix — merge all regions into one global queue — reintroduces the single point of failure and cross-region latency problems the federation was designed to avoid in the first place.

## Architecture

### 1. Region-local admission stays the fast path
Every region keeps Day 42's admission controller exactly as-is for the common case. Nothing changes for a region operating alone — split requests are scored and granted locally with no cross-region round trip on the hot path.

```python
def request_split_slot(shard_id, priority_score):
    # unchanged from Day 42 — local-first, no blocking on federation
    return local_admission_controller.enqueue(shard_id, priority_score)
```

### 2. Lightweight capacity gossip between regions
Each region periodically broadcasts a compact summary — not individual requests, just aggregate state: current queue depth, spare slot count, and the highest pending priority score. This runs on a slow heartbeat (seconds, not milliseconds), so it never sits on the critical path of an actual split decision.

```python
def broadcast_capacity_summary():
    summary = {
        "region": REGION_ID,
        "queue_depth": local_admission_controller.queue_depth(),
        "spare_slots": local_admission_controller.spare_capacity(),
        "max_pending_priority": local_admission_controller.top_priority(),
        "ts": monotonic_region_clock(),
    }
    gossip_broadcast(summary)
```

### 3. Slot borrowing with TTL-bound loans
When a region's local queue is saturated *and* it sees a peer advertising spare slots, it can request a loan — a fixed batch of migration slots temporarily reassigned to it. Loans carry a TTL, same pattern as Day 37's resource leases: if the loan isn't renewed, it auto-expires and reverts to the lender rather than requiring an explicit release message that might get lost.

```python
def request_loan(from_region, slot_count, ttl_seconds=60):
    loan = lender_api(from_region).offer_loan(slot_count, ttl_seconds)
    if loan:
        local_admission_controller.add_borrowed_capacity(loan)
    return loan
```

### 4. Region-normalized priority instead of wall-clock aging
To make priority scores comparable across regions, aging is expressed relative to each region's own local baseline queue-wait distribution rather than raw elapsed time. A request that's waited "twice the local median" carries the same federated weight whether the local median is 2 seconds or 20.

## Failure Modes
- **Gossip network partition mid-event**: a region stops hearing from peers during exactly the window it most needs to borrow capacity. Fix: on gossip staleness beyond a threshold, a region falls back to local-only admission with a conservative cap — deny-by-default beyond a safe local ceiling rather than assuming peers have spare capacity that may not exist.
- **Unreturned loans stacking up**: a borrowing region under sustained load keeps renewing loans and never lets go, silently starving the lender when the lender's own load picks up later. Fix: loans have a hard maximum renewal count; past that, the lender's local admission controller gets priority over any outstanding loan regardless of the borrower's pending queue.
- **Clock skew breaking region-normalized aging**: if a region's local clock is meaningfully wrong, its "relative to local median" priority signal becomes unreliable and can either starve or over-prioritize its requests in the eyes of peers. Fix: gossip summaries are validated against peer-reported timestamps with an outlier check — a region whose reported timing is inconsistent with the group is treated as gossip-stale and excluded from lending/borrowing until it re-syncs.
- **Thundering herd on the lender**: multiple starved regions request loans from the same spare-capacity region at once, oversubscribing it. Fix: the lender rate-limits loan grants per gossip interval and applies the same priority-scoring logic from Day 42 to incoming loan requests, not just its own local queue.

## What's Next
Day 44's open question: the federation layer assumes gossip latency is roughly symmetric between regions, but real inter-region links aren't — a loan negotiated over a 200ms link behaves very differently under time pressure than one over a 20ms link. Next up: making loan TTLs and borrowing decisions latency-aware instead of using a fixed TTL for every region pair.

---
*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
