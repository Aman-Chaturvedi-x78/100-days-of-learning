# Day 38: Heartbeat Storms — Why TTL Lease Renewal Needed Jitter

## TL;DR

Day 37 gave every acquired resource a self-expiring TTL lease with heartbeat renewal, so sub-agents no longer depended on the Day 36 sweep to clean up after them. It worked — until sub-agent counts scaled up during burst spawns. Then renewals synchronized into a thundering herd against the lease store, and the fix for one thundering-herd problem (orphaned resources) quietly created another (renewal storms). Fixed it with per-lease renewal jitter set once at acquisition, a short-window batching broker that coalesces renewals instead of hammering the store one call at a time, and a grace buffer so a slightly late renewal doesn't get mistaken for a dead sub-agent.

## Context

Quick recap of how we got here, since this day only makes sense against the last three:

- **Day 35** split teardown into a graceful drain/checkpoint/exit path and a hard-kill-and-salvage path, with severity-tiered grace periods.
- **Day 36** noticed that even graceful teardown sometimes leaves connections, locks, and rate-limit budget behind — so it added a resource ledger and a reconciliation sweep to reclaim orphaned resources after the fact.
- **Day 37** asked: why clean up after the fact at all? It gave every resource a TTL lease at acquisition time, scaled by severity tier, with heartbeat renewal keeping a live sub-agent's lease alive. The Day 36 sweep got demoted to a backstop instead of the primary mechanism.

Day 37 felt like the right endpoint. It wasn't quite — it just moved the failure mode somewhere new.

## The Problem

- Sub-agents spawned in the same wave — say, a fast-escalating solar wind event that fires off 40+ severity-tiered sub-agents in a couple hundred milliseconds — all inherited nearly identical base TTLs. Their heartbeat renewals landed in the same narrow window, cycle after cycle, because nothing broke the symmetry between them.
- Under burst spawn, all of those renewals piled onto the lease store at once. p99 renewal latency spiked hard, and some renewals timed out outright under the contention.
- Here's the part that actually hurt: a renewal that timed out because the store was busy was indistinguishable, from the watchdog's point of view, from a genuinely dead sub-agent. The lease expired on schedule, and the watchdog killed a perfectly healthy agent — a false-positive teardown, caused entirely by our own cleanup mechanism.
- Worse, retried renewals after a timeout landed right back in the same congested window on the very next cycle, instead of relieving the pressure. The storm was self-reinforcing.

This is the kind of failure mode that doesn't show up in low-concurrency testing. Everything looked fine with a handful of sub-agents. It only appeared once spawn volume crossed a threshold — which is exactly when you can least afford agents dying for no reason.

## Architecture

### 1. Per-lease renewal jitter, fixed at acquisition

Each lease gets a random offset applied once, at the moment it's acquired — not recalculated on every renewal cycle. That distinction turned out to matter a lot (see Failure Modes below): recalculating jitter on each renewal let leases slowly drift back into sync over many cycles, which defeats the entire point.

```python
renewal_interval = base_interval * (1 + random.uniform(-0.15, 0.15))
next_renewal_at = acquired_at + renewal_interval  # offset locked in once, at acquisition
```

The jitter band (±15-20%) was picked empirically — wide enough to meaningfully spread renewals across the base interval, narrow enough that no individual sub-agent's effective TTL drifts far from its severity tier's intended budget.

### 2. Renewal batching / coalescing

Jitter alone spreads out *when* each renewal call fires, but it doesn't reduce the number of round trips to the store — and it turned out the store's per-call round-trip cost, not raw call volume, was the actual bottleneck. So a lightweight renewal broker now sits between sub-agents and the lease store: it collects all renewals that come due within a short window and coalesces them into a single multi-key request.

```python
# broker drains all renewals due within the current window, then issues one batched call
due = renewal_queue.drain(window_ms=250)
lease_store.renew_batch([r.lease_id for r in due])
```

250ms was a deliberate middle ground — long enough to meaningfully batch renewals together, short enough that it doesn't itself become a source of delay for genuinely expiring leases. More on why that number matters below.

### 3. Grace buffer before hard expiry

Even with jitter and batching, some renewal will occasionally land late under load. Rather than treat lateness as death, there's now a short buffer between "the renewal window closed" and "the lease actually expires." A renewal that arrives within that buffer still counts.

```python
if now < lease.expires_at + GRACE_BUFFER:
    accept_late_renewal(lease)
else:
    mark_expired(lease)
```

This is the safety net underneath the other two fixes — jitter and batching reduce *how often* renewals are late, but the grace buffer is what stops "late" from meaning "dead."

## Failure Modes

- **Jitter alone wasn't enough.** The first pass only added jitter and left renewal calls unbatched. It measurably reduced how many renewals landed in the exact same tick, but p99 latency barely moved — because the store's per-call round-trip cost, not simultaneous call volume, was the real constraint. Needed batching on top of, not instead of, jitter.
- **Coalescing window too wide.** An early version of the broker batched everything within a full 1-second window to maximize coalescing efficiency. That backfired: it delayed real expirations by up to a second, which quietly undermines the whole point of proactive, self-expiring leases from Day 37 — a dead sub-agent's resources would sit unreclaimed for longer than intended.
- **Jitter recomputed on renewal.** The very first implementation recalculated the jitter offset fresh on every renewal cycle instead of fixing it once at acquisition. Over enough cycles, random recalculation statistically pulled leases back toward clustering — the exact synchronization problem the jitter was supposed to prevent. Locking the offset at acquisition time fixed it permanently.
- **Grace buffer masking a real problem.** Early on there was a temptation to just widen the grace buffer generously and call it done. Resisted that — a grace buffer that's too generous stops distinguishing "briefly delayed" from "actually gone," which reopens the exact false-negative risk that TTL leases exist to close in the first place. It's a buffer against store jitter, not a substitute for the watchdog actually working.

## What's Next

Two directions worth exploring for Day 39:

- Whether the lease store itself needs sharding or partitioning as sub-agent counts keep climbing — batching buys headroom, but it doesn't scale indefinitely on a single store.
- Whether adaptive base intervals per severity tier (rather than a fixed per-tier TTL) could reduce contention further upstream, before renewals even reach the broker.

*Orbital Watch: watching the sky, one resilient agent at a time.*
