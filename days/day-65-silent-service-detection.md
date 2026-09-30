# Day 65: Silent Drift Detection — When the Provider Changes and Nothing Tells You

## TL;DR
Day 64 handles every change the system itself makes — a projection schema edit, an adapter addition, a code redeploy — by bumping an epoch and reclassifying. But a provider can change what it returns without touching any of that: a field switches from kilometers to meters, a previously-always-null field starts populating, a value range shifts. No epoch moves, so Day 64's whole classify-and-quarantine pipeline never fires, and every branch keeps using a scoped hash earned under behavior that no longer holds. Today's fix adds passive statistical profiling of raw field values per operation shape, feeding a detected shift into Day 64's machinery as a synthetic drift signal — with a persistence requirement before anything acts on it, since a single anomalous batch is far more likely to be a real astronomical event than a provider change.

## The Problem
- Every invalidation trigger built since Day 57 is reactive to something the system did: a code diff, a schema edit, a registry update. None of them fire when the *provider* changes behavior unilaterally, because nothing about the operation shape, the projection schema, or the adapter set changed on our end.
- The effect ledger already stores raw results (Day 52) and could, in principle, notice a field's values look different — but nothing currently looks. Trust, read-sets, and scoped hashing all operate purely on structural and access information, never on the actual distribution of values a field has historically held.
- This is a real risk specifically because Orbital Watch's upstreams (NASA NeoWs, NOAA SWPC, CelesTrak) are third-party APIs outside any control — a unit change, a newly populated optional field, or a precision change in orbital elements is exactly the kind of unannounced provider-side shift that has no code-side signal at all.
- The domain makes this harder, not easier. Space weather and near-earth object data is legitimately bursty — a real solar storm produces genuinely extreme values that look, statistically, a lot like drift. A detector tuned to catch provider changes risks flagging the exact moments the system most needs to keep trusting its data without interruption.

## Architecture

### 1. Rolling field profiles
For every operation shape, maintain a per-field profile built from raw results as they're committed: value type, a magnitude distribution (percentile bounds rather than a fixed mean/stddev, given the bursty domain), null-rate, and cardinality for categorical fields.

```python
def update_profile(shape, field, value):
    profile = FIELD_PROFILES.setdefault((shape, field), RollingProfile())
    profile.observe(value)   # percentile sketch, null counter, distinct-value tracker
```

### 2. Shift detection against baseline
A separate, longer-window baseline profile is compared against the recent rolling profile on a schedule. A shift is flagged when the recent distribution falls meaningfully outside the baseline's historical percentile range, the null-rate changes past a threshold, or a field that has always been null starts populating.

```python
def detect_shift(shape, field):
    baseline = BASELINE_PROFILES[(shape, field)]
    recent = FIELD_PROFILES[(shape, field)]
    if recent.null_rate_delta(baseline) > NULL_RATE_THRESHOLD:
        return "null_rate_shift"
    if recent.outside_percentile_band(baseline, band=(1, 99)):
        return "magnitude_shift"
    if baseline.always_null() and not recent.always_null():
        return "field_newly_populated"
    return None
```

### 3. Persistence gate before acting
A single flagged batch is not enough — it could be a real event, a transient upstream blip, or noise. The shift must hold across a minimum number of subsequent commits before it's treated as drift, the same conservative-then-earn pattern used for coverage confidence (Day 56) and analyzer trust (Day 59), just applied to a signal about the *data* instead of the *code*.

```python
def confirm_shift(shape, field, min_persisting_commits=50):
    streak = SHIFT_STREAKS.setdefault((shape, field), 0)
    if detect_shift(shape, field):
        streak += 1
    else:
        streak = 0
    SHIFT_STREAKS[(shape, field)] = streak
    return streak >= min_persisting_commits
```

### 4. Feed confirmed drift into Day 64's pipeline
A confirmed shift is treated exactly like an altering producer-side change from Day 64 — the affected field is intersected against every branch's read-set, intersecting branches are quarantined, and a synthetic epoch bump is recorded so the invalidation is visible in the same bookkeeping as a registry-driven one.

```python
def on_confirmed_drift(shape, field):
    changed_fields = {field}
    new_epoch = content_hash((upstream_epoch(shape), "silent-drift", field))
    apply_altering_change(shape, changed_fields, new_epoch)   # Day 64
```

## Failure Modes
- **Domain events look like drift.** A genuine solar storm or a close-approach NEO event can push field values far outside their historical baseline for entirely legitimate reasons — exactly the moments the system most needs to keep trusting fast data. The persistence gate reduces false positives from short-lived events but doesn't distinguish a sustained real event from a sustained provider change; both look identical to this detector.
- **No root-cause signal.** A confirmed shift only says "this field's distribution changed," never why. A provider bug, a unit change, and a legitimate new regime of physical values all trigger the same quarantine path, so a human still has to investigate before deciding whether the shift is something to permanently adapt to (a new projection schema entry, a new canonicalization adapter) or something to wait out.
- **Detection window is a real exposure.** During the persistence-confirmation period, already-trusted branches keep using a scoped hash computed under the old, now-drifted, understanding of the field — the same lag problem flagged for diagnostic mode back on Day 60, here applied to statistical rather than reconciliation-based detection.
- **Continuous profiling cost.** Every field of every operation shape now carries an always-on profiling cost, scaling with the number of distinct fields tracked across the whole system — a nontrivial addition on top of everything else already running per commit.
- **Threshold tuning is domain-specific and unstable.** Percentile bands and null-rate thresholds that work for a stable field can be wrong for one with seasonal or event-driven behavior, and there's no single setting that fits both quiet periods and active space-weather windows.

## What's Next
Day 66: the domain-event-vs-drift ambiguity is the real open problem here. A detected shift during a confirmed NOAA space weather alert is far more likely to be a genuine event than a provider change, but nothing today correlates the drift signal against independent confirmation the way Day 41's forecast-gated splitting did for a completely different part of the system. The next step is using that kind of external corroboration to tell "the world changed" apart from "the provider changed."

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
