# Day 69: Transition Timeout — Forcing a Decision When Certainty Doesn't Arrive

## TL;DR
Day 68's hysteresis fixed the hard-cutoff contamination problem but created an unbounded holding pen: if a corroboration source flickers right at the enter/exit threshold without ever accumulating enough consecutive checks in either direction, commits pile up in the transition regime indefinitely, with detection suppressed the whole time. Today's fix adds a bounded timeout. If a transition hasn't resolved naturally within a fixed window, the system commits to a best-guess regime using the best evidence available, rather than waiting for a certainty that may never come — with the guess explicitly flagged as provisional so it can be corrected later if better evidence arrives.

## The Problem
- Day 68's `resolve_transition` only fires when hysteresis naturally confirms a direction — `consecutive_active() >= ENTER_EVENT_CONSECUTIVE_CHECKS` or the symmetric exit condition. Nothing fires if neither condition is ever met.
- A corroboration source can flicker indefinitely at exactly the wrong cadence: active, inactive, active, inactive, each run shorter than the required consecutive-check threshold, resetting the counter every time without ever reaching it. This isn't a theoretical edge case — Day 66 already flagged that external corroboration sources have their own latency and reliability characteristics, and a source reporting intermittently, rather than cleanly, is a realistic failure mode for exactly the kind of third-party feed this system depends on.
- While stuck, Day 68's detection suppression (its point 4) means the shape gets zero drift protection for the entire duration — which was an acceptable, bounded tradeoff for a normal transition window, but becomes an unacceptable, unbounded blind spot if the transition never resolves.
- Commits accumulating in the transition regime also accumulate unresolved resume decisions, per Day 68's own Failure Modes — the longer a transition drags on, the more decisions downstream are made against data that was never properly classified, and the larger the eventual correction has to be when it does resolve.

## Architecture

### 1. Transition deadline
Every time a shape enters the transition regime, a deadline is set. If the deadline passes before hysteresis naturally resolves the direction, the timeout path takes over instead of continuing to wait.

```python
TRANSITION_TIMEOUT = timedelta(minutes=30)   # tuned per shape; space-weather events move faster than orbital-debris ones

def enter_transition(shape):
    REGIME_STATE[shape].transition_deadline = now() + TRANSITION_TIMEOUT
```

### 2. Best-guess resolution via accumulated evidence
At timeout, rather than picking a direction arbitrarily, the system looks at which status the corroboration source reported for the plurality of checks during the transition window — not requiring consecutive checks, just the majority — and resolves provisionally toward that.

```python
def resolve_on_timeout(shape):
    history = REGIME_STATE[shape].transition_check_history
    majority_status = history.majority_status()  # "active" or "inactive", by count, not streak
    provisional_regime = "event" if majority_status == "active" else "quiet"
    resolve_transition(shape, provisional_regime, provisional=True)   # Day 68's resolver, flagged provisional
```

### 3. Provisional flag enables cheap correction later
A provisionally-resolved transition is marked so that if the corroboration source later settles clearly in the *other* direction, the system doesn't need a second full hysteresis cycle to fix it — it can re-run Day 68's retroactive relabeling (which already rehashes dependent records in place, per Day 64's mechanics) directly against the newly confirmed direction, without waiting through another timeout.

```python
def check_provisional_correction(shape):
    if REGIME_STATE[shape].provisional and clearly_resolved_elsewhere(shape):
        resolve_transition(shape, confirmed_regime(shape), provisional=False)
```

### 4. Escalating timeout on repeated flapping
A shape that hits the timeout path repeatedly in a short span is a signal that its corroboration source itself is unhealthy, not just that one event happened to be ambiguous. Each repeated timeout for the same shape within a rolling window extends the next timeout window rather than keeping it fixed, trading faster decisions for a steadier one once a pattern of flakiness is evident.

```python
def next_timeout_duration(shape):
    recent_timeouts = count_timeouts_in_window(shape, window=timedelta(hours=6))
    return TRANSITION_TIMEOUT * (1 + 0.5 * recent_timeouts)   # back off, don't just repeat
```

## Failure Modes
- **A majority vote isn't the same as confidence.** Resolving on a bare plurality (point 2) means a nearly 50/50 split still produces a confident-looking regime label, even though the evidence barely favored one side — the provisional flag (point 3) mitigates the consequence but doesn't change how shaky the initial call was.
- **Provisional corrections can themselves thrash.** If a shape oscillates near the timeout boundary repeatedly, point 3's correction path can fire more than once for the same event, each time rehashing dependent records again — cheaper than a full hysteresis cycle, but not free, and nothing caps how many times a single event's classification can flip.
- **Escalating timeouts delay legitimate detection recovery.** Point 4's backoff is meant to stabilize a flaky source, but it also means a shape that's had a rough few hours keeps a longer detection-blind window even after the source recovers, since the backoff only resets after enough quiet time passes, which isn't explicitly defined yet.
- **The timeout duration is yet another per-shape constant.** On top of the hysteresis thresholds from Day 68, operators now need to reason about a third tunable value, and getting it wrong in either direction either forces premature guesses (too short) or extends the blind window near the original unbounded-risk level (too long).
- **No upper bound on provisional-correction churn.** Nothing stops an adversarial or simply malfunctioning corroboration source from keeping a shape in a permanent provisional/correction loop, repeatedly triggering point 3 without ever reaching a stable, confirmed state.

## What's Next
Day 70: the deeper issue underneath today's fix is that one corroboration source is being trusted as the sole arbiter of regime truth, and its own unreliability becomes this system's unreliability. The next step is corroboration redundancy — comparing multiple independent sources against each other before trusting any single one's status, rather than building increasingly elaborate timeout logic around trusting just one flaky feed.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
