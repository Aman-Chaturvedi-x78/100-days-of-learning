# Day 71: Correlation-Aware Quorum — Two Votes Aren't Evidence If They're the Same Vote Twice

## TL;DR
Day 70's quorum assumes every registered corroboration source is observing an event independently, but nothing checks that assumption. If a secondary feed syncs from the same upstream as the primary rather than making its own independent observation, a quorum vote just counts one real signal twice and calls it agreement. Today's fix adds a correlation check between registered sources, built the same way Day 63 grouped code branches into families: by comparing historical agreement patterns, not by trusting a source's description of itself. Sources whose status reports move together too consistently across many independent events get flagged as correlated and collapsed into a single effective vote before quorum is computed.

## The Problem
- Day 70's `weighted_quorum` treats every registered source as an independent observation and sums (weighted) votes accordingly. Independence was assumed at registration time, not verified — nothing checks whether `fetch_swpc_mirror_alerts` is actually watching anything different from `fetch_noaa_active_alerts`, or whether it's simply relaying the same upstream feed with different latency.
- A mirror relationship between two sources isn't always obvious from how they're registered. A "secondary" source added specifically for redundancy might, in practice, derive from the primary's own published alert — technically a separate API, practically the same underlying signal, arriving a few minutes later.
- This defeats the entire purpose of Day 70's fix. A quorum of two correlated sources gives exactly the same false confidence as Day 69's single-source problem, just dressed up to look like agreement — two feeds confirming the same non-independent fact isn't stronger evidence than one feed confirming it once, but `weighted_quorum` currently can't tell the difference.
- The risk compounds with Day 70's reliability weighting (its point 4): if two correlated sources both happen to agree with the eventual quorum outcome (unsurprising, since they're the same signal), both accumulate reliability credit together, entrenching a false sense of redundancy rather than correcting it.

## Architecture

### 1. Pairwise agreement tracking
For every pair of sources registered against the same shape, track how often their reported status matches across resolved events — not just whether they currently agree, but whether that agreement holds up over many independent events, the same accumulate-before-trusting pattern used for branch families (Day 63) and analyzer trust (Day 59).

```python
def record_pairwise_agreement(shape, source_a, source_b, status_a, status_b):
    key = (shape, frozenset({source_a, source_b}))
    record = PAIRWISE_AGREEMENT.setdefault(key, {"agree": 0, "total": 0})
    record["total"] += 1
    if status_a == status_b:
        record["agree"] += 1
```

### 2. Correlation flagging
Two sources that agree far more often than their individual base rates would predict by chance are flagged as correlated. This reuses the same "earned, not assumed" posture as every trust mechanism in this arc — a correlation flag requires enough observed events to be statistically meaningful, not just two or three coincidental agreements.

```python
def is_correlated(shape, source_a, source_b, threshold=0.97, min_events=30):
    record = PAIRWISE_AGREEMENT.get((shape, frozenset({source_a, source_b})))
    if not record or record["total"] < min_events:
        return False  # not enough history to call it either way — conservative default
    return (record["agree"] / record["total"]) >= threshold
```

### 3. Collapsing correlated sources before quorum
Before computing a quorum vote, sources flagged as correlated for a shape are grouped, and each group contributes a single effective vote (its members' consensus, or the highest-weighted member's vote if they happen to disagree on a given event) rather than one vote per source. This is structurally the same move Day 63 made for branch families — pool evidence from things that behave identically, rather than letting apparent multiplicity stand in for actual independence.

```python
def effective_votes(shape, reports, source_reliabilities):
    groups = correlated_groups(shape)   # union-find over the is_correlated() pairs
    votes = []
    for group in groups:
        group_reports = [reports[s] for s in group]
        group_weight = max(source_reliabilities[s] for s in group)  # one vote, best-trusted member's weight
        votes.append((majority(group_reports), group_weight))
    return votes
```

### 4. Quorum computed on effective, not raw, votes
`resolve_corroboration_status` and `weighted_quorum` from Day 70 now operate on the collapsed vote set from point 3 rather than the raw per-source reports — correlated sources no longer inflate apparent agreement, and a shape with two correlated sources plus one genuinely independent one is correctly treated as having two effective votes, not three.

```python
def resolve_corroboration_status(shape, window):
    raw_reports = {s: source(window.start, window.end) for s, source in CORROBORATION_SOURCES.get(shape, {}).items()}
    votes = effective_votes(shape, raw_reports, SOURCE_RELIABILITY[shape])
    weighted_active = sum(w for v, w in votes if v)
    total_weight = sum(w for _, w in votes)
    return weighted_active / total_weight >= QUORUM_FRACTION if total_weight else None
```

## Failure Modes
- **Correlation detection needs the same volume of history it's meant to protect against being gamed by.** `min_events=30` means a newly registered source pair spends its first thirty events uncorrelated by default, during which a genuinely mirrored pair still inflates quorum confidence exactly as before — the fix only activates after enough evidence accumulates, which is the same cold-start tradeoff flagged for trust scoring back on Day 59, now recurring for source relationships instead of code branches.
- **High agreement isn't proof of correlation, and low agreement isn't proof of independence.** Two genuinely independent sources covering the same real, unambiguous events will naturally agree often, risking a false correlation flag that incorrectly collapses real redundancy into one vote. Conversely, two correlated sources with enough independent noise in their reporting pipelines (different polling intervals, different parsing quirks) might never cross the threshold, keeping a false sense of redundancy alive indefinitely.
- **Group weighting by the best member can mask a bad one.** Point 3's "highest-weighted member's vote" rule means a correlated group's vote is only as good as its best member, which is reasonable for weighting but means a consistently wrong secondary source sitting in the same group as a reliable primary never gets separately penalized — its errors are silently absorbed rather than surfaced.
- **This adds a third layer of accumulated trust state on top of two others.** Reliability weighting (Day 70) and pairwise correlation (today) are both earned through history, and both now interact when computing a single quorum vote — reasoning about why a particular event's regime was classified a particular way now requires tracing through two separate, independently-evolving trust mechanisms at once.
- **Correlation can change over time without re-triggering detection.** A secondary source that genuinely was independent at registration, but is later migrated by its provider to mirror the primary feed (a cost-cutting or consolidation decision on the provider's side, invisible to this system), will keep being treated as independent for as long as its historical agreement record from before the migration still dominates the rolling average.

## What's Next
This closes the corroboration-reliability arc that started with Day 66's single-source corroboration check: single source (66) → timeout-hardened single source (69) → multi-source quorum (70) → correlation-aware quorum (71). The open thread it leaves is the last failure mode above — correlation is measured as a lagging historical average, so a source relationship that changes underneath the system (a provider consolidating feeds, a mirror switching its origin) isn't detected until enough fresh, post-change history outweighs the old. The next step would need something more like Day 65's drift detection applied to the correlation signal itself — watching for a *shift* in how two sources agree, not just the long-run average.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
