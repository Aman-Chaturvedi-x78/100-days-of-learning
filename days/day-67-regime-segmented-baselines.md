# Day 67: Regime-Segmented Baselines — Keeping Event Data From Polluting Quiet Data

## TL;DR
Day 66 correctly stopped a corroborated event from triggering quarantine, but left an open question about what happens to the baseline profile itself once event data is logged as "expected." If it blends into the same rolling profile used for quiet-period comparison, every real event permanently widens what counts as normal, and the detector gets a little worse at its actual job every time it correctly handles an edge case. Today's fix stops blending. Field profiles are now kept per regime — quiet, and one or more event regimes reported by Day 66's corroboration sources — and a commit is only ever compared against the baseline for the regime it actually belongs to.

## The Problem
- Day 65's baseline is a single rolling profile per `(shape, field)`. Day 66 decides whether a shift is expected or not, but never changes which profile that data feeds into — corroborated event data and ordinary quiet-period data currently update the exact same baseline.
- This means every genuine event, correctly identified as expected variance by Day 66, still leaves a permanent mark on the one baseline used to judge everything afterward. A single severe solar storm can widen the percentile bounds enough that a genuinely anomalous value during the next quiet period no longer looks anomalous at all.
- The failure direction is the opposite of Day 65's original problem, which is what makes it easy to miss: Day 65 was built to stop false positives during real events. Left unaddressed, this creates false negatives during quiet periods, and false negatives are the more dangerous failure — this series has flagged a silent missed cascade as worse than an unnecessary one on nearly every day since Day 52, and an undetected provider change during a quiet period is exactly that.
- There's also a subtler problem hiding in the "expected" label itself. A provider bug that happens to occur during a real, corroborated event (the coincidental-corroboration failure mode flagged on Day 66) currently gets excused entirely, because nothing compares that data against what the *event itself* should normally look like — only against whether an event was active at all.

## Architecture

### 1. Regime-labeled commits
Every commit is tagged with a regime label at write time, sourced from Day 66's corroboration check rather than inferred after the fact — `"quiet"` by default, or a specific event type (`"event:solar-storm"`, `"event:close-approach"`) when a corroboration source reports an active, overlapping confirmation.

```python
def regime_for_commit(shape, field, timestamp):
    source = CORROBORATION_SOURCES.get(shape)
    if source is None:
        return "quiet"
    active = source(timestamp, timestamp)
    return f"event:{active[0].event_type}" if active else "quiet"
```

### 2. Per-regime baseline maintenance
`FIELD_PROFILES` and `BASELINE_PROFILES` are re-keyed to include the regime label, so quiet-period data only ever updates the quiet baseline, and each event type accumulates its own separate baseline over repeated occurrences.

```python
def update_profile(shape, field, value, regime):
    profile = FIELD_PROFILES.setdefault((shape, field, regime), RollingProfile())
    profile.observe(value)
```

### 3. Regime-aware drift detection
Day 65's `detect_shift` and `confirm_shift` now compare a commit only against the baseline for its own regime, not a single merged one. This directly closes the coincidental-corroboration gap from Day 66: a provider bug occurring during a real solar storm still gets compared against the *solar-storm* baseline, and can still be flagged as drift if it deviates from what that event type normally looks like — corroboration no longer blanket-excuses every anomaly during an active event.

```python
def detect_shift(shape, field, regime):
    baseline = BASELINE_PROFILES[(shape, field, regime)]
    recent = FIELD_PROFILES[(shape, field, regime)]
    if recent.null_rate_delta(baseline) > NULL_RATE_THRESHOLD:
        return "null_rate_shift"
    if recent.outside_percentile_band(baseline, band=(1, 99)):
        return "magnitude_shift"
    if baseline.always_null() and not recent.always_null():
        return "field_newly_populated"
    return None
```

### 4. New-regime bootstrap
A regime label reported for the first time — a first-ever confirmed event type for a given shape — has no baseline to compare against yet. Rather than skipping detection entirely, new regimes fall back to comparison against the quiet baseline until enough of their own data accumulates, the same conservative-default pattern used everywhere else in this arc: no evidence of what "normal" looks like for this regime yet, so borrow the most conservative available comparison instead of granting a free pass.

```python
def detect_shift(shape, field, regime):
    key = (shape, field, regime)
    if key not in BASELINE_PROFILES or BASELINE_PROFILES[key].sample_count < MIN_BASELINE_SAMPLES:
        return detect_shift(shape, field, "quiet")  # bootstrap against quiet baseline
    baseline = BASELINE_PROFILES[key]
    recent = FIELD_PROFILES[key]
    # ... same comparison as point 3, once the regime has its own baseline
```

### 5. Regime retirement
A regime that stops being reported (an event type that no longer applies, or a corroboration source that's deprecated) keeps its accumulated baseline archived rather than deleted, mirroring Day 57's approach to retired code-version read-sets — useful for later analysis, never consulted for live detection once retired.

```python
def retire_regime(shape, field, regime):
    key = (shape, field, regime)
    if key in BASELINE_PROFILES:
        REGIME_ARCHIVE[key] = BASELINE_PROFILES.pop(key)
```

## Failure Modes
- **Classification depends entirely on Day 66's corroboration accuracy.** A missed or late corroboration mislabels genuine event data as quiet, and that data pollutes the quiet baseline exactly as before today's fix — regime segmentation only helps if the regime label itself is correct, which makes this fix fully dependent on the reliability of the corroboration sources flagged as a risk on Day 66.
- **Regime granularity is a real tradeoff.** A moderate solar storm and an extreme one may need genuinely different baselines to avoid the same blending problem recurring within a single "event" regime, but splitting further fragments the data thin enough that each regime struggles to accumulate a reliable baseline of its own — too coarse reintroduces today's problem one level down, too fine starves every regime of samples.
- **Transition-boundary ambiguity.** Data captured right as an event begins or ends is inherently ambiguous — a value technically outside the corroboration source's reported window but still influenced by the event's onset or decay can get misclassified into the wrong regime, contaminating whichever baseline it lands in.
- **Multiplied state.** Per-regime baselines multiply the storage and maintenance cost that was already growing across path fingerprints (Day 56), code versions (Day 57), access surfaces (Day 58), branch families (Day 63), and upstream epochs (Day 64) — this adds yet another dimension, and retired-regime archives (point 5) add unbounded growth on top of that without a pruning policy.
- **Quiet-baseline bootstrap borrowing has its own risk.** New regimes borrowing the quiet baseline (point 4) means an event type's first occurrences are judged by quiet-period standards, which could itself misfire as drift for values that are normal for that event type but well outside quiet-period norms — the exact false-positive problem Day 66 was built to solve, recurring specifically for first-time event types.

## What's Next
Day 68: transition-boundary ambiguity is the sharpest open edge here — Day 39 already solved a structurally similar problem (flapping between shard-split and shard-merge decisions) with hysteresis around the boundary rather than a hard cutoff. The natural next step is applying that same hysteresis idea to regime transitions, so data near an event's onset or offset isn't binned into a hard-edged regime based on a single timestamp comparison.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
