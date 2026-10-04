# Day 68: Regime Transition Hysteresis — The Edges of an Event Are Not the Event

## TL;DR
Day 67 split baselines by regime so event data and quiet data stop blending, but it classifies every commit by a single timestamp check against the corroboration source: active now, or not. Data captured right as a storm ramps up or winds down sits in a genuine gray zone — not yet confirmed event, not cleanly quiet either — and a hard cutoff forces it into whichever regime happened to be active at that exact instant, contaminating that regime's baseline with data that doesn't really belong to it. Today's fix applies the same hysteresis pattern Day 39 used for shard split/merge flapping: separate, asymmetric thresholds for entering and leaving a regime, and a distinct holding state for data captured during the transition itself.

## The Problem
- Day 67's `regime_for_commit` is a point-in-time check: query the corroboration source at the commit's timestamp, label it quiet or event based on whether anything is active right then. There is no concept of "approaching an event" or "an event that's winding down" — only "is" or "isn't," evaluated independently for every single commit.
- Real events don't start or stop instantaneously in the data, even when a corroboration source reports a clean start/end timestamp. A solar storm's effect on field values ramps up before the alert fires and tails off after it clears, so commits near the boundary get labeled by whichever side of an arbitrary timestamp they fall on, not by what their values actually look like.
- This reintroduces a smaller version of Day 67's original problem one level down. A handful of boundary commits with storm-influenced values labeled "quiet" (because the alert hadn't technically fired yet) widen the quiet baseline exactly the way unsegmented data did before Day 67 — just less severely, since it's a handful of commits instead of an entire event's worth.
- The inverse also happens: late-arriving quiet-period data mislabeled "event" because the alert was technically still active narrows the event baseline toward values that don't represent the event's actual behavior, making the event regime's own detector (introduced to catch provider bugs during real events, per Day 67) less accurate right when it matters.
- Day 39 already solved the structurally identical problem — flapping between adjacent states driven by a signal crossing a threshold — with asymmetric enter/exit thresholds and a cooldown. Nothing from that solution has been reused here yet, even though the shape of the problem is the same: a binary decision being made on a noisy boundary signal.

## Architecture

### 1. Asymmetric enter/exit thresholds
Rather than transitioning regime label the instant a corroboration source's status flips, require the new status to hold for a minimum number of consecutive checks before the transition actually takes effect — and use different thresholds for entering an event regime versus leaving one, mirroring Day 39's hysteresis.

```python
ENTER_EVENT_CONSECUTIVE_CHECKS = 3   # confirm quickly: missing a real event start is costly
EXIT_EVENT_CONSECUTIVE_CHECKS = 8    # confirm slowly: a storm's tail can be noisy/intermittent

def update_regime_state(shape, source_status_history):
    if current_regime(shape) == "quiet":
        if source_status_history.consecutive_active() >= ENTER_EVENT_CONSECUTIVE_CHECKS:
            transition_regime(shape, to="event")
    else:
        if source_status_history.consecutive_inactive() >= EXIT_EVENT_CONSECUTIVE_CHECKS:
            transition_regime(shape, to="quiet")
```

The asymmetry is deliberate: entering an event regime quickly protects against Day 66's original problem (false-positive quarantine during a real event that hasn't been confirmed yet), while exiting slowly protects against prematurely dumping storm-tail data back into the quiet baseline.

### 2. A distinct transition regime for boundary commits
Commits captured between the first signal of a status change and the hysteresis threshold being met are neither confidently quiet nor confidently event. They get their own regime label, `"transition:<shape>"`, with its own small, short-lived profile — never merged into either the quiet or event baseline while the transition is unresolved.

```python
def regime_for_commit(shape, field, timestamp):
    state = REGIME_STATE[shape]
    if state.in_transition:
        return f"transition:{shape}"
    return state.current_regime  # "quiet" or "event:<type>", per Day 67
```

### 3. Retroactive relabeling on resolution
Once hysteresis confirms a transition (point 1), the commits that were held in the transition regime are relabeled according to which direction the transition actually resolved — and retroactively folded into the correct baseline using the same rehash-in-place mechanics Day 64 built for producer-epoch changes, since this is structurally the same problem: data that was classified provisionally needs to be corrected after the fact without looking like fresh drift to downstream comparisons.

```python
def resolve_transition(shape, resolved_regime):
    transition_commits = pop_transition_commits(f"transition:{shape}")
    for commit in transition_commits:
        reclassify_commit(commit, resolved_regime)   # folds into the correct FIELD_PROFILES bucket
        rehash_dependent_records(commit, resolved_regime)  # Day 64-style in-place correction
```

### 4. Transition-regime detection suppression
While a shape is in the transition regime, Day 65/67's drift detection is suppressed for it entirely rather than compared against either baseline — there's no reliable baseline for "ambiguous boundary data" to compare against, and forcing a comparison against either side would just manufacture false signals out of the uncertainty itself.

```python
def detect_shift(shape, field, regime):
    if regime.startswith("transition:"):
        return None   # no detection during an unresolved transition; resolve first, detect after
    # ... Day 67's regime-aware comparison otherwise
```

## Failure Modes
- **Decisions made during the transition window are provisional.** Any resume decision, trust update, or baseline contribution made while a commit sits in the transition regime is based on an incomplete picture — point 3's retroactive correction fixes the baseline, but any downstream resume decision already made against uncorrected data isn't automatically revisited, echoing the same decide-now-correct-later lag flagged for diagnostic mode back on Day 60.
- **Two new tunable constants instead of one.** Hysteresis trades one hard-cutoff problem for two heuristic thresholds (enter and exit counts) that need separate tuning per shape, and there's no principled way yet to derive them beyond "enter fast, exit slow" as a qualitative rule of thumb.
- **Transition regime as an unbounded holding pen.** If a corroboration source's status flickers right at the hysteresis boundary — not quite enough consecutive checks to confirm either direction — commits can accumulate in the transition regime indefinitely without ever resolving, and nothing today bounds how long that's allowed to continue.
- **Yet another axis of state.** Transition regimes multiply the bookkeeping already spread across path fingerprints, code versions, access surfaces, branch families, upstream epochs, and now quiet/event regimes — each boundary event now briefly creates its own transient regime bucket on top of all of that.
- **Suppressing detection creates a blind window.** While a shape sits in transition, point 4 means no drift detection runs for it at all — a genuine provider-side problem that happens to coincide with a real event's boundary gets a free pass for the duration of the transition, which is a narrower version of the coincidental-corroboration risk flagged back on Day 66.

## What's Next
Day 69: the unbounded transition-regime risk flagged above is the immediate next problem. If a corroboration source never settles into a clean confirmed state — flickering indefinitely, or simply slow to finalize — commits can sit in limbo forever. The next step is a timeout: a bounded window after which the system commits to a best-guess regime rather than waiting indefinitely for certainty that may not come.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
