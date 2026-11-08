# Day 96: The Quiet Quarter Problem — Replaying Extreme Events Through the Pipeline

## TL;DR
Everything measured since Day 84 has been measured on ordinary days. Day 89's corpus is 14 canaries on normal traffic. Day 94's sensitivity analysis replayed a quiet 90-day window. Day 95's ablation ended with four mechanisms kept on the strength of a promise about rare events, and one mechanism, Day 69's provisional resolution, flagged as harmful on quiet data and possibly essential on fast-onset data. Orbital Watch exists for the rare days. A pipeline watching space weather, close approaches and catalog changes earns its keep when a storm arrives in twenty minutes, not in the long stretches where nothing happens. Today builds the replay suite the series has lacked since Day 76: reconstructed extreme events fed through the full pipeline at their original timing. It covers three event classes, and the first one broke something. During a fast storm onset the regime baseline kept treating storm data as quiet for 17 minutes, the drift detector read a 40-field distribution shift as upstream corruption, and the system raised 38 "upstream drift" alerts instead of a storm alert. The fix is asymmetric hysteresis: enter the event regime fast, leave it slowly.

## The Problem
- Defensive mechanisms look unnecessary on quiet data, which is the nature of defense. Day 95 had to settle for insurance claims because the evidence that would settle them was not in the window. Every claim in the register says "protects against X" where X had not occurred in 90 days.
- The system's central design is a stable baseline against which drift is measured (Days 65, 67, 72). That design assumes the baseline is correct and the data deviates. A real extreme event reverses that: the baseline is wrong because the world changed, and the data is right. Which of the two is true cannot be known in advance from one point, and the mechanisms for telling them apart (regime transitions, hysteresis, corroboration) were tuned on days when the answer was nearly always "the data drifted."
- Day 69 already hints at this. Its transition timeout and provisional resolution exist because a regime transition that never resolves is dangerous. Day 95 found it slightly harmful on average. An average over a quiet quarter says nothing about the tail.
- A synthetic burst (Day 76) or a Day 77 combinatorial scenario can stress concurrency, but it cannot stress the system's modeling of the world. For that the inputs need to carry the real structure of a rare event: correlated fields moving together at physical rates, with the lags and noise the real feeds have.

## Architecture

### 1. An event library with three classes
Events are reconstructed from archived feed captures where public archives exist, and from time-compressed variants of real recorded onsets otherwise. Each is a replayable bundle: per-feed timestamped payloads at original cadence, the labelled ground truth (when the event began, its peak severity, when an operator should have been told), and the feeds' own quirks, including delayed products and revised values.

```python
@dataclass
class ExtremeEvent:
    name: str
    event_class: str                    # "geomagnetic_onset" | "catalog_surge" | "close_approach_cluster"
    feeds: dict[str, list[TimedPayload]]
    truth: EventTruth                   # onset_ts, peak_severity, required_alert_by_ts
    provenance: str                     # "archived" | "reconstructed" | "time_compressed"

EVENT_LIBRARY = [
    ExtremeEvent("fast_storm_onset_a",      "geomagnetic_onset",      ..., provenance="archived"),
    ExtremeEvent("fast_storm_onset_b",      "geomagnetic_onset",      ..., provenance="time_compressed"),
    ExtremeEvent("fragmentation_surge",     "catalog_surge",          ..., provenance="reconstructed"),
    ExtremeEvent("uncertain_orbit_cluster", "close_approach_cluster", ..., provenance="reconstructed"),
]
```

Provenance is first-class. A time-compressed or reconstructed event is a model of a real event, and every result produced from it carries that label, in the same spirit as Day 89's label provenance.

### 2. A replay driver that preserves timing
The driver feeds each feed at its original cadence against a simulated clock, so timers (the transition timeout, persistence gates, TTL leases) behave as they do live. Instant replay would hide every timing bug, because the mechanisms that fail on a fast onset are precisely the timer-governed ones.

```python
def replay_event(pipeline, event: ExtremeEvent, speed=1.0):
    clock = SimClock(start=event.truth.onset_ts - PRE_ROLL_S)
    outputs = []
    for ts, feed, payload in merge_by_time(event.feeds):
        clock.advance_to(ts)
        pipeline.ingest(feed, payload, now=clock.now)
        outputs.extend(pipeline.drain_outputs())
    return EventRun(event, outputs, pipeline.regime_history, pipeline.alert_log)
```

### 3. Event-specific scoring against ground truth
Scoring uses the event's own truth record: time to first correct operator-facing alert, time to correct severity, the number of misleading outputs (alerts that point at the wrong cause), and the regime commit time relative to onset.

```python
def score_event(run: EventRun) -> EventScore:
    truth = run.event.truth
    first_correct = first(o for o in run.outputs if o.kind == "storm_alert" and o.severity >= truth.peak_severity - 1)
    return EventScore(
        alert_delay_s=(first_correct.ts - truth.onset_ts) if first_correct else None,
        met_deadline=bool(first_correct and first_correct.ts <= truth.required_alert_by_ts),
        regime_commit_delay_s=run.regime_history.first_commit(REGIME_EVENT).ts - truth.onset_ts,
        misleading_outputs=sum(1 for o in run.outputs if o.kind == "upstream_drift" and truth.is_real_event(o.ts)),
    )
```

### 4. The first finding: storm onset breaks the baseline
On `fast_storm_onset_a`, the planetary K estimate and solar wind speed rise from quiet to severe in about 20 minutes. The Day 74 severity bypass did its job: the first correct alert went out on time. The rest of the pipeline did not.

```
event: fast_storm_onset_a
alert_delay_s              : 310   (deadline 600)  OK
regime_commit_delay_s      : 1020  (17 min)
misleading_outputs         : 38    ("upstream_drift" alerts during a real event)
fields flagged as drifting : 40
```

The causal chain is entirely made of mechanisms from earlier days behaving as designed. The regime baseline (Day 67) required persistence (Day 68: 3 consecutive agreeing estimates at 5-minute intervals) before committing to the event regime. For those 15 minutes the baseline still said "quiet," so the drift detector (Day 65) compared storm data to a quiet baseline and flagged 40 fields. The corroboration quorum (Day 70) then found independent feeds agreeing that the data had drifted and concluded, correctly by its own logic, that upstream corruption was confirmed. The operator received a storm alert at 5 minutes and, over the next 12, a stream of "upstream data drift confirmed" warnings that contradicted it.

### 5. Asymmetric hysteresis: enter fast, exit slow
The Day 68 mechanism treats entering and leaving a regime as symmetric: the persistence count is 3 either way. The risks are not symmetric. Missing the start of an event is expensive and a premature entry is cheap, the same asymmetry as Day 46's isolation decisions and Day 85's HEALTHY floor. Entry now takes one estimate plus independent corroboration of severity from two feeds; exit keeps the slow rule.

```python
class AsymmetricRegimeTracker:
    ENTER_PERSISTENCE = 1
    ENTER_CORROBORATION = 2            # independent feeds (Day 70/71 correlation-collapsed)
    EXIT_PERSISTENCE = 6

    def propose(self, estimate, feeds_agreeing: int):
        if self.regime != REGIME_EVENT:
            if estimate.severity_regime == REGIME_EVENT:
                self._enter_count += 1
                if (self._enter_count >= self.ENTER_PERSISTENCE
                        and feeds_agreeing >= self.ENTER_CORROBORATION):
                    return self._commit(REGIME_EVENT, reason="fast_entry")
            else:
                self._enter_count = 0
        else:
            if estimate.severity_regime != REGIME_EVENT:
                self._exit_count += 1
                if self._exit_count >= self.EXIT_PERSISTENCE:
                    return self._commit(estimate.severity_regime, reason="slow_exit")
            else:
                self._exit_count = 0
```

Using corroboration for entry keeps a single noisy feed from yanking the system into the event regime. It reuses the correlation-aware quorum from Day 71, so two mirrors of one source do not count as two feeds.

### 6. The replay after the fix
```
event: fast_storm_onset_a            before    after
alert_delay_s                          310       310
regime_commit_delay_s                 1020       240     (17 min -> 4 min)
misleading_outputs                      38         2
fields flagged as drifting              40        11
```

The commit delay is bounded by the estimation cadence, since the first estimate after onset is the earliest available. The two remaining misleading outputs fall in the first estimation interval, before any estimate exists to commit. Those are the irreducible lag, and Day 98 returns to how that lag should be shown to an operator.

### 7. Day 69 under the same replay
Day 95's suspect mechanism gets its test. With the old symmetric tracker, provisional resolution at timeout committed the event regime at 15 minutes, the only reason the old pipeline ever got there. With asymmetric entry the timeout almost never fires on a fast onset, and on the slow-onset variant (`fast_storm_onset_b` stretched over two hours) it fires and commits correctly. The mechanism's measured effect is positive on both extremes and negative only on quiet data. It stays, now with an insurance claim and a test that exercises it.

### 8. A tenth taxonomy dimension and a standing check
The failure had no name in Day 82's taxonomy. Following the discipline from Day 88, the dimension is proposed with evidence, and a check is added to the gate.

```python
propose_taxonomy_addition(
    dimension="RARE_EVENT_BEHAVIOR",
    definition="Mechanism behaves acceptably on ordinary inputs and wrongly on rare extreme ones, "
               "typically because its parameters or baseline assume the ordinary regime.",
    evidence="Day 96: fast storm onset produced 38 misleading 'upstream drift' alerts and a 17-minute regime lag.",
)

register_check("preflight_rare_event_replay", preflight_rare_event_replay, covers={"RARE_EVENT_BEHAVIOR"})
```

The check replays the event library through any candidate that touches regime state, baselines, drift or quorum. A fixture with a seeded slow-commit defect joins the Day 92 battery. Coverage reads 10 of 10, with the same caveat as every earlier time it hit a round number.

## Failure Modes
- **The library has four events and two are not archived.** Reconstructed and time-compressed events model a real event. They inherit the modeler's assumptions about how correlated fields co-move, so a pipeline can be tuned to pass them while still failing a real onset with a different structure. Four events is a handful, and tuning to a handful is overfitting.
- **Fast entry trades a missed-event risk for a false-entry risk, and false entry has not been measured.** The asymmetric rule was validated on events and on the quiet 90-day window, where it entered the event regime 3 times without a real event behind it, each caught by the 6-estimate exit rule within 40 minutes. Three in 90 days looks acceptable and also looks like three operator-visible regime flips.
- **Original-timing replay still uses the simulated feeds' idealized availability.** In every replay the feeds kept arriving. Real extreme events are exactly when public data services see load, delays and outages. A pipeline that handles the fast-onset replay cleanly has not been shown to handle the same event with a feed missing.
- **The ground-truth "required alert by" deadline is a number chosen by the system's authors.** It is plausible and it is not derived from what a downstream operator can use. Meeting a deadline nobody validated is the same shape of problem as Day 89's self-authored scenarios.

## What's Next
Every replay today fed the pipeline all of its feeds. Real extreme events are also when feeds fail, and the system has never been tested for that. Quorum logic (Day 70), correlation collapsing (Day 71) and the suspect state (Day 46) were all designed with sources arriving, late at worst. What happens when the primary source is simply gone in the middle of the event it exists to report? Day 97 crosses the event library with a source-loss matrix.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
