# Day 97: When the Source Dies Mid-Event — Degraded Modes, Honest Capability, and a Heartbeat

## TL;DR
Day 96's replays kept every feed alive. In a real extreme event, the feeds a space-weather pipeline depends on are under load, delayed, or gone, and nothing in the system has been tested for that. Today crosses the three-class event library with a source-loss matrix, the Day 77 approach of combinatorial scenarios applied to availability. 4 events times 8 loss patterns is 32 scenarios, and the matrix found a silent failure worse than any earlier one: with the primary solar-wind feed down during a storm onset, the quorum (Day 70) could not form, the pipeline emitted no alert, and its output stream looked identical to a quiet night. For 11 minutes the system reported nothing, indistinguishable from reporting that nothing was wrong. The fix has three parts: an explicit capability matrix that says what the system can still claim with a given set of live feeds, a quorum that adapts to the feeds available instead of counting absentees as dissenters, and a capability beacon that gives silence a meaning.

## The Problem
- Quorum logic (Day 70) was designed around disagreement: sources arrive and say different things. It has no concept of a source that does not arrive. Its denominator is the registered source set, so a feed that is down counts the same as a feed that voted no.
- With three corroborating feeds, requiring agreement from two is robust against one lying or lagging source. With one down, the two survivors must both agree, which is a stricter rule than the design intended. With two down, agreement is impossible, and the quorum can never form no matter how clear the evidence from the survivor is. The system fails toward silence in exactly the situation where a single remaining feed is screaming.
- The suspect-state machinery (Day 46) distinguishes a dead source from a silent one using a relay probe, and the timeout machinery (Day 69) bounds waits. Both produce an internal verdict. Neither says anything outward. Whether the pipeline is blind or merely quiet is not something an operator can read from its output.
- Silence is therefore ambiguous in the worst way. A system that alerts when something happens and says nothing otherwise provides no information in the absence of an alert unless it also guarantees it would have alerted. That guarantee depends on how many feeds are alive, and the output carries no trace of it.

## Architecture

### 1. A source-loss matrix crossed with the event library
Loss patterns are defined over the independent feed groups the pipeline has (after Day 71's correlation collapsing): the solar wind feed, the planetary K nowcast, the alerts feed, and, for the non-space-weather classes, the catalog and orbit-solution feeds. For a three-feed class there are 8 subsets of live feeds, including all live and none live.

```python
def loss_patterns(feed_groups: list[str]) -> list[frozenset]:
    patterns = []
    for r in range(len(feed_groups) + 1):
        for live in combinations(feed_groups, r):
            patterns.append(frozenset(live))
    return patterns                    # 2^n subsets of live feeds

def run_matrix(pipeline_factory, events, loss_mode="dead_from_onset_minus_5m"):
    results = []
    for event in events:
        for live in loss_patterns(event.feed_groups()):
            degraded = event.with_feeds_removed(set(event.feed_groups()) - live, mode=loss_mode)
            run = replay_event(pipeline_factory(), degraded)
            results.append((event.name, live, score_event(run), silent_minutes(run, event)))
    return results
```

Loss modes include `dead_from_onset_minus_5m` (feed disappears just before the event), `dies_at_peak`, `intermittent` and `stale_but_responding` (the feed keeps answering with old data, the nastiest form). The first mode ran for all 32 scenarios, and the others on the eight worst.

### 2. The metric that matters: silent minutes
The score from Day 96 is extended with a quantity defined for this problem: the number of minutes during the event window in which the pipeline's output was indistinguishable from its output on a quiet night, while the ground truth said an event was in progress and the live feeds contained enough information to detect it.

```python
def silent_minutes(run: EventRun, event: ExtremeEvent) -> int:
    quiet_signature = canonical_quiet_output()
    count = 0
    for minute in event.truth.window_minutes():
        out = run.output_at(minute)
        if equivalent(out, quiet_signature) and event.detectable_from_live_feeds(minute, run.live_feeds):
            count += 1
    return count
```

The "detectable from live feeds" clause matters. A minute that is silent because no remaining feed carries any signal is a legitimate limit. A minute that is silent while one remaining feed clearly shows a severe storm is a bug.

### 3. The finding
```
event: fast_storm_onset_a
live feeds                          alert_delay_s   silent_minutes   misleading
solar_wind + kp + alerts                310              0               2
kp + alerts                             340              0               2
solar_wind + alerts                     420              0               3
solar_wind + kp                         390              0               2
alerts only                             --               9               0
kp only                                 --              11               0     <- silent, one feed screaming
solar_wind only                         --              11               0
none                                    --              (blind)          0
```

For single-feed survivors the pipeline emitted nothing for 9 to 11 minutes while that feed showed a severe storm. The alerts feed, which is an official product, was alive in two of those rows and still produced silence because the quorum needed a second corroborating feed to trust it.

### 4. A capability matrix: what the system can honestly claim
The fix starts by writing down, per output claim, what evidence it requires and what confidence it can carry as evidence thins. Claims become a lookup, not a side effect of quorum arithmetic.

```python
CAPABILITY_MATRIX = {
    "storm_alert": [
        # (required live feed groups, confidence cap, label)
        ({"solar_wind", "kp", "alerts"}, 0.95, "FULL"),
        ({"kp", "alerts"},                0.85, "FULL"),
        ({"solar_wind", "kp"},            0.80, "FULL"),
        ({"alerts"},                      0.70, "DEGRADED_SINGLE_OFFICIAL"),
        ({"kp"},                          0.55, "DEGRADED_SINGLE_DERIVED"),
        ({"solar_wind"},                  0.55, "DEGRADED_SINGLE_DERIVED"),
        (set(),                           0.00, "BLIND"),
    ],
}

def capability_for(claim: str, live: set[str]) -> tuple[float, str]:
    best = (0.0, "BLIND")
    for required, cap, label in CAPABILITY_MATRIX[claim]:
        if required <= live and cap > best[0]:
            best = (cap, label)
    return best
```

A single official feed is worth more than a single derived one, and the matrix says so out loud, where it was formerly implicit in which feeds the quorum happened to include.

### 5. A quorum that denominates over live sources
The quorum's threshold now applies to the set of feeds that are live, and the confidence cap from the capability matrix limits what the output may claim. A down feed no longer counts as a dissenter. A feed in suspect state (Day 46) is excluded from the denominator and noted.

```python
def adaptive_quorum(votes: dict[str, bool], registered: set[str], live: set[str]) -> QuorumResult:
    voters = {s: v for s, v in votes.items() if s in live}
    if not voters:
        return QuorumResult(decision=None, label="BLIND")
    agree = sum(1 for v in voters.values() if v)
    needed = max(1, ceil(len(voters) * QUORUM_FRACTION))   # fraction of LIVE sources
    cap, label = capability_for("storm_alert", set(voters))
    return QuorumResult(decision=agree >= needed, confidence_cap=cap, label=label,
                        missing=sorted(registered - live))
```

For one live feed the rule degenerates to "that feed's vote counts," with a lower confidence ceiling and a `DEGRADED_*` label. A single-feed alert says it is a single-feed alert, and the operator can weigh it.

### 6. Telling dead from stale
The nastiest loss mode, a feed that responds with old data, defeats liveness checks based on response. Each feed is judged live only if its newest payload timestamp is within its expected cadence plus tolerance, using Day 46's relay probe as the second opinion before declaring a feed dead.

```python
def feed_state(feed, now) -> str:
    age = now - feed.latest_payload_ts
    if age <= feed.cadence_s * 2:
        return "LIVE"
    if relay_probe(feed).responds_with_fresh_data():
        return "LIVE"                      # our fetch path is the problem, not the feed (Day 46)
    return "STALE" if feed.responds() else "DEAD"
```

### 7. A capability beacon: giving silence a meaning
Alerts only mean something if their absence is meaningful. The pipeline now emits a beacon at a fixed cadence regardless of events: which feed groups are live, the maximum claim each class of output can currently support, and a timestamp. A downstream consumer treats a missing beacon as the pipeline being down, a beacon showing `BLIND` as a loss of sight, and a beacon showing `FULL` with no alert as an actual all-clear. This is the heartbeat idea from Day 37 applied to the output instead of to agent leases.

```python
def emit_beacon(pipeline, now):
    live = {f.group for f in pipeline.feeds if feed_state(f, now) == "LIVE"}
    return Beacon(
        ts=now,
        live_feed_groups=sorted(live),
        capability={claim: capability_for(claim, live) for claim in CAPABILITY_MATRIX},
        next_beacon_by=now + BEACON_CADENCE_S * 2,
    )
```

### 8. The matrix after the fix
```
event: fast_storm_onset_a
live feeds                          alert_delay_s   silent_minutes   label
solar_wind + kp + alerts                310              0           FULL
alerts only                             335              0           DEGRADED_SINGLE_OFFICIAL
kp only                                 405              0           DEGRADED_SINGLE_DERIVED
solar_wind only                         430              0           DEGRADED_SINGLE_DERIVED
none                                    --             (BLIND, beacon says so)
```

Across all 32 scenarios `silent_minutes` is zero wherever the live feeds carry a detectable signal, and the `none` rows are explicit in the beacon, so operators see "I cannot see" instead of "nothing happening." The `stale_but_responding` mode caught on the first try with the age check and missed only in the case where a feed's stale payload carried a fresh-looking timestamp, which is discussed below.

## Failure Modes
- **The capability caps are judgments, not measurements.** 0.70 for a single official feed and 0.55 for a single derived feed are the system authors' estimates. They could be calibrated against Day 89's outcome corpus, but that corpus has no single-feed events in it. The numbers carry the same risk as the Day 96 deadline: plausible and unvalidated.
- **A feed that lies about its own timestamp defeats the staleness check.** The age rule trusts the payload's timestamp. A feed re-serving old content with a refreshed header would be judged live. Defending against that requires content-level freshness (change detection against the previous payload), which the beacon does not yet do.
- **The beacon is another process that can die.** A consumer that treats beacon absence as "pipeline down" is only safe if the beacon is produced on a path as independent as possible from the one that produces alerts. Today they share a scheduler, and a scheduler stall would silence both in a way the consumer reads, correctly, as a down pipeline. That is acceptable and it is not free.
- **Adaptive quorum lowers the bar for alerting when feeds are missing.** The right trade in a storm is not obviously right in a feed outage with a spurious spike in the one remaining feed. The cap and the `DEGRADED_*` label bound the damage by telling the operator how thin the evidence is, which assumes an operator reads the label.

## What's Next
The pipeline now emits several distinct kinds of internal uncertainty: regime in transition, drift suspected, provisional resolution, a mechanism on probation, a degraded source set, a quorum below full strength, a stale feed. Each was added by a different day for a good reason. Somebody on the other end has to read all of that. Day 98 asks what the operator actually sees: how fourteen internal states collapse into something a person can act on in the minute they need to.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
