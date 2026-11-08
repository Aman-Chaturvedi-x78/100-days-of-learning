# Day 98: What the Operator Actually Sees — Collapsing Fourteen Internal States into Three Axes

## TL;DR
Ninety-seven days of building have made the pipeline's inner life rich. It knows when a regime is in transition (Day 68), when a drift is suspected but not corroborated (Day 66), when a resolution is provisional (Day 69), when a mechanism is on probation (Day 90), when a source set is degraded (Day 97), when a quorum is below full strength (Day 70), when a feed is stale or suspect (Day 46). Every one of those was added for a good reason and every one leaks into the output payload under its own label. A count of the output schema found 14 distinct state labels a consumer could encounter. None of the series' safeguards matters if a person looking at the output in the middle of a storm cannot tell what is happening, how sure the system is, and what to do. Today designs the contract between pipeline and operator: three axes (what is the situation, how confident is the system, how healthy is the system) with reason codes, an action field, and a rule that no internal uncertainty is allowed to be silent. A contract test replays Day 96's storm and Day 97's feed loss minute by minute and checks that the rendered output was never misleading. That test checks the contract. It does not check whether a real person understands the display, and the post says so.

## The Problem
- The output grew by accretion. Each day added a field or a label, usually inside an existing payload, because that was where the new state was discovered. No one designed the output; it is the sum of 40 local decisions. The result is a payload with state names like `regime=TRANSITION`, `drift=SUSPECTED`, `resolution=PROVISIONAL`, `source_set=DEGRADED_SINGLE_DERIVED`, `quorum=SUB_FULL`, `mech_status=PROBATION`.
- A consumer has to combine them. Is `regime=TRANSITION` with `drift=SUSPECTED` and `quorum=SUB_FULL` a drift alert or a storm? Day 96 showed how the system itself got this wrong: 38 `upstream_drift` warnings during a real event, each of which was technically correct under the pipeline's own logic.
- Internal states differ in whether they matter to the operator. A mechanism being on probation changes how much the pipeline trusts its own output and so matters. A cluster epoch change is bookkeeping and does not. There is no declared classification of which is which, so every internal state is either exposed (noise) or hidden (silent uncertainty), decided by whoever wrote the field.
- Silence has a second form beyond Day 97's. A state that changes the system's reliability but never reaches the output is silent uncertainty. The pipeline can be provisional, degraded and on probation at the same moment and render as a confident normal output.

## Architecture

### 1. An inventory of every state a consumer can see
The first step is mechanical: walk the output schema and list every field that carries an enumerated state or a flag. The count was 14, from 11 mechanisms.

```
label_family          values                                         from
regime                QUIET | TRANSITION | EVENT                     Day 67-69
drift                 NONE | SUSPECTED | CORROBORATED               Day 65-66
resolution            FINAL | PROVISIONAL                            Day 69
quorum                FULL | SUB_FULL | SINGLE                       Day 70, 97
source_set            FULL | DEGRADED_* | BLIND                      Day 97
feed_state            LIVE | STALE | SUSPECT | DEAD                  Day 46, 97
mech_status           NORMAL | PROBATION | SHADOW                    Day 90
canary_stage          n/a | 1% | 10% | 50% | 100%                    Day 84
verdict_interval      width of confidence interval                   Day 90
trust                 HIGH | LOW | RESET                             Day 59, 64
cluster_epoch         integer                                        Day 87
inconclusive          bool                                           Day 85
stale_read_retry      bool                                           Day 81, 91
beacon                FULL | DEGRADED | BLIND                        Day 97
```

### 2. Three axes the operator reads
The operator needs three answers: what is happening, how sure is the system, and can the system currently see. These become the only things in the headline of the output. Everything else is demoted to a reason list.

```python
class Situation(Enum):
    NOMINAL = "nominal"
    WATCH = "watch"                  # something may be developing
    ALERT = "alert"                  # event in progress, severity attached

class Confidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class Health(Enum):
    FULL = "full"
    DEGRADED = "degraded"
    BLIND = "blind"

@dataclass
class OperatorView:
    situation: Situation
    severity: int | None
    confidence: Confidence
    health: Health
    reasons: list[ReasonCode]         # why confidence or health is not at the top
    action: str                       # one sentence: what the operator should do now
    as_of: float
```

### 3. A mapping from every internal state to an axis, or an explicit "invisible"
Each of the 14 is assigned to exactly one of: contributes to Situation, lowers Confidence, lowers Health, or is declared operator-invisible with a one-line justification. The table is data, and a test fails if a state exists that is not in it.

```python
STATE_MAP = {
    ("regime", "EVENT"):               Axis.SITUATION("alert"),
    ("regime", "TRANSITION"):          Axis.SITUATION("watch"),
    ("drift", "CORROBORATED"):         Axis.SITUATION("watch"),
    ("drift", "SUSPECTED"):            Axis.CONFIDENCE("medium", reason="DRIFT_UNCONFIRMED"),
    ("resolution", "PROVISIONAL"):     Axis.CONFIDENCE("medium", reason="PROVISIONAL_REGIME"),
    ("quorum", "SINGLE"):              Axis.CONFIDENCE("low",    reason="SINGLE_SOURCE"),
    ("quorum", "SUB_FULL"):            Axis.CONFIDENCE("medium", reason="REDUCED_CORROBORATION"),
    ("source_set", "DEGRADED_*"):      Axis.HEALTH("degraded",   reason="FEEDS_MISSING"),
    ("source_set", "BLIND"):           Axis.HEALTH("blind",      reason="NO_FEEDS"),
    ("feed_state", "STALE"):           Axis.HEALTH("degraded",   reason="STALE_FEED"),
    ("mech_status", "PROBATION"):      Axis.CONFIDENCE("medium", reason="MECHANISM_ON_PROBATION"),
    ("trust", "LOW"):                  Axis.CONFIDENCE("low",    reason="LOW_TRUST_ANALYZER"),
    ("cluster_epoch", "*"):            Axis.INVISIBLE("bookkeeping; does not change what the output means"),
    ("canary_stage", "*"):             Axis.INVISIBLE("deployment metadata; confidence effect carried by mech_status"),
}
```

The `INVISIBLE` entries each carry a written reason, so hiding something is a decision on the record, not an accident of where a field landed.

### 4. The "no silent uncertainty" rule as an executable property
The central rule is a property: for any minute in a run, if any internal state that the map assigns to Confidence or Health is active, the rendered view must show a confidence below HIGH or a health below FULL, respectively, with a matching reason code. The property is checked over every replayed minute.

```python
def check_no_silent_uncertainty(run: EventRun) -> list[Violation]:
    violations = []
    for minute in run.minutes():
        internal = run.internal_states_at(minute)
        view = render_view(internal)
        for state in internal:
            axis = STATE_MAP.lookup(state)
            if axis.kind == "CONFIDENCE" and view.confidence.rank > axis.level.rank:
                violations.append(Violation(minute, state, "confidence too high for active state"))
            if axis.kind == "HEALTH" and view.health.rank > axis.level.rank:
                violations.append(Violation(minute, state, "health too high for active state"))
            if axis.kind in ("CONFIDENCE", "HEALTH") and axis.reason not in view.reasons:
                violations.append(Violation(minute, state, "missing reason code"))
    return violations
```

### 5. The action field is derived, and short
Each combination of the three axes maps to a single action sentence from a small table, not generated text, so it is reviewable and testable.

```python
ACTIONS = {
    (Situation.ALERT,   Confidence.HIGH,   Health.FULL):     "Event in progress. Act on severity {sev}.",
    (Situation.ALERT,   Confidence.MEDIUM, Health.FULL):     "Event likely in progress. Act on severity {sev}; confirm with a second source if time allows.",
    (Situation.ALERT,   Confidence.LOW,    Health.DEGRADED): "Possible event, single source. Treat as real until contradicted.",
    (Situation.NOMINAL, Confidence.HIGH,   Health.FULL):     "No action.",
    (Situation.NOMINAL, Confidence.LOW,    Health.BLIND):    "System cannot see. Do not read this as all-clear. Use independent sources.",
}
```

The default rule for combinations not in the table is the most conservative applicable entry, and the table is under test: every reachable (situation, confidence, health) triple must have an action, or the run fails.

### 6. A contract test replaying the earlier days
The contract test runs three replays and checks the rendered output against a rubric derived from ground truth, minute by minute: the storm onset from Day 96, the same onset with each of Day 97's single-feed survivors, and a quiet night with one feed stale.

```python
def contract_test(pipeline_factory):
    cases = [
        ("storm_full_feeds",        EVENT_LIBRARY["fast_storm_onset_a"], all_feeds),
        ("storm_kp_only",           EVENT_LIBRARY["fast_storm_onset_a"], {"kp"}),
        ("storm_no_feeds",          EVENT_LIBRARY["fast_storm_onset_a"], set()),
        ("quiet_one_stale_feed",    QUIET_NIGHT,                          all_feeds - {"solar_wind"}),
    ]
    report = {}
    for name, event, live in cases:
        run = replay_event(pipeline_factory(), event.with_live_feeds(live))
        report[name] = {
            "silent_uncertainty": check_no_silent_uncertainty(run),
            "misleading_minutes": misleading_minutes(run, event),       # rendered view contradicts truth
            "time_to_correct_action_s": time_to_correct_action(run, event),
        }
    return report
```

`misleading_minutes` is defined against the rubric: a minute is misleading if the headline implied calm during a true event with detectable evidence, implied an event during a true quiet period, or showed high confidence with reasons that should have lowered it.

### 7. Results
```
case                       silent_uncertainty  misleading_minutes  time_to_correct_action_s
storm_full_feeds                  0                   2                    310
storm_kp_only                     0                   1                    405
storm_no_feeds                    0                   0                    -- (BLIND shown)
quiet_one_stale_feed              0                   0                    -- (DEGRADED shown)
```

Before the map, the same four cases showed 31 silent-uncertainty violations across them, concentrated in provisional resolution and probation states that raised no confidence reduction. The two misleading minutes in the first case are the pre-estimate lag from Day 96 that was called irreducible then, and the contract now handles them the only honest way, by rendering `WATCH` with `LOW` confidence and the reason `AWAITING_FIRST_ESTIMATE` for that first interval instead of `NOMINAL`.

## Failure Modes
- **No human was involved in this test.** The rubric encodes what the authors believe an operator needs. `time_to_correct_action_s` is the pipeline's output timing, not the time a person takes to read and decide. Whether three axes, a reason list and one action sentence are comprehensible under stress is an empirical question about people, and the series has run zero experiments on people. The contract can be correct and still unusable.
- **Collapsing 14 states to three axes loses information some operators need.** An analyst deciding whether to trust a feed may want the raw `feed_state`. The reasons list preserves it, and the full payload remains available in a detail view, but the headline decides what gets read first.
- **The mapping table is the system's central piece of judgment, and it was written by one person in a day.** Mapping `drift=SUSPECTED` to a confidence reduction rather than to a situation change is a choice. A different operator might read it the other way, and a wrong choice here changes the outcome far more than any threshold from Day 94.
- **Hiding a state behind `INVISIBLE` removes it from the operator's view permanently, and the justification is a sentence.** The `cluster_epoch` and `canary_stage` entries are fine today. A future mechanism that makes an invisible state operator-relevant has no automatic trigger to move it.

## What's Next
The series has two days left. Ninety-eight days of mechanisms, checks, replays, audits and contracts have produced a long list of claims about the system: that the gate catches what it declares, that the baselines are sound, that the thresholds are tuned, that the pipeline degrades honestly. Some of those claims are backed by measurements that can be reproduced. Some are backed by one replay on four events. Some are backed by a design post and good intentions. Day 99 writes the claims ledger: every claim the series has made about the system, with the evidence that supports it, an honest rating of how strong that evidence is, and what would change the rating.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
