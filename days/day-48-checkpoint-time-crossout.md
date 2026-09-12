# Day 48: Don't Trust the Resumable Flag — Verify It Against What Actually Happened

**TL;DR:** Day 47 gave every task a self-declared `resumable` flag so orphaned work could resume from checkpoint instead of restarting. But self-declaration has no teeth — a task can claim "safe to resume" while it quietly performed a non-idempotent side effect. Today's fix: an effect ledger that records every side-effecting call a task makes, tagged with an idempotency key where one exists. At checkpoint and resume time, the declared flag is cross-checked against the ledger — any effect without a registered idempotency key downgrades the task to non-resumable regardless of what it claimed.

## The Problem

- Day 47's `resumable` flag is set by the task author's intent at write time, not by anything the runtime observes
- A task can flip `resumable = true` and still make a call that isn't safe to repeat (e.g., a POST that isn't idempotent, a counter increment, a one-shot external notification)
- On a hard revoke + later resume, replaying from the last checkpoint would silently re-run that effect
- There was no mechanism catching the mismatch between "declared safe" and "actually safe" before Day 48
- Short tasks (which skip checkpointing per Day 47) are exempt, so the exposure is specifically the checkpointed long-running path

## Architecture

### 1. Effect ledger writes at the call site

Every outbound side-effecting call goes through a thin wrapper that logs to a local durable ledger before the call fires, tagged with an idempotency key when the callee supports one.

```python
def call_with_effect_log(task_id, effect_type, idempotency_key, fn, *args):
    ledger.record(
        task_id=task_id,
        effect_type=effect_type,
        idempotency_key=idempotency_key,  # None if the callee can't dedupe
        status="attempted",
    )
    result = fn(*args)
    ledger.update(task_id, effect_type, status="committed")
    return result
```

### 2. Checkpoint-time cross-check

At each checkpoint (same cadence as Day 47's periodic checkpointing), the runtime pulls the task's declared `resumable` flag and reconciles it against the ledger entries since the last checkpoint.

```python
def reconcile_resumability(task_id, declared_resumable):
    entries = ledger.since_last_checkpoint(task_id)
    unsafe = [e for e in entries if e.idempotency_key is None]
    if unsafe and declared_resumable:
        return False  # downgrade — declaration overridden by observed effects
    return declared_resumable
```

### 3. Resume-path enforcement

On a fresh grant within Day 47's grace window, the resumer reads the *reconciled* flag from the last checkpoint, not the task's live declaration. If the reconciled flag is `False`, the task restarts from scratch even if it still self-reports `resumable = true` in its current in-memory state.

```python
def resume_or_restart(task_id, checkpoint):
    if checkpoint.reconciled_resumable:
        return resume_from_checkpoint(checkpoint)
    return restart_from_scratch(task_id)
```

## Failure Modes

- **Idempotency key present but the callee doesn't honor it** — reconciliation trusts the key's existence, not the callee's actual dedup behavior; a callee that accepts a key and ignores it produces a false sense of safety. Mitigated only partially by scoping this to a small allowlist of verified-idempotent external APIs; unverified callees are treated as keyless.
- **Ledger write succeeds but the effect itself fails silently** — logging "attempted" before the call means a crash mid-call can leave a ledger entry with no matching "committed" update; reconciliation treats "attempted-but-not-committed" as unsafe by default, which is conservative but can over-restart tasks that actually completed.
- **High-frequency effects blow up ledger size** — a task making many small idempotent calls (e.g., per-item progress pings) logs an entry per call; needed a rollup so the ledger stores one entry per idempotency-key-namespace per task rather than one per call.

## What's Next

Day 49: the allowlist-of-verified-idempotent-callees approach doesn't scale — every new external integration needs manual verification before it can be trusted. Next is whether that verification can be partially automated (e.g., replay-testing a callee against a known key to confirm dedup behavior) instead of taking the callee's idempotency claim on faith the way Day 48 currently does for keyless calls.

*Orbital Watch: a multi-agent system watching the sky so you don't have to.*
