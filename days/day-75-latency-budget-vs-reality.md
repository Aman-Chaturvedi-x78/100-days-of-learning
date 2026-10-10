# Day 74: Latency Budget vs. Reality — Running the Number Day 73 Made Possible

## TL;DR
Day 73's threshold registry made `worst_case_detection_latency` computable for the first time — but computing it and checking it against what the domain actually needs are two different things. Today runs that number end-to-end for the space-weather detection chain and compares it against how fast a real solar storm actually escalates, using NOAA's own alert-tier timing as the reference. The chain turns out to be too slow for the fastest-moving severity tier, specifically because several of the persistence gates built for good reasons on their own days (65, 68, 72) stack additively along the same critical path. The fix isn't lowering any single threshold — it's letting the registry's dependency graph short-circuit the chain for high-confidence signals, so a severe, unambiguous event doesn't have to wait through every gate built for the ambiguous case.

## The Problem
- `worst_case_detection_latency` (Day 73, point 2) sums estimated wall-clock contributions along the dependency chain rooted at drift detection: Day 65's `drift_persistence_commits` (50 commits) → Day 66/70's corroboration/quorum resolution (bounded by the slowest registered source, per Day 70's own failure modes) → Day 68's hysteresis entry confirmation (`ENTER_EVENT_CONSECUTIVE_CHECKS`) → Day 72's correlation-shift persistence (`min_persisting_events`, 20 events) where source grouping is mid-reclassification. Run against realistic commit cadence for the NeoWs/SWPC ingestion rate, the chain comes out to several minutes in the best case and well over the useful response window in a degraded case (a slow corroboration source, per Day 70's flagged risk, or an in-progress correlation reclassification, per Day 72).
- NOAA's own SWPC alert tiers escalate faster than that for the most severe events — a G4/G5-class geomagnetic storm can move from first indication to full-severity alert in a timeframe shorter than this chain's best-case latency, meaning the system's own detection pipeline can still be working through persistence gates built for ambiguous, slow-moving cases while the actual event has already peaked and begun to decline.
- This is a direct, now-measurable consequence of something flagged abstractly on Day 72 and named explicitly on Day 73: every gate in this arc was designed in isolation to solve its own false-positive problem, and each one adds latency on the same critical path. No single gate is wrong on its own terms — Day 65's persistence count correctly filters single-batch noise, Day 68's hysteresis correctly filters boundary flapping, Day 72's correlation persistence correctly filters a flickering source relationship — but stacked serially, their combined latency was never checked against a deadline until today, because today is the first day that deadline was even computable.
- The chain also doesn't distinguish signal strength. A single ambiguous blip and an unmistakably severe anomaly travel through exactly the same sequence of gates at exactly the same pace, because every gate was built to assume it might be looking at the hard case.

## Architecture

### 1. A severity-estimated fast path
Each stage in the chain now also emits a rough severity/confidence estimate alongside its normal pass/fail output — not a new detector, just exposing a signal several stages already compute internally (Day 65's `outside_percentile_band` already knows *how far* outside the band a value is, not just whether it's outside).

```python
def severity_estimate(shape, field, observation):
    deviation = percentile_distance(observation, BASELINE_PROFILES[(shape, field)])
    return min(deviation / SEVERE_DEVIATION_REFERENCE, 1.0)  # 0.0-1.0
```

### 2. Registry-declared bypass conditions
The threshold registry (Day 73) gains a second kind of entry: a bypass rule, declaring that a sufficiently high severity estimate at one stage can skip the persistence requirement at a later stage, rather than every signal being forced through every gate uniformly.

```python
BYPASS_RULES = {
    "correlation_persistence_events": {
        "condition": lambda ctx: ctx.severity_estimate >= 0.9,
        "bypassed_wait": "min_persisting_events",
        "rationale": "an unambiguous severity signal doesn't need the same correlation "
                      "patience built for a borderline one",
    },
}

def confirm_correlation_shift(shape, pair, context, min_persisting_events=20):
    rule = BYPASS_RULES.get("correlation_persistence_events")
    if rule and rule["condition"](context):
        return True  # high-confidence bypass, logged as such
    # ... Day 72's normal streak-based confirmation otherwise
```

### 3. Bypass is a separate, logged path — not a lowered threshold
Critically, this isn't tuning any of Days 65/68/72's thresholds down. The normal persistence-gated path stays exactly as conservative as it was designed to be for ambiguous signals — point 2's bypass only fires for the narrow, high-confidence case, and every bypass is logged distinctly from a normal confirmation (reusing Day 73's decision trace, point 3) so it's visible afterward which decisions were made on the fast path versus the patient one.

```python
def log_decision(mechanism, outcome, context):
    DECISION_LOG.append({
        "mechanism": mechanism, "outcome": outcome,
        "path": "bypass" if context.get("bypassed") else "normal",
        "thresholds_in_force": {k: v.value for k, v in THRESHOLD_REGISTRY.items() if k in context.relevant_thresholds},
        "timestamp": now(),
    })
```

### 4. Recomputed latency, now with a bypass floor
`worst_case_detection_latency` from Day 73 gets a companion, `best_case_detection_latency`, computed by walking the same dependency chain but substituting each bypass-eligible stage's zero-wait outcome where a rule applies — giving an honest two-number answer ("here's the worst case for an ambiguous signal, here's the best case for an unambiguous one") instead of one number that was secretly only describing the patient path.

```python
def best_case_detection_latency(shape):
    chain = resolve_dependency_chain(THRESHOLD_REGISTRY, start="drift_persistence_commits")
    return sum(0 if has_bypass_rule(t) else estimate_wall_time(t, shape) for t in chain)
```

## Failure Modes
- **Severity estimation is itself new, unvalidated surface area.** `severity_estimate` is introduced today specifically to drive a safety-relevant bypass decision, but it has none of the replay-evidenced, earned-trust treatment this series has insisted on for every other consequential signal since Day 49 — it's a plausible first cut, not a validated one, and a bypass gated on a bad severity estimate reintroduces exactly the false-positive risk the bypassed gate existed to prevent.
- **Bypass creates a new seam for exactly the attack this series keeps worrying about.** A provider-side problem that happens to produce an extreme-looking value (not a real event, just a bug that manufactures a severe-looking deviation) would now race through the fast path instead of being caught by the correlation and corroboration patience it was supposed to pass through — the bypass is a second, narrower version of the coincidental-corroboration risk flagged back on Day 66, deliberately reintroduced in exchange for latency.
- **Two numbers instead of one is more honest but also more to misread.** `best_case_detection_latency` and `worst_case_detection_latency` can both be true simultaneously for the same shape, and a dashboard or operator glancing at only the optimistic number risks assuming the system is faster than it actually is for the ambiguous cases that are, by definition, the more common ones.
- **The bypass rule registry needs the same per-shape tuning discipline as everything else, with less history to tune against.** `SEVERE_DEVIATION_REFERENCE` and the `0.9` bypass threshold are brand-new constants with zero accumulated operational history, unlike the thresholds they're allowed to short-circuit, which at least had days of design reasoning behind them even if not formal validation.
- **This doesn't fix the underlying latency, it routes around it for a subset of cases.** The full chain is still exactly as slow as Day 73 measured it for every signal that doesn't qualify for a bypass — which, by construction, includes every genuinely ambiguous case this whole arc was built to handle carefully. Today buys speed only for the cases that arguably needed it least.

## What's Next
Day 75: the severity-estimate bypass is a patch on a chain whose fundamental shape is still serial — every stage still waits for the previous one to finish before starting its own work, bypasses aside. The open question is whether stages that don't actually depend on each other's *conclusions* (only on each other's *data*) could run concurrently instead of serially, which would lower the latency for the patient path too, rather than only creating an escape hatch around it.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
