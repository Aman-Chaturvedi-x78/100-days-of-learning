# Day 47: What Happens to the Work That Was Mid-Flight

**TL;DR:** Day 46 gave hard-revoked loans a safe failure path — reclaim capacity, mark the region suspect, move on. What it didn't answer: the sub-agent work that was actually running on that loan when the revoke fired doesn't just vanish. Today's decision: periodic lightweight checkpointing during execution, a bounded grace window to resume from that checkpoint if the region comes back quickly, and an explicit fallback to restart-from-scratch when it doesn't — gated by whether the task can even claim it's safe to resume.

## The Problem

- Day 46's `notify_revoked` was a hook with no real body. The loan dies cleanly; the work sitting on top of it doesn't.
- Naive option A, always resume from wherever the work left off: fine if the sub-agent's work is idempotent, actively wrong if it isn't. A task that's already written a partial result somewhere and gets replayed from an earlier point can double-apply side effects.
- Naive option B, always restart from scratch on any hard revoke: safe, but throws away real progress on tasks that might have been seconds from finishing when a transient region blip happened. Given Day 46's disambiguation already exists specifically to avoid over-reacting to transient link issues, discarding all in-flight work unconditionally undoes some of that care.
- The actual fork isn't "resume or restart" — it's "can this specific task's state be resumed safely, and is the region back soon enough for that to matter."

## Architecture

1. **Sub-agents checkpoint periodically, not just at revoke time**

```python
class SubAgentCheckpoint:
    def __init__(self, task_id, loan_id, resumable):
        self.task_id = task_id
        self.loan_id = loan_id
        self.state = None
        self.resumable = resumable  # task declares this up front, not inferred later
        self.last_checkpoint_at = None

    def save(self, state):
        # written to LOCAL durable storage, not the peer region —
        # the whole point is surviving that region being unreachable
        local_checkpoint_store.write(self.task_id, state)
        self.state = state
        self.last_checkpoint_at = now()
```

2. **Hard revoke marks the task orphaned instead of discarding it immediately**

```python
def notify_revoked(self, loan):
    for task in tasks_on_loan(loan):
        task.status = "orphaned"
        task.orphaned_at = now()
        # last checkpoint (if any) stays in local_checkpoint_store — untouched
```

3. **A bounded grace window decides whether resume is even on the table**

```python
GRACE_WINDOW = timedelta(seconds=90)  # tuned against Day 46's suspect-state clearing latency

def resolve_orphaned_task(task):
    if now() - task.orphaned_at > GRACE_WINDOW:
        return restart_from_scratch(task)

    if not region_registry.is_suspect(task.original_region):
        # region cleared "suspect" within the window — worth trying to resume
        if task.checkpoint.resumable and task.checkpoint.state is not None:
            return resume_from_checkpoint(task)

    return restart_from_scratch(task)
```

4. **Resume gets a fresh loan, not a regrant of the dead one**

```python
def resume_from_checkpoint(task):
    # the old loan is gone — Day 46's revoke was final, not paused.
    # this goes through normal admission, same priority as the original request
    new_loan = admission_controller.request(
        region=task.original_region,
        priority=task.original_priority,
    )
    return execution_engine.resume(task, task.checkpoint.state, new_loan)
```

## Failure Modes

- **Checkpointing overhead on the hot path** — writing state on every step is wasted cost for short tasks that would restart cheaply anyway. Checkpoint frequency is scaled to task duration: short tasks skip checkpointing entirely and fall straight to `restart_from_scratch` logic if orphaned; only tasks running long enough to make restart expensive checkpoint at all.
- **A task lying about `resumable`** — nothing today verifies a self-declared resumability flag against what the task's side effects actually did. A task that claims resumable but wrote a non-idempotent side effect before its last checkpoint would get silently double-applied on resume. This is a real gap, not a solved one — see below.
- **Grace window mismatched against Day 46's suspect-state clearing time** — too short, and everything restarts from scratch even for the transient blips Day 46's relay disambiguation was built to tolerate. Too long, and orphaned tasks hold local checkpoint storage and borrower-side state for longer than useful. Tuned against Day 46's numbers, not derived independently.
- **Local checkpoint store surviving the same outage** — the whole design leans on writing checkpoints locally so they survive the peer region being unreachable. If local storage itself is degraded at the same time, there's no checkpoint to resume from regardless of grace window — falls straight to restart.

## What's Next

Day 47 trusts a task's self-declared `resumable` flag. Day 48's question: how does the framework verify that a task claiming resumability is actually safe to resume, rather than taking its word for it — and what happens to the ones that turn out to have lied?

*Orbital Watch: a multi-agent system slowly learning that most distributed systems problems are actually the same problem wearing a different hat.*
