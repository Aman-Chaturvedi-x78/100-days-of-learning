# Day 76: Burst Stress Testing — Finding Out If the Concurrency Actually Holds

## TL;DR
Day 75 introduced real concurrency into a pipeline that had never had any, with per-key locking as its only correctness guard, and flagged that guard as untested against the exact scenario it exists to serve: a burst of commits arriving together during a fast-escalating event. Today builds that test — a synthetic burst generator that replays realistic concurrent commit patterns against the task graph — and it finds a real bug. Not in the locking itself, but in an assumption Day 75's dependency declarations made about task identity: two commits racing through `run_stage_when_ready` for the *same* operation shape but *different* commits were sharing memoized per-stage task futures that were only ever supposed to be scoped to one commit.

## The Problem
- Day 75's `process_commit` builds a fresh `tasks` dict per call, which looks commit-scoped, but `STAGE_IMPLEMENTATIONS` and the underlying `FIELD_PROFILES` / `PAIRWISE_PROFILES` state it reads and writes are shared across every concurrent invocation of `process_commit` for the same shape — which was always the intended design (profiles need to accumulate across commits), but nothing in Day 75's design explicitly reasoned about what happens when two `process_commit` calls for the *same shape* are in flight *simultaneously*, racing each other through the same stage sequence against overlapping state.
- A burst stress test is the obvious way to find this, and until today, no such test existed — every mechanism in this arc from Day 56 onward was validated by walking through its logic by hand or against a single synthetic event, never against the kind of realistic concurrent load a severe event actually produces, which was the explicit gap flagged at the end of Day 75.
- The synthetic burst generator reveals the specific bug: Day 68's hysteresis state (`REGIME_STATE[shape]`) and Day 69's transition deadline tracking are read-modify-write operations that were never wrapped in Day 75's per-key locks at all — point 4 of Day 75 only locked `FIELD_PROFILES` and `PAIRWISE_PROFILES`, because those were the structures the day's own reasoning happened to focus on. Regime state was a mutable structure just as exposed to concurrent access, and it was missed.
- Under burst load, two commits for the same shape can both read `REGIME_STATE[shape].consecutive_active` as N, both increment it to N+1 in their own local computation, and both write N+1 back — silently losing one increment. For a signal that's supposed to require `ENTER_EVENT_CONSECUTIVE_CHECKS` consecutive confirmations, a lost increment under exactly the high-volume burst conditions a real event produces means hysteresis can take longer to confirm an event than its own design intended, in the specific situation where fast confirmation matters most.

## Architecture

### 1. Synthetic burst generator
A test harness that replays a configurable number of commits for the same shape within a tight time window, with realistic field-value distributions drawn from the regime profiles already being tracked (reusing Day 65's and Day 67's profile machinery as the source of realistic synthetic data, rather than inventing a separate fixture format).

```python
async def generate_burst(shape, count, window_seconds, severity="high"):
    baseline = BASELINE_PROFILES[(shape, field_for_shape(shape), "quiet")]
    commits = [synthesize_commit(shape, baseline, severity) for _ in range(count)]
    await asyncio.gather(*(process_commit(c) for c in commits))
```

### 2. Invariant checks run after every burst
Rather than asserting a specific outcome (which varies with scheduling), the test checks invariants that must hold regardless of execution order — total state transitions should never exceed the number of commits that could have caused them, and no stage's state should diverge from what a fully serial replay of the same commits would have produced.

```python
def assert_no_lost_updates(shape, serial_replay_result, concurrent_result):
    assert concurrent_result.consecutive_active <= len(serial_replay_result.commits)
    assert concurrent_result.final_regime == serial_replay_result.final_regime, \
        "concurrent execution diverged from serial ground truth"
```

### 3. The fix: extending Day 75's locking to regime state
`REGIME_STATE` and the transition-deadline tracking introduced in Day 68/69 get the same per-key (per-shape) lock treatment Day 75 gave to field and pairwise profiles — closing the specific gap the burst test surfaced, not introducing a new locking strategy.

```python
async def update_regime_state(shape, source_status_history):
    async with REGIME_STATE_LOCKS[shape]:   # the lock Day 75 should have included here too
        if current_regime(shape) == "quiet":
            if source_status_history.consecutive_active() >= ENTER_EVENT_CONSECUTIVE_CHECKS:
                transition_regime(shape, to="event")
        else:
            if source_status_history.consecutive_inactive() >= EXIT_EVENT_CONSECUTIVE_CHECKS:
                transition_regime(shape, to="quiet")
```

### 4. Burst tests become a standing gate, not a one-off
Rather than running the burst generator once to find this bug and moving on, it's wired into the same decision-log infrastructure Day 73 built — every new stage or threshold added to the pipeline going forward must pass a burst-invariant check before being considered complete, closing off the exact failure mode (a new piece of mutable state introduced without locking being noticed) that let today's bug through in the first place.

```python
def register_stage(stage_name, implementation, mutable_state_keys):
    STAGE_IMPLEMENTATIONS[stage_name] = implementation
    BURST_TEST_REGISTRY[stage_name] = mutable_state_keys  # must declare what it touches
    assert all(key in LOCK_REGISTRY for key in mutable_state_keys), \
        f"{stage_name} touches unlocked state: {mutable_state_keys}"
```

## Failure Modes
- **The burst generator only tests what it's told to generate.** `synthesize_commit`'s realism depends entirely on how well the chosen severity profile matches an actual solar-storm-scale event's real commit cadence and value distribution — a burst test with an unrealistic arrival pattern could pass cleanly while missing a race condition that only manifests at the specific concurrency level a true severe event produces.
- **Invariant checking against a serial replay has its own cost.** `assert_no_lost_updates` requires computing the serial ground truth for every burst test, which means every test run pays for both the concurrent execution being tested and a full sequential replay to compare against — this is fine for a test suite but means the technique doesn't translate directly into a cheap runtime sanity check.
- **Point 4's registration gate only catches state a developer remembers to declare.** `mutable_state_keys` is a manually provided list when registering a new stage, which is exactly the same kind of hand-maintained declaration that caused today's bug in the first place (Day 75's own locking coverage was an unstated assumption, not a declared and checked one) — the gate raises the bar but doesn't eliminate the possibility of a future stage under-declaring what it touches.
- **Fixing the found bug doesn't prove there are no others.** Today's burst generator found one specific race by chance of what it happened to stress — `REGIME_STATE` being read-modify-write under contention. There's no proof the generator's current test scenarios cover every mutable structure in the pipeline; it only proves the ones it actually exercised.
- **Burst testing adds meaningful CI/test-suite runtime.** Running enough burst scenarios to have real confidence (varying severity, commit count, and timing jitter) multiplies test execution time considerably compared to the single-event-at-a-time testing this arc has relied on through Day 75, which is a real tradeoff against fast iteration during future development.

## What's Next
Day 77: today's fix closed the one gap the burst generator happened to find, but the generator itself was only run against a handful of scenario shapes. The next step is widening its coverage systematically — generating burst scenarios across the full combinatorial space of severity, shape, and timing rather than the specific cases chosen by hand today, since the bug found today was found by luck as much as by design.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
