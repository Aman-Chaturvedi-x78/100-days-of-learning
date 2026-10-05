# Day 72: Correlation Drift Detection — Watching the Relationship, Not Just the Sources

## TL;DR
Day 71's correlation flagging is a lagging historical average: it tells you whether two sources have agreed suspiciously often across their *entire* recorded history, not whether that relationship just changed. A provider quietly consolidating a mirror feed onto a shared upstream, or conversely splitting a previously-correlated pair onto genuinely separate infrastructure, produces a shift in agreement rate that Day 71's accumulate-forever model will absorb slowly and silently. Today's fix applies the same tool this series built for field values back on Day 65 — rolling-window statistical profiling with a persistence gate — to the agreement rate between every source pair, so a change in *how two sources relate to each other* gets detected with the same rigor as a change in what either source reports.

## The Problem
- Day 71's `PAIRWISE_AGREEMENT` record is a single running total: `agree` and `total` counts that only ever grow. A pair's agreement rate is `agree / total` across the pair's entire history, which means a relationship that was genuinely independent for its first thousand events and became correlated afterward (or vice versa) shows up as a blended average that doesn't clearly reflect either period.
- This is structurally the exact problem Day 65 solved for individual field values — a single rolling profile blending two different regimes of behavior into one number that's an accurate description of neither. Day 71 never got the regime-segmentation treatment Day 67 gave to field profiles, so the same contamination risk that was fixed for data values one layer down still exists for source relationships one layer up.
- The consequence is asymmetric and dangerous in the same direction flagged repeatedly since Day 52: a pair that becomes newly correlated (provider consolidation) keeps being counted as two independent votes for a long time after the consolidation, silently reintroducing Day 70's original single-source-disguised-as-two problem. A pair that becomes newly independent (infrastructure split) keeps being collapsed into one effective vote longer than necessary, which is merely wasteful rather than dangerous, but still a real cost.
- Nothing today treats the pairwise agreement rate itself as a monitored signal with a baseline and a shift detector — it's treated as a fact to be measured once and trusted indefinitely, rather than a quantity that can itself drift.

## Architecture

### 1. Rolling agreement profile per source pair
Replace the single cumulative counter with the same `RollingProfile` abstraction Day 65 built for field values, applied to the binary agree/disagree outcome of each resolved event for a given source pair.

```python
def record_pairwise_agreement(shape, source_a, source_b, status_a, status_b, timestamp):
    key = (shape, frozenset({source_a, source_b}))
    profile = PAIRWISE_PROFILES.setdefault(key, RollingProfile())
    profile.observe(1.0 if status_a == status_b else 0.0, timestamp)
```

### 2. Baseline-vs-recent comparison, reusing Day 65 directly
A longer-window baseline agreement rate is compared against a shorter recent window, exactly mirroring `detect_shift` — a long-run baseline of "these two usually agree 40% of the time" that suddenly shows 95% agreement in the recent window is as meaningful a signal as a field's value distribution shifting.

```python
def detect_correlation_shift(shape, pair):
    baseline = CORRELATION_BASELINES[(shape, pair)]
    recent = PAIRWISE_PROFILES[(shape, pair)]
    if recent.outside_percentile_band(baseline, band=(1, 99)):
        return "correlation_shift"
    return None
```

### 3. Persistence gate before re-classifying a pair
A single resolved event agreeing or disagreeing unusually is noise, not evidence — exactly Day 65's reasoning for field drift. The shift must hold for a minimum number of subsequent resolved events before the pair's correlation status (as used by Day 71's `is_correlated`) is updated.

```python
def confirm_correlation_shift(shape, pair, min_persisting_events=20):
    streak = CORRELATION_SHIFT_STREAKS.setdefault((shape, pair), 0)
    if detect_correlation_shift(shape, pair):
        streak += 1
    else:
        streak = 0
    CORRELATION_SHIFT_STREAKS[(shape, pair)] = streak
    return streak >= min_persisting_events
```

### 4. Re-grouping on confirmed shift, in either direction
A confirmed shift toward higher agreement re-runs Day 71's `is_correlated` check against the new, recent-window rate and re-groups the pair into a single effective vote going forward. A confirmed shift toward lower agreement does the reverse — ungroups a previously-correlated pair, restoring their independent votes. Either direction reuses Day 71's existing `correlated_groups` union-find structure; today's change only decides *when* that structure gets recomputed, not how it works once it does.

```python
def on_confirmed_correlation_shift(shape, pair):
    recent_rate = PAIRWISE_PROFILES[(shape, pair)].recent_rate()
    if recent_rate >= CORRELATION_THRESHOLD:
        add_to_correlated_group(shape, pair)
    else:
        remove_from_correlated_group(shape, pair)
    CORRELATION_BASELINES[(shape, pair)] = PAIRWISE_PROFILES[(shape, pair)].snapshot_as_baseline()
```

## Failure Modes
- **Cold start, again.** A newly registered source pair has no baseline agreement rate yet, so there's nothing to detect a shift against — the pair falls back to Day 71's original accumulate-from-zero behavior until enough history exists to establish both a baseline and a meaningful recent window, which is the same two-stage cold start flagged for trust scoring back on Day 59, recurring here for source relationships.
- **Low event volume stretches every window.** Corroboration events for rare shapes (a once-a-month close-approach confirmation, say) accumulate so slowly that both the baseline and the persistence gate take a long time to fill — a correlation shift for a low-volume shape could take months to confirm, during which the pair's grouping status is stale by construction, not by oversight.
- **A real shift and a noisy patch look the same at first.** Exactly like Day 66's domain-event-vs-drift ambiguity, a handful of coincidentally aligned (or misaligned) events can look like the start of a genuine correlation shift before the persistence gate has had enough data to tell the difference — the gate reduces false positives but introduces the same detection lag flagged for every persistence-gated mechanism since Day 56.
- **Re-grouping churn compounds with Day 71's weighting.** Toggling a pair's grouping status back and forth near the shift-detection threshold doesn't just change the vote count — it also resets which member's reliability weight dominates the group (Day 71, point 3's "best-weighted member" rule), so a borderline pair can produce visibly different quorum outcomes across adjacent events purely from classification noise.
- **This is now three layers of rolling statistics stacked on top of each other.** Field-value drift (Day 65), regime-segmented field baselines (Day 67), and now correlation-rate drift all reuse the same `RollingProfile` machinery, but each layer has its own thresholds, windows, and persistence gates — reasoning about why a given quorum decision happened now potentially requires tracing through all three independently-tuned detectors at once.

## What's Next
Day 73: stacking detector on detector on detector, as flagged above, is itself becoming the central risk of this arc. Every layer since Day 65 has been built by taking a working pattern and reapplying it one level higher, and each reapplication adds its own tunable thresholds without removing any of the ones below it. The next step isn't a new detector — it's asking whether this growing stack of independently-tuned thresholds can be reasoned about as a whole, or whether it needs to be consolidated before adding a fourth layer on top of correlation drift.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
