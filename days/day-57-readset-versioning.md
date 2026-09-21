# Day 57: Read-Set Versioning — Learning Doesn't Survive a Redeploy

## TL;DR
Day 56's coverage confidence assumes a dependent's own code stays fixed while its read-set is being learned. But a redeploy can change which fields a dependent reads without touching anything upstream — silently invalidating every access log and coverage score accumulated so far. Today's fix: bind each read-set and its coverage state to a content hash of the dependent's own code, so any redeploy forces a clean re-learning cycle instead of trusting observations that no longer reflect reality. This closes the read-set arc that started on Day 54: declared, then observed, then confidence-scored, now versioned.

## The Problem
- Read-set accumulation (Day 55) and coverage confidence (Day 56) are both keyed on `(operation_shape, dependent_id)` — nothing about *which version* of the dependent's code produced those observations. Two deploys of the same dependent, months apart and behaviorally quite different, look identical to the tracking system.
- A redeploy that changes what fields a dependent reads — adds a new field access, drops an old one, changes a branch's condition so a previously-rare path becomes common (or vice versa) — doesn't touch the upstream operation at all, so nothing in the current design has any signal that it happened.
- The result: a read-set learned under old code keeps being trusted under new code, potentially missing a field the new code depends on. That's a silent false negative — the same class of failure flagged since Day 52's projection schemas, now recurring one layer over, on the consumer side instead of the producer side.
- This risk compounds with Day 56's confidence mechanism specifically: a dependent that had *already* crossed the confidence threshold under old code keeps its scoped-hash fast path active immediately after redeploy, with zero re-verification — the system's most "trusted" dependents are, ironically, the ones most exposed to this gap, because trust was earned under conditions that no longer hold.

## Architecture

### 1. Dependent code fingerprint
Hash the dependent's deployed code/config, reusing the content-hash machinery from Day 51, as a versioning key alongside its operation shape.

```python
def dependent_version(dependent_id):
    return content_hash(load_dependent_code(dependent_id))
```

### 2. Read-set keyed by (dependent, code version)
Accumulated access logs, path fingerprints, and coverage state are all stored per code version, not per dependent identity alone — a redeploy naturally produces a fresh key rather than overwriting or silently continuing to accumulate into stale state.

```python
def record_run(dependent_id, code_version, operation_shape, access_log, path_fingerprint):
    key = (operation_shape, dependent_id, code_version)
    READ_SETS[key] |= access_log
    PATH_FINGERPRINTS.setdefault(key, set()).add(path_fingerprint)
```

### 3. Invalidation on redeploy
When a new code version is observed for a dependent, its prior read-set and coverage state are retired rather than reused — the dependent restarts at Day 56's conservative full-hash fallback until it re-earns coverage confidence under the new code. Detection happens lazily, at the first run after redeploy, rather than requiring an explicit deploy-time notification.

```python
def is_resumable_for(item, dependent_id, ledger):
    version = dependent_version(dependent_id)
    key = (item.operation_shape, dependent_id, version)
    confident = coverage_confidence(key, runs_since_new_path.get(key, 0))
    for upstream_id, consumed_hash in item.dependencies.items():
        shape = ledger.get(upstream_id).operation_shape
        canonical = ledger.get(upstream_id).canonical
        current = (scoped_hash(shape, dependent_id, canonical) if confident
                    else content_hash(canonical))
        if current != consumed_hash:
            return False
    return True
```

### 4. Prior-version retention for audit, not reuse
Retired read-sets aren't deleted outright — they're kept, tagged by their code version, purely for debugging (e.g. comparing what a dependent read before vs. after a suspicious deploy) the same way Day 52 kept raw results alongside projected hashes for audit purposes. They're never consulted for resume decisions once superseded.

```python
def retire_version(dependent_id, old_version):
    archived = {k: v for k, v in READ_SETS.items() if k[1:] == (dependent_id, old_version)}
    READ_SET_ARCHIVE[old_version] = archived
```

## Failure Modes
- **Re-learning cost on every deploy.** Each redeploy pays a temporary tax of full-hash comparisons until confidence is re-earned — for dependents deployed frequently (multiple times a day via CI/CD), this can mean the scoped-hash optimization from Day 54 is rarely active at all, undermining the entire point of the last four days' work for exactly the dependents under the most active development.
- **Coarse versioning.** A code hash changes on any diff, including a whitespace-only or comment-only change that doesn't affect field access — the same raw-vs-semantic distinction Day 52 solved for upstream results hasn't been applied to the dependent's own code yet, so a purely cosmetic commit costs the same re-learning cycle as a genuine logic change.
- **Retired-state cost.** Discarding rather than merging prior read-sets on redeploy is the conservative choice, but it throws away potentially-still-valid observations for fields the new code didn't actually change how it reads — a large refactor that touches one branch but leaves ninety percent of the dependent's logic untouched still pays a full re-learning cost for the untouched ninety percent.
- **Archive growth.** Retaining every prior version's read-set for audit purposes (point 4) means storage grows unboundedly for dependents with frequent deploys — nothing today prunes old archived versions, which will eventually need its own retention policy.

## What's Next
This closes the read-set line of work: declared (Day 54) → observed (Day 55) → confidence-scored (Day 56) → versioned (Day 57). The open thread it leaves behind mirrors where this whole stretch started — Day 52 solved raw-vs-semantic hashing for upstream *results*; the same problem now exists one layer over, for the dependent's own *code*. A byte-level code hash can't tell a logic change from a formatting change, so every redeploy pays the same re-learning cost regardless of whether the change could ever have affected what fields get read.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
