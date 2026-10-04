# Day 70: Corroboration Quorum — Stop Trusting One Source's Word for It

## TL;DR
Day 69's timeout logic treats a single corroboration source's flakiness as something to manage with backoff and provisional guesses. But the deeper problem is architectural: regime classification for a whole shape hangs on one external feed's status, so that feed's own reliability ceiling becomes the ceiling for every mechanism built on top of it since Day 66. Today's fix replaces single-source corroboration with a quorum across multiple independent sources where more than one exists, requiring agreement rather than trusting any single feed's say-so, and falls back to Day 69's existing timeout machinery only for shapes where no redundancy is available at all.

## The Problem
- `CORROBORATION_SOURCES` (Day 66) maps each operation shape to exactly one function. Every downstream mechanism — corroborated-drift logic, regime labeling, hysteresis, timeout resolution — ultimately asks that one source a yes/no question and builds elaborate handling around the answer being unreliable, rather than questioning why there's only one answer to ask for.
- This matters specifically because the sources in question are third-party feeds (NOAA SWPC, JPL/MPC) with their own latency, outage, and reporting-granularity characteristics that this system has no control over. Day 69's backoff logic is a reasonable response to an unreliable single source, but it's solving the wrong layer of the problem — it makes flakiness more tolerable instead of making the underlying signal more trustworthy.
- Some operation shapes plausibly have more than one independent way to confirm the same real-world event. A solar storm severe enough to show up in Orbital Watch's field data is also likely to be independently reported through more than one public feed (NOAA's own alert tiers, space-weather mirrors, other national agencies' equivalents) — but nothing today checks more than the single registered source.
- Relying on one source also means a corroboration-source bug or outage is indistinguishable, from this system's point of view, from the absence of a real event — which is exactly the ambiguity Day 66 was trying to resolve in the first place, just pushed one layer deeper into "is my corroboration source itself trustworthy right now."

## Architecture

### 1. Multi-source registry per shape
Where more than one independent confirmation source exists for an operation shape, register all of them rather than picking one. A shape with only one available source keeps working exactly as before — this is additive, not a replacement for shapes that can't be multiply covered.

```python
CORROBORATION_SOURCES = {
    "noaa-swpc:alert-linked": [fetch_noaa_active_alerts, fetch_swpc_mirror_alerts],
    "neows-lookup:keyed:responded": [fetch_jpl_close_approach_confirmations, fetch_mpc_confirmations],
}
```

### 2. Quorum-based status resolution
Instead of asking one source whether an event is active, query every registered source for the shape and require a minimum agreeing fraction before treating the event as independently confirmed. A single source reporting active, with others silent or reporting inactive, is not enough on its own.

```python
QUORUM_FRACTION = 0.5   # strict majority; tunable per shape based on source count and reliability

def resolve_corroboration_status(shape, window):
    sources = CORROBORATION_SOURCES.get(shape, [])
    if not sources:
        return None  # no corroboration possible — Day 66's original fallback
    reports = [source(window.start, window.end) for source in sources]
    active_votes = sum(1 for r in reports if r)
    return active_votes / len(sources) >= QUORUM_FRACTION
```

### 3. Partial-agreement states feed Day 68's transition regime directly
A split vote — some sources reporting active, others not, falling short of quorum — is not treated as "inactive by default." It's routed straight into Day 68's transition regime, since a disagreement between independent sources is exactly the kind of ambiguous signal that regime already exists to hold pending further evidence, rather than inventing a fourth regime category to handle it.

```python
def regime_for_commit(shape, field, timestamp):
    status = resolve_corroboration_status(shape, window_around(timestamp))
    if status is None:
        return "quiet"                      # no sources registered at all
    if status is True:
        return f"event:{active_event_type(shape)}"
    if is_split_vote(shape, timestamp):
        return f"transition:{shape}"         # route disagreement into Day 68's existing machinery
    return "quiet"
```

### 4. Per-source reliability weighting
Not every registered source is equally trustworthy for every shape — a primary agency feed and a secondary mirror don't deserve equal votes forever. Each source accumulates a reliability score based on how often it agrees with the eventual quorum-confirmed outcome, and quorum resolution (point 2) shifts from a flat vote count toward a weighted one as enough history accumulates — the same earn-it-before-trusting-it pattern this series has used for analyzer trust (Day 59) and coverage confidence (Day 56), just applied to external data sources instead of internal code.

```python
def weighted_quorum(shape, reports, source_reliabilities):
    weighted_votes = sum(w for r, w in zip(reports, source_reliabilities) if r)
    total_weight = sum(source_reliabilities)
    return weighted_votes / total_weight >= QUORUM_FRACTION
```

## Failure Modes
- **Redundancy isn't universal.** Many operation shapes will only ever have one plausible corroboration source — CelesTrak orbital-element shapes, as flagged back on Day 66, have no independent public confirmation feed at all. Quorum logic provides zero benefit there, and those shapes remain exactly as exposed to single-source flakiness as before, still routed through Day 69's timeout path.
- **Correlated sources defeat the purpose of a quorum.** If two registered sources for the same shape both ultimately derive from the same upstream feed (a mirror syncing from the primary rather than observing independently), a quorum vote isn't actually independent evidence — it's the same signal counted twice, giving false confidence in agreement that was never really there.
- **Weighting can entrench an early mistake.** Point 4's reliability scoring bootstraps from early agreement with quorum outcomes, but if the first several events happen to be ones where the less reliable source got lucky, its weight can rise further than its actual track record deserves before enough history corrects it — a smaller-scale version of the replay-sample-bias risk flagged back on Day 52.
- **More sources means more latency variance, not less.** Querying multiple external feeds per shape means the overall corroboration check now waits on the slowest registered source rather than the single one from before — a timeout-prone laggard source can now stall quorum resolution for a shape that otherwise has fast, reliable coverage.
- **Quorum thresholds need per-shape tuning just like everything else in this arc.** `QUORUM_FRACTION` is another constant added to a growing pile (hysteresis thresholds, transition timeouts, confidence thresholds going back to Day 56 and 59), and there's still no unified way to reason about how all these thresholds interact for a single shape under real event conditions.

## What's Next
Day 71: the correlated-sources failure mode is the sharpest gap left. A quorum is only as good as the independence of its members, and nothing today actually verifies that two registered sources for the same shape are observing the event through genuinely separate channels rather than one mirroring the other. The next step is detecting and discounting correlated sources so a quorum reflects real independent agreement rather than an illusion of it.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
