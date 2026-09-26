# Day 62: Branch-Level Trust — Don't Let One Path Poison the Rest

## TL;DR
Day 61 scoped opacity down to individual branches, but trust (Day 59) is still tracked per dependent as a single aggregate score. That means a dependent with one permanently-opaque debug branch and five clean, fully analyzable branches has its trust dragged down by a path that has nothing to do with the others. Today's fix: key trust — and the read-set it gates — by `(dependent, branch fingerprint)` instead of by dependent alone, reusing Day 56's path fingerprints. A resume check only consults the trust and read-set for the specific branch that item's original execution actually took, so a dependent's well-behaved paths can earn full optimization independent of what an unrelated branch is doing.

## The Problem
- Day 59's trust score is one running number per dependent, built from reconciliation agreement across every call to that dependent regardless of which code path each call took. A branch that's hit once in a hundred calls contributes to the same aggregate as a branch hit constantly, with no distinction between them.
- Day 61's opacity is branch-scoped, but nothing downstream of that respects the distinction — an opaque branch's unresolvable mismatches (or a merely unreliable, not-yet-trusted branch) still get folded into the same aggregate score as the dependent's well-behaved branches, because `TRUST_SCORES` today is keyed purely by `dependent_id`.
- This means a dependent can never reach high trust on its safe branches if a rarely-hit but genuinely opaque branch keeps injecting disagreement into a shared score — the optimization Day 54 was built for stays unavailable even where it's fully earned, purely because of bookkeeping granularity rather than any real uncertainty about the safe branches.
- There's also a scoping mismatch worth noticing: Day 56 already records which branch a given execution took (the path fingerprint), and that fingerprint is available at commit time — but resume-time comparisons never look it up. The information needed to solve this already exists in the system; it just isn't being used at the point where trust and read-sets are consulted, which makes this less a new capability than a wiring fix across work already done.
- The problem gets worse the more branches a dependent has. A dependent with ten branches, one of which is opaque, effectively loses roughly a tenth of its useful trust signal to noise it has no way to isolate — and as Day 61 branch-level opacity detection gets adopted more widely, more dependents will end up in exactly this shape.

## Architecture

### 1. Record the branch taken alongside the consumed hash
At commit time, when a dependent records which upstream hash it consumed, it now also records the path fingerprint of the branch it actually took — the same fingerprint Day 56 already generates for coverage tracking. This is the missing link between existing instrumentation and the resume-time decision.

```python
def record_dependency(item_id, dependent_id, upstream_id, path_fingerprint, ledger):
    consumed_hash = scoped_hash_for_branch(dependent_id, path_fingerprint, upstream_id, ledger)
    ledger.record_dependency(item_id, dependent_id, upstream_id, consumed_hash, path_fingerprint)
```

### 2. Trust and read-sets keyed by (dependent, branch)
Instead of one aggregate score, `TRUST_SCORES` and `READ_SETS` are both re-keyed to include the branch fingerprint, so a mismatch on one branch only ever updates that branch's own trust — Day 60's diagnostic attribution logic is unchanged, it just now writes to a narrower key.

```python
def update_trust(dependent_id, path_fingerprint, agreed: bool):
    key = (dependent_id, path_fingerprint)
    score = TRUST_SCORES.setdefault(key, {"agree": 0, "total": 0})
    score["total"] += 1
    score["agree"] = score["agree"] + 1 if agreed else 0
```

### 3. Resume check consults only the taken branch
`is_resumable_for` looks up the branch fingerprint recorded for that specific item's original execution and uses only that branch's trust and read-set — not the dependent's aggregate state, and not any other branch's. This also folds in Day 61's opacity check directly at the branch level, since opacity and trust now share the same key shape.

```python
def is_resumable_for(item, dependent_id, ledger):
    for upstream_id, dep in item.dependencies[dependent_id].items():
        fp = dep.path_fingerprint
        if is_opaque(dependent_id, fp):
            current = content_hash(ledger.get(upstream_id).canonical)
        else:
            trusted = trust_gated_surface(dependent_id, fp)
            current = scoped_hash_for_branch(dependent_id, fp, upstream_id, ledger) if trusted \
                       else content_hash(ledger.get(upstream_id).canonical)
        if current != dep.consumed_hash:
            return False
    return True
```

### 4. New-branch bootstrapping
A branch fingerprint never seen before starts with zero trust and an empty read-set, independent of how trusted the dependent's other branches are — deliberately no cross-branch trust transfer yet, keeping this day's change additive rather than introducing a new, unverified generalization on top of it. This is a conscious scoping decision, not an oversight: mixing "we trust this dependent generally" into a newly-discovered branch's trust would reintroduce the exact poisoning problem this day exists to fix, just in the opposite direction.

```python
def trust_gated_surface(dependent_id, path_fingerprint, threshold=0.98, min_checks=30):
    score = TRUST_SCORES.get((dependent_id, path_fingerprint), {"agree": 0, "total": 0})
    if score["total"] < min_checks:
        return False
    return (score["agree"] / score["total"]) >= threshold
```

### 5. Backward compatibility for un-fingerprinted history
Trust and read-set data accumulated before this change was keyed only by `dependent_id`. Rather than discarding that history outright, it's migrated into a synthetic "unknown-branch" bucket that new, correctly-fingerprinted observations never write into — old aggregate trust doesn't get to silently vouch for a specific new branch, but it also isn't thrown away, in case a fallback path (point 4 of Day 56, dependents without branch hooks) still needs it.

```python
def migrate_legacy_trust(dependent_id):
    if dependent_id in LEGACY_TRUST_SCORES:
        TRUST_SCORES[(dependent_id, "unknown-branch")] = LEGACY_TRUST_SCORES.pop(dependent_id)
```

## Failure Modes
- **Branch cold starts multiply.** Every distinct branch fingerprint now needs its own trust history from scratch — a dependent with many branches pays Day 59's cold-start cost once per branch instead of once per dependent, which can mean most branches never individually accumulate enough calls to cross the confidence threshold, especially for branches that are each individually low-traffic even if the dependent as a whole is called frequently.
- **Fingerprint granularity tradeoff.** Too coarse a fingerprint merges genuinely different branches into one trust bucket, reintroducing exactly the poisoning problem this day is meant to fix; too fine explodes the number of tracked (dependent, branch) pairs, growing state faster than it's useful and multiplying the bookkeeping overhead flagged back in Day 61.
- **New coupling to the tracing subsystem.** Resume-time correctness now depends on the path fingerprint having been correctly recorded at commit time — if fingerprinting is unavailable or fails for a given call (e.g. a dependent without branch hooks, per Day 56's original limitation), there's no defined fallback yet beyond treating it as an ungated, always-full-hash branch, which is safe but silently forfeits the entire optimization for that call site.
- **No sharing across similar branches.** Two branches that are functionally near-identical (e.g. differing only in a log statement or an unrelated side branch that doesn't touch the tracked result) still build trust completely independently, which is safe but leaves obvious efficiency on the table — exactly the gap flagged for tomorrow.
- **Legacy migration ambiguity.** The "unknown-branch" bucket from point 5 is a rough approximation — it conflates whatever mix of branches contributed to the pre-migration aggregate, so its trust level may not accurately reflect any single branch's actual reliability, and it's unclear how long it should be kept around versus expired outright.

## What's Next
Day 63: branch-level cold starts are the direct cost of today's fix. Day 58 already showed that trust can generalize across *dependents* sharing similar code patterns (its feature-bucketing idea) — the natural next step is applying that same generalization within a single dependent, letting structurally similar branches share trust instead of each starting from zero.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
