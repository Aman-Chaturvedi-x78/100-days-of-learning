# Day 49: Automating Idempotency Verification Instead of Hand-Auditing an Allowlist

**TL;DR:** Day 48's effect ledger only trusts a callee's self-declared "resumable" flag if every side-effecting call it made carried an idempotency key — verified against an allowlist of known-idempotent callees. That allowlist was manually audited per integration. Day 49 replaces the manual audit with a tiered auto-classification pipeline, falling back to non-resumable (never assuming idempotency) wherever evidence is missing.

## The Problem

- Manual allowlist verification doesn't scale as Orbital Watch adds more downstream integrations (NASA/NOAA/CelesTrak endpoints, internal microservices, third-party alert relays).
- Every new callee is a human bottleneck: someone has to read the API docs, confirm idempotent behavior, and add it to the allowlist before the effect ledger will trust it.
- Manual audits go stale — a callee's behavior can change (a PUT becomes a PATCH, a "safe" retry starts double-charging) with nothing to catch the drift.
- Defaulting new/unaudited callees to non-resumable is safe but means resumability coverage lags behind integration growth indefinitely.

## Architecture

### 1. Static Signal Classification

Inspect callee metadata at registration time — HTTP method semantics, explicit idempotency-key header support — and auto-verify without human sign-off.

```python
def classify_static(callee_metadata):
    if callee_metadata.method in ("GET", "HEAD"):
        return Verdict.IDEMPOTENT
    if callee_metadata.supports_idempotency_key:
        return Verdict.IDEMPOTENT
    if callee_metadata.method == "PUT" and callee_metadata.has_resource_key:
        return Verdict.IDEMPOTENT
    return Verdict.UNKNOWN
```

### 2. Contract-Declared Classification

For internal services, an interface-level annotation shifts verification cost to the service owner once, at declaration time, instead of re-verifying per caller.

```python
@idempotent(key_param="request_id")
def submit_alert(request_id: str, payload: dict):
    ...

# registered once when the service starts, trusted by every caller after
registry.register(submit_alert, source=Source.CONTRACT_DECLARED)
```

### 3. Unverifiable Fallback

No static signal, no contract declaration → stays non-resumable, identical to Day 48's default. Automation only expands the allowlist; it never assumes idempotency in the absence of evidence.

```python
def resolve_verdict(callee):
    verdict = classify_static(callee.metadata)
    if verdict == Verdict.UNKNOWN:
        verdict = registry.lookup(callee, source=Source.CONTRACT_DECLARED)
    return verdict or Verdict.NON_IDEMPOTENT  # fail closed
```

### 4. Drift Detection

Periodically replay observed retry outcomes against the declared contract; auto-revoke on mismatch and flag for the service owner.

```python
def check_drift(callee, observed_calls):
    for call in observed_calls:
        if call.retry_count > 0 and call.side_effect_diverged:
            registry.revoke(callee, reason="observed non-idempotent retry")
            alert_owner(callee)
            return
```

## Failure Modes

- **Static signals can lie.** A `PUT` endpoint that looks idempotent by REST convention but has server-side side effects (e.g., triggers a downstream notification) will be misclassified — this is exactly what the drift check exists to catch, but there's a window between misclassification and detection.
- **Contract declarations rot.** A service owner adds `@idempotent` correctly, then later changes the implementation without updating the annotation — the registry trusts a stale declaration until drift detection notices behavioral divergence.
- **Drift detection needs volume.** Low-traffic callees may not generate enough retry observations to ever trigger a drift check, leaving misclassifications undetected indefinitely for rarely-called integrations.
- **Revocation is blunt.** Auto-revoking a callee on drift downgrades *all* of its operations to non-resumable, even if only one code path actually misbehaved — related to the open question below.

## What's Next

Day 50: a callee isn't always uniformly idempotent — a batch endpoint might safely retry for most items but fail idempotently for one edge case. The allowlist (and now the classification pipeline) operates per-callee, not per-operation-shape. Need to figure out what a finer-grained verdict looks like without exploding the classification surface area.

*Orbital Watch: teaching a multi-agent system to watch the sky without losing track of itself.*
