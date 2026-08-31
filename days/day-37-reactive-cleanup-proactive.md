# Day 37: From Reactive Cleanup to Proactive Prevention

## TL;DR
Day 36 gave Orbital Watch a resource ledger and a reconciliation sweep to reclaim connections, locks, and rate-limit budget left behind by killed sub-agents. That's a safety net — it still lets leaks happen and then cleans them up after the fact. Today I moved the fix upstream: every resource a sub-agent acquires now comes with a hard TTL lease at acquisition time, so orphaned resources self-expire instead of waiting to be found.

## The Problem
- The reconciliation sweep (Day 36) only runs on an interval — leaked resources sit alive for up to a full sweep cycle before they're reclaimed.
- Sweep-based cleanup scales with *how many things leaked*, not with *how many things were acquired* — under heavy churn, the sweep itself becomes a bottleneck.
- A sweep can only reclaim what it recognizes as orphaned. Anything the ledger doesn't correctly attribute to a dead sub-agent just... stays leaked.
- Net effect: the system was always one step behind its own failures, cleaning up messes instead of preventing them.

## Architecture

### 1. Lease-based acquisition
Every resource request now goes through a lease manager instead of directly acquiring the resource. The lease carries a TTL set at acquisition time, scaled by severity tier (higher-severity sub-agents get longer leases since their work matters more).

```python
def acquire_resource(agent_id, resource_type, severity_tier):
    ttl = TTL_BY_SEVERITY[severity_tier]
    lease = lease_manager.create(
        owner=agent_id,
        resource_type=resource_type,
        expires_at=now() + ttl,
    )
    resource = resource_pool.checkout(resource_type)
    lease.bind(resource)
    return resource, lease
```

If the lease expires without renewal, the resource is automatically returned to the pool — no sweep required.

### 2. Heartbeat renewal for legitimate long-running work
Not every sub-agent finishes inside its base TTL. Healthy, still-working sub-agents renew their lease on a heartbeat rather than getting starved by a fixed window.

```python
def heartbeat(lease_id):
    lease = lease_manager.get(lease_id)
    if lease.is_alive():
        lease.extend(TTL_BY_SEVERITY[lease.severity_tier])
    else:
        raise LeaseExpiredError(lease_id)
```

A missed heartbeat is itself a signal — it usually means the sub-agent died or hung, which is exactly the case we want the lease to expire on.

### 3. Sweep demoted to safety net
The Day 36 reconciliation sweep didn't go away — it moved from primary mechanism to backstop. It now only needs to catch leases that somehow bypassed expiry logic (clock skew, crash mid-lease-write), which is a much smaller surface than "every orphaned resource in the system."

```python
def reconciliation_sweep():
    # now only catches leases the TTL mechanism itself failed to expire
    stale = lease_manager.find_expired_but_unreleased()
    for lease in stale:
        resource_pool.force_release(lease.resource)
        log_leak_prevented_by_sweep(lease)
```

## Failure Modes

- **Clock skew between the lease manager and sub-agent hosts.** A sub-agent that thinks it has 30 more seconds but the lease manager has already expired the lease will keep working against a resource that's been handed to someone else. Fix: lease manager is the single source of truth for time; sub-agents renew *before* their local estimate of expiry, with margin.
- **Thundering herd on expiry.** If many sub-agents were spawned in a burst (e.g., a severity spike), their leases expire in a burst too, and the resource pool sees a spike of simultaneous returns/reacquisitions. Added jitter to TTL assignment to spread expiry over time.
- **Renewal race conditions.** A heartbeat renewal arriving just as the lease manager is expiring the same lease can race — the sub-agent thinks it renewed, the manager thinks it expired and reassigned the resource. Fixed by making renewal a compare-and-swap against the lease's current expiry timestamp, not a blind extend.
- **Over-conservative TTLs killing legitimate slow work.** Early TTL values were tuned too tight for lower-severity sub-agents doing genuinely slow I/O-bound work, causing false expirations. Tuned per-tier TTLs against actual observed task duration distributions instead of guessing.

## What's Next
Next up: instrumenting the lease system itself — sweep-prevented-leak counts, lease expiry latency, and renewal race frequency, so I can see whether the proactive model is actually reducing sweep load or just moving the same failures somewhere less visible.

*Orbital Watch: watching the sky so the sub-agents don't have to watch each other.*
