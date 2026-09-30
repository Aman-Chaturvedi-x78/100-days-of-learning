# Day 66: Corroborated Drift — Telling a Real Event From a Real Problem

## TL;DR
Day 65's drift detector can tell that a field's distribution shifted, but it can't tell why. In a domain where the upstreams are NASA NeoWs, NOAA SWPC, and CelesTrak, the two most likely explanations for a sustained shift are exactly the two the detector can't separate: a genuine solar storm or close-approach event, and an actual provider-side change. Today's fix borrows the pattern Day 41 already proved out for pre-emptive shard splitting — don't act on a signal alone, gate it against an independent, authoritative confirmation source. When Day 65 confirms a persisting shift, the system now checks whether an external event feed corroborates it for the same window before deciding what the shift means.

## The Problem
- Day 65's persistence gate answers "did this shift hold long enough to be real," not "what caused it." Both a sustained solar storm and a sustained provider change produce a persisting, statistically significant shift, and the detector has no way to distinguish them from the shape of the data alone.
- This is exactly backwards from what the domain needs. A confirmed NOAA space weather alert is precisely the moment Orbital Watch most needs to keep trusting incoming data quickly — that's the entire premise behind Day 41's forecast-gated pre-emptive splitting. But it's also precisely the moment Day 65's detector is most likely to fire, because real events produce the most extreme, most anomalous-looking values.
- Left as-is, Day 64's quarantine pipeline would fire during active events, stripping trust from branches and forcing full-hash fallback at the exact moments when the system is under the most load and needs its optimizations working the most. That's a self-inflicted availability problem layered on top of a real astronomical one.
- The system already has the answer to "is this a real event" sitting elsewhere. Day 41 built forecast-based splitting gated on confirmed NOAA alerts for a completely different subsystem — the corroboration source exists, it's just never been wired into the drift-detection path.

## Architecture

### 1. Corroboration source registry
Each operation shape that has a plausible independent confirmation source gets one registered — NOAA's SWPC alert feed for space-weather-related shapes, JPL/MPC close-approach confirmations for NEO shapes. Not every shape has one; CelesTrak orbital-element shapes, for instance, have no equivalent authoritative "this is an expected anomaly" signal, and are handled explicitly in point 4.

```python
CORROBORATION_SOURCES = {
    "noaa-swpc:alert-linked": fetch_noaa_active_alerts,
    "neows-lookup:keyed:responded": fetch_jpl_close_approach_confirmations,
}
```

### 2. Drift-window correlation
Before a Day 65-confirmed shift is handed to Day 64's quarantine pipeline, check whether the registered corroboration source reports an active, independently-confirmed event overlapping the shift's time window.

```python
def check_corroboration(shape, field, shift_window):
    source = CORROBORATION_SOURCES.get(shape)
    if source is None:
        return None  # no corroboration possible for this shape
    events = source(shift_window.start, shift_window.end)
    return events if events else None
```

### 3. Branching outcome
A corroborated shift is treated as expected variance rather than drift — trust is preserved, no quarantine fires, and the event is logged for visibility. An uncorroborated shift proceeds exactly as Day 65 designed: fed into Day 64's classify-and-quarantine pipeline as a synthetic epoch bump.

```python
def resolve_confirmed_shift(shape, field, shift_window):
    corroboration = check_corroboration(shape, field, shift_window)
    if corroboration:
        log_expected_variance(shape, field, shift_window, corroboration)
        return "corroborated"
    on_confirmed_drift(shape, field)  # Day 65 -> Day 64's quarantine path
    return "uncorroborated"
```

### 4. Fallback for shapes with no corroboration source
Operation shapes with no registered independent confirmation source skip straight to Day 65's original uncorroborated treatment — the conservative default that's carried every day of this arc since Day 52: no evidence of a benign cause, no exception granted.

```python
def check_corroboration(shape, field, shift_window):
    source = CORROBORATION_SOURCES.get(shape)
    if source is None:
        return None  # falls straight through to on_confirmed_drift, same as before Day 66
    return source(shift_window.start, shift_window.end)
```

## Failure Modes
- **The corroboration source is itself a dependency.** NOAA's alert feed and JPL's confirmation service are external systems with their own latency and their own outage risk — checking them adds a dependency on another provider's reliability exactly during the high-load windows this fix is meant to protect, and a corroboration-source outage during a real event would silently fall back to uncorroborated treatment and quarantine anyway.
- **Coincidental corroboration.** An independently confirmed event can be active at the same time as an unrelated provider bug, and today's design has no way to separate "this drift is caused by the event" from "this drift merely overlaps the event." A genuine provider change during a solar storm gets wrongly excused as expected variance.
- **Corroboration lag versus detection lag.** Day 65's persistence gate already introduces exposure time before a shift is confirmed; checking an external source adds another round trip before a verdict is reached, during which trusted branches keep operating under whichever assumption — drift or event — turns out to be wrong.
- **No coverage for every shape.** Shapes without a registered corroboration source get zero benefit from today's fix and stay on Day 65's original quarantine-on-persistence path, meaning some parts of the system remain exactly as exposed to false-positive quarantine during real events as before.
- **Blending real event data into the baseline.** Logging a corroborated shift as "expected variance" doesn't yet say what happens to Day 65's rolling baseline profile itself — if event data gets folded into the same baseline used for quiet-period comparison, the baseline permanently widens, and the detector becomes less sensitive to genuine drift the next time a quiet period actually needs protecting.

## What's Next
Day 67: that last failure mode is the real open thread. If corroborated event data and ordinary quiet-period data both feed the same baseline profile, every real event permanently desensitizes the detector a little more, until enough events have accumulated that the baseline is too wide to catch anything. The next step is keeping event-period data statistically separate from quiet-period data instead of blending them into one profile.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
