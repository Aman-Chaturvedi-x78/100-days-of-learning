# Day 64: Producer-Side Trust Invalidation — Trust Earned Under One Upstream Isn't Valid Under Another

## TL;DR
Trust in the read-set pipeline now expires when the consumer changes (Day 57 code versions, Day 58 access surfaces, Day 63 family signatures), but nothing expires it when the producer half of the pipeline changes. Extend a Day 52 projection schema or add or edit a Day 53 canonicalization adapter, and every trust score, read-set, and recorded consumed hash was earned or computed against a different definition of "the canonical result." Today's fix stamps trust and consumed hashes with an upstream epoch, classifies each upstream change by whether it alters canonical output for data already in the ledger (checked by replaying retained raw results), and rehashes recorded dependencies in place instead of letting an epoch change look like drift.

## The Problem
- Trust scores (Days 59-63) and read-sets (Days 54-56) are measured against the canonical, projected result of an upstream operation. That result is a function of two registries: the Day 52 projection schema (which fields are stripped) and the Day 53 adapter set (how shapes map to canonical form). Both keep changing by design, since projection promotion is continuous and adapters are added as new shapes appear.
- Nothing ties trust to the registry state it was earned under. A dependent that reached the trust threshold when field `velocity_covariance` was part of the hash keeps that trust after Day 52 promotes the field into the strip-list, even though its scoped hash now silently ignores the field.
- That is a concrete interaction bug, not just staleness. Day 52 only promotes a field after replay evidence shows harmless drift, but the evidence covers dependents and branches that existed during the observation window. A branch that starts reading the field afterwards was never part of the evidence, so its read-set can include a field the projection removes, and its scoped hash under-triggers with full confidence.
- The second problem is the reverse. Every recorded consumed hash was computed under the old registries. When the epoch changes, recomputing the current hash under the new registries produces a mismatch for every dependency in the ledger, and Day 51's topological resume reads that as drift and cascades re-execution through everything. A bookkeeping change looks identical to a data change.

## Architecture

### 1. Upstream epoch
Each operation shape gets an epoch: a hash of the projection schema entry and the ordered adapter list that apply to it. Any trust record, read-set entry, or consumed hash is stored with the epoch it was computed under.

```python
def upstream_epoch(operation_shape):
    return content_hash((
        PROJECTION_SCHEMAS.get(operation_shape),
        [adapter_id(a) for a in CANONICAL_ADAPTERS.get(operation_shape, [])],
    ))

def record_dependency(item_id, dependent_id, upstream_id, path_fingerprint, ledger):
    shape = ledger.get(upstream_id).operation_shape
    epoch = upstream_epoch(shape)
    consumed = scoped_hash_for_branch(dependent_id, path_fingerprint, upstream_id, ledger)
    ledger.record_dependency(item_id, dependent_id, upstream_id, consumed, path_fingerprint, epoch)
```

### 2. Classify the change by replaying retained raw results
The effect ledger keeps raw results alongside canonical and projected hashes (Day 52). On an epoch change, recompute canonical and projected forms for a sample of stored raw results under the new registries and diff them against what was stored. The outcome sorts the change into one of three classes.

```python
def classify_epoch_change(shape, old_epoch, new_epoch, ledger, sample_size=200):
    changed_fields = set()
    for raw in ledger.sample_raw(shape, sample_size):
        old = ledger.stored_projection(raw)
        new = project(shape, canonicalize(shape, raw))
        changed_fields |= diff_fields(old, new)
    if not changed_fields:
        return "preserving", set()      # additive adapter, disjoint discriminator
    return "altering", changed_fields    # canonical output changed for stored data
```

An additive adapter with a discriminator that matches no previously stored shape lands in "preserving." A strip-list addition or an adapter edit lands in "altering," and the set of changed fields says exactly which ones.

### 3. Preserving changes carry trust forward
If replay shows byte-identical output, trust and read-sets are re-stamped with the new epoch and nothing else happens. New shapes get their own cold start, existing shapes keep what they earned.

### 4. Altering changes intersect with read-sets
For an altering change, take the changed field set and intersect it with the read-set of every branch and family that consumes this shape. Only intersecting branches lose trust. A dependent whose branches never read the changed fields keeps everything.

```python
def apply_altering_change(shape, changed_fields, new_epoch):
    for (dep, fp), read_set in READ_SETS_FOR_SHAPE(shape):
        if read_set & changed_fields:
            quarantine(dep, fp)              # full-hash fallback, probation restarts
            if field_now_stripped(shape, read_set & changed_fields):
                flag_projection_conflict(shape, dep, fp)   # promotion contradicts a live reader
        else:
            restamp_trust(dep, fp, new_epoch)
```

A projection conflict means Day 52's promotion was made on evidence that did not include this reader. The flag pins that dependent branch to raw-field hashing for the conflicting field and queues the promotion for re-evaluation.

### 5. Rehash in place instead of cascading
Consumed hashes recorded under the old epoch are never compared against hashes from the new epoch. When resume needs a comparison and the epochs differ, the old-epoch hash is recomputed from the stored raw result under the new registries, and the comparison runs on equal terms. Drift then means the data changed, not the rules.

```python
def resolve_consumed_hash(dep_record, ledger):
    current_epoch = upstream_epoch(dep_record.shape)
    if dep_record.epoch == current_epoch:
        return dep_record.consumed_hash
    raw = ledger.get_raw(dep_record.upstream_id)
    if raw is None:
        return None            # raw pruned: cannot rehash, caller falls back to conservative cascade
    return scoped_hash_from_raw(dep_record, raw)
```

## Failure Modes
- **Raw retention is the load-bearing assumption.** Rehash-in-place and replay classification both need retained raw results. Where raw data has been pruned, the system cannot distinguish a rules change from a data change and falls back to the conservative cascade, which is the exact spurious cascade this design exists to avoid.
- **Sampling can miss an altering change.** Classification diffs a sample of stored results. A change that only affects a rare shape variant can look "preserving" when the sample happens to contain none of the affected records. Sample stratification by shape variant reduces this, but does not remove it.
- **Epoch churn.** Day 52 promotes fields continuously, so busy operation shapes can change epoch often. Each change costs a replay pass and possible quarantines. Nothing rate-limits promotion against the invalidation cost it now causes.
- **Two-axis key growth.** Trust is now effectively keyed by consumer version (Day 57), access surface (Day 58), family (Day 63), and producer epoch. Every axis multiplies the number of states that need bookkeeping and pruning.
- **Quarantine re-imposes cold starts.** An altering change that touches a widely read field quarantines many branches at once, and they all go back to full-hash comparison together. The optimization is gone system-wide for that shape until families re-earn trust, which is the Day 62-63 cold-start cost again, triggered from the other side.

## What's Next
Day 65: every mechanism so far reacts to a change the system can see, whether that is a code diff, a schema edit, or a registry update. A provider can change what it returns without touching any of those. If an upstream API starts sending a field in a slightly different unit or begins populating a previously null field, no epoch bumps and no version changes, and trusted branches keep using a scoped hash earned under the old behavior. The open question is how trust should be audited when the trigger is silent.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
