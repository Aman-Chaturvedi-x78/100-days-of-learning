# Day 73: A Threshold Registry — Making the Stack Reasoned About Instead of Just Reused

## TL;DR
Day 72 flagged the real problem directly: field-value drift (65), regime-segmented baselines (67), and correlation drift (72) all reuse the same rolling-baseline-plus-persistence-gate shape, but each layer introduced its own independently-tuned constants with no shared view of how they interact. Today isn't a new detector. It's a threshold registry — a single place every persistence-gated mechanism in this arc registers its tunable constants, declares what it depends on, and exposes its current state for inspection, so a given decision ("why is this shape quarantined right now") can be traced across all three layers at once instead of requiring someone to separately remember where each threshold lives and what it means.

## The Problem
- Counting from Day 56 onward, this arc has accumulated independently-defined thresholds: coverage confidence run-count (56), analyzer trust agreement rate and `min_checks` (59), diagnostic window length (60), branch-family probation count (63), upstream-epoch replay sample size (64), drift persistence `min_persisting_commits` (65), hysteresis enter/exit consecutive-check counts (68), transition timeout duration and backoff factor (69), quorum fraction (70), pairwise-correlation threshold and `min_events` (71), and now correlation-shift persistence `min_persisting_events` (72). Every one of these lives as a bare module-level constant or a function default, defined where its mechanism was built and nowhere else.
- No two of these were ever designed with awareness of the others' current values. A `min_persisting_commits=50` for field drift (65) and a `min_persisting_events=20` for correlation drift (72) were each chosen in isolation, but they jointly determine how long a real upstream problem can hide behind a flickering correlation classification before anything downstream reacts — and nobody has ever actually computed that combined latency.
- Debugging a specific decision today means manually walking backward through up to a dozen separate mechanisms, each defined in its own section of a growing design document, to answer "why did this branch get quarantined on this particular day." The information to answer that question exists, scattered across eight days of posts, but nothing collects it into one place at runtime.
- This is the direct, concrete cost Day 72 named only abstractly: the stack isn't failing, but it has become the kind of system where adding a thirteenth threshold is now a bigger risk than any single mechanism's own failure modes, because nobody — human or system — has a complete picture of how the twelve already in place interact.

## Architecture

### 1. A single threshold registry
Every tunable constant introduced since Day 56 is re-registered through one shared structure, keyed by the mechanism that owns it, replacing the scattered module-level defaults with declarations that carry metadata about what the threshold does and what else it affects.

```python
@dataclass
class Threshold:
    owner: str              # e.g. "day65_drift_persistence"
    value: float
    unit: str                # "commits", "events", "seconds", "fraction"
    affects: list[str]       # names of other thresholds/mechanisms whose behavior depends on this one
    rationale: str

THRESHOLD_REGISTRY: dict[str, Threshold] = {
    "drift_persistence_commits": Threshold(
        owner="day65", value=50, unit="commits",
        affects=["quarantine_latency", "correlation_persistence_events"],
        rationale="filters single-batch noise before treating a shift as confirmed",
    ),
    "correlation_persistence_events": Threshold(
        owner="day72", value=20, unit="events",
        affects=["quorum_vote_count", "quarantine_latency"],
        rationale="filters coincidental agreement before re-grouping a source pair",
    ),
    # ... the remaining ~10 thresholds from Days 56-71, each declared the same way
}
```

### 2. Derived latency computation
Because every threshold now declares what it `affects`, the registry can compute compound quantities that no single mechanism's code currently exposes — like the worst-case time between a genuine upstream problem occurring and the system's full pipeline (drift detection → corroboration → regime classification → quorum → quarantine) actually reacting to it.

```python
def worst_case_detection_latency(shape):
    chain = resolve_dependency_chain(THRESHOLD_REGISTRY, start="drift_persistence_commits")
    return sum(estimate_wall_time(t, shape) for t in chain)
```

### 3. Decision trace, not just a decision
Every quarantine, regrouping, or regime transition now logs which specific threshold values were in force at the moment the decision was made, not just the outcome — so "why was this branch quarantined on this day" becomes a lookup against the trace rather than a reconstruction from first principles.

```python
def log_decision(mechanism, outcome, context):
    DECISION_LOG.append({
        "mechanism": mechanism,
        "outcome": outcome,
        "thresholds_in_force": {k: v.value for k, v in THRESHOLD_REGISTRY.items() if k in context.relevant_thresholds},
        "timestamp": now(),
    })
```

### 4. Change impact preview before adjusting any one threshold
Because the registry knows the `affects` graph, changing one threshold's value can be previewed against everything downstream of it before the change is committed — surfacing, for instance, that lowering Day 65's persistence count by half would also roughly halve the detection latency computed in point 2, rather than requiring someone to manually trace the consequence through eight days of separate mechanisms.

```python
def preview_change(threshold_name, new_value):
    affected = transitive_closure(THRESHOLD_REGISTRY, threshold_name)
    return {name: estimate_behavior_change(name, threshold_name, new_value) for name in affected}
```

## Failure Modes
- **The registry is a map, not a fix.** Nothing about today's change makes any individual threshold better-chosen — a badly tuned constant is now visible and traceable, but still badly tuned. This is explicitly a visibility and reasoning layer, not a correctness improvement to any mechanism built on Days 56 through 72.
- **The `affects` graph has to be maintained by hand.** Declaring which thresholds affect which others (point 1) is itself a manual judgment call made today, retroactively, about mechanisms built over many prior days — it's as likely to have gaps or errors as any of the thresholds it's describing, and nothing validates that the declared graph matches the code's actual behavior.
- **Derived latency estimates are approximations.** `worst_case_detection_latency` (point 2) sums estimated wall-clock contributions along a dependency chain, but several of the underlying mechanisms have highly variable real-world timing (external corroboration-source latency from Day 66, quorum resolution waiting on the slowest source from Day 70) — the computed number is a rough upper bound, not a guarantee.
- **The decision log adds its own unbounded growth.** Point 3's full-context logging on every decision is more state accumulating on top of everything already tracked — path fingerprints, code versions, access surfaces, branch families, upstream epochs, regimes, and now decision traces — with no retention or pruning policy defined yet.
- **A registry doesn't stop a fourteenth threshold from being added the old way.** Nothing enforces that a future mechanism actually registers through this structure rather than reintroducing another bare module-level constant — the registry only has unifying value if its use is a convention everyone actually follows going forward.

## What's Next
With the threshold registry in place, the natural next question is whether `worst_case_detection_latency` across the full chain is actually acceptable for the kind of events Orbital Watch needs to react to quickly — a number that's never been computed before today, and that the next day's work should probably check against what a real solar-storm-scale event actually needs.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
