# Day 63: Branch Families — Sharing Trust Between Branches That Read the Same Thing

## TL;DR
Day 62 keyed trust by `(dependent, branch fingerprint)`, which stopped one bad branch from poisoning the rest, but every branch now starts from zero trust on its own. A dependent with twelve branches that all read the same three fields pays Day 59's cold-start cost twelve times. Today's fix: group branches into families by structural equivalence of what they actually read from the tracked result, and pool reconciliation evidence across each family. Membership is strict (identical access signature, not "similar"), new members serve a probation period before inheriting family trust, and any disagreement inside a family resets it and ejects the offender.

## The Problem
- Per-branch trust (Day 62) is safe but slow. Every new path fingerprint needs its own 30+ reconciliation checks before the scoped hash is allowed to run, even when the branch is behaviorally identical to one that already earned trust.
- Many branches differ only in code that never touches the tracked result: a different log line, a different downstream call, a different retry policy. From the point of view of "which fields of the upstream result does this path read," they are the same branch.
- Low-traffic branches are hit hardest. A branch taken once in a few hundred calls will rarely reach `min_checks` on its own, so it stays on the full-hash path indefinitely even though a structurally identical sibling has already proven the pattern.
- The information to group branches already exists. Day 58's extractor produces a normalized access surface per code region, and Day 55/56 record runtime-observed reads per path fingerprint. Nothing compares branches to each other yet.

## Architecture

### 1. Family signature
A branch's signature is a hash over two things: its statically extracted access surface (Day 58, after normalization) and its runtime-observed read-set (Day 55). Two branches share a family only if both match exactly. Requiring both matters, because the static extractor is the component whose trustworthiness is being scored (see Failure Modes).

```python
def family_signature(dependent_id, path_fingerprint):
    static_surface = extract_branch_surface(dependent_id, path_fingerprint)   # Day 58
    observed = frozenset(READ_SETS.get((dependent_id, path_fingerprint), set()))  # Day 55
    return content_hash((static_surface, observed))
```

### 2. Trust keyed by family, evidence pooled
Reconciliation results from any member update the shared family score. The Day 62 per-branch key remains, but now points at a family bucket.

```python
BRANCH_FAMILY = {}   # (dependent_id, path_fingerprint) -> family_id
FAMILY_TRUST = {}    # (dependent_id, family_id) -> {"agree": int, "total": int}

def update_trust(dependent_id, path_fingerprint, agreed: bool):
    family = BRANCH_FAMILY[(dependent_id, path_fingerprint)]
    score = FAMILY_TRUST.setdefault((dependent_id, family), {"agree": 0, "total": 0})
    score["total"] += 1
    score["agree"] = score["agree"] + 1 if agreed else 0
```

### 3. Probation for new members
A newly seen branch whose signature matches an existing family does not inherit family trust immediately. It must first accumulate its own small number of agreeing reconciliations. This bounds the damage from a signature collision caused by an analyzer miss: a wrongly grouped branch gets caught by its own evidence before the family's trust can vouch for it.

```python
PROBATION_CHECKS = 5

def is_trusted(dependent_id, path_fingerprint, threshold=0.98, min_checks=30):
    if is_opaque(dependent_id, path_fingerprint):            # Day 61: never joins a family
        return False
    own = BRANCH_TRUST.get((dependent_id, path_fingerprint), {"agree": 0, "total": 0})
    if own["total"] < PROBATION_CHECKS or own["agree"] < own["total"]:
        return False
    family = BRANCH_FAMILY[(dependent_id, path_fingerprint)]
    fam = FAMILY_TRUST.get((dependent_id, family), {"agree": 0, "total": 0})
    return fam["total"] >= min_checks and fam["agree"] / fam["total"] >= threshold
```

### 4. Divergence: reset the family, eject the offender
If any member disagrees with reconciliation (after Day 60's attribution confirms it is an analyzer error rather than a tracking gap), the whole family's score resets, following Day 59's asymmetric rule. The offending branch is also ejected into a solo family that cannot rejoin unless its signature changes. A disagreement inside a family means the "these branches are equivalent" assumption was wrong for at least one member, so the other members' trust is suspect until re-earned.

```python
def on_confirmed_analyzer_error(dependent_id, path_fingerprint):
    family = BRANCH_FAMILY[(dependent_id, path_fingerprint)]
    FAMILY_TRUST[(dependent_id, family)] = {"agree": 0, "total": 0}
    solo = f"solo:{path_fingerprint}"
    BRANCH_FAMILY[(dependent_id, path_fingerprint)] = solo
    EJECTED.add((dependent_id, path_fingerprint, family_signature(dependent_id, path_fingerprint)))
```

### 5. Re-grouping on code change
Family membership is computed against a specific code version (Day 57) and access surface (Day 58). A redeploy that changes any member's signature triggers re-grouping for that dependent. Branches whose signatures no longer match their family leave it; they do not carry family trust with them.

## Failure Modes
- **Circular validation.** The signature depends on the static extractor, which is the thing trust is supposed to validate. If the extractor systematically misses one kind of dynamic access, two genuinely different branches can look identical and pool evidence. Including the runtime read-set in the signature and requiring probation both narrow this, but a miss that neither the extractor nor runtime tracking can see (Day 61 territory) still slips through.
- **Correlated evidence.** Pooled agreements are less independent than they look. Thirty agreeing checks spread across five sibling branches may reflect five hits on the same easy input shape, so family confidence can overstate how well any single branch has been exercised.
- **Blast radius of a reset.** One confirmed disagreement resets every member of a large family, so a single rare bad branch can knock a dozen healthy branches back to full-hash. This is deliberate, but it makes the cost of a false-positive attribution higher than it was under per-branch trust.
- **Strictness costs coverage.** Requiring exact signature equality means near-identical branches (one extra harmless field read) never share trust. Safe, but it leaves some cold-start savings unclaimed.
- **State growth.** Families, ejection records, and probation counters add another layer on top of path fingerprints, code versions, and access surfaces, and they need pruning when dependents are retired.

## What's Next
Day 64: trust is now earned per family and per code version on the consumer side, but nothing on the producer side can invalidate it. If a Day 52 projection schema is extended or a Day 53 adapter is added, the shape of what a dependent receives changes, yet trust earned under the old upstream behavior keeps applying. The open question is how trust should expire or re-validate when the upstream half of the pipeline changes underneath it.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
