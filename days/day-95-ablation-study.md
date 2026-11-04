# Day 95: Turn Each One Off — An Ablation Study of 64 Mechanisms

## TL;DR
Ninety-four days of this series added things. Each day introduced a mechanism to fix a failure the previous day exposed, and each one made sense at the time. Nobody has asked the opposite question: if a mechanism were removed, would anything measurable change? Day 94 gave a hint. Of 118 tunable constants, 61 contribute almost nothing to outcomes, and constants exist because mechanisms need them. Today runs the experiment the series has avoided. For each of the 64 live mechanisms, replace it with its declared safe default (pass-through, or the pre-mechanism behavior), replay the same 90 days of recorded traffic, and measure the change in the three outcome metrics with intervals that respect time correlation. 38 mechanisms are clearly load-bearing, 15 are marginal, 8 show no measurable effect, and three show a slight improvement when removed. Four of the eight no-effect mechanisms are deleted outright. The other four are kept, but only with an explicit insurance claim and an expiry, because the replay window contains no extreme event and "no effect on a quiet quarter" is not evidence that something is unneeded.

## The Problem
- Systems accumulate mechanisms because adding one is locally justified and removing one is not. A new mechanism has a story: this failure, that fix. A removal has no story and a risk, so removal never happens, and the cost of everything kept is paid indefinitely in complexity, reading time and interaction surface (Day 93's cycles, Day 94's coupled thresholds).
- The gate makes this asymmetry worse. It is a one-way ratchet: it asks whether a new mechanism is safe to add and has no question about whether the system is better off with what it already has. Every check in it is about preventing harm from additions.
- Complexity has a measurable cost in this system. Each live mechanism is a node in the dependency map, an entry in the inventory that needs evidence, and often a set of constants in the registry. The 64 mechanisms generate roughly 40 of the 118 tunable constants between them and account for most of the cycle risk.
- The right test is counterfactual, and the system is built for one. Replay (Days 52, 89, 94) runs the pipeline over recorded history under a modified configuration. Disabling a mechanism is a modified configuration. The experiment was always cheap to run, and the arc simply never asked for it.

## Architecture

### 1. Every mechanism declares what "off" means
Disabling a mechanism requires knowing what to put in its place. A new field in the inventory records the disabled behavior: a pass-through for a filter, the previous constant for an adaptive threshold, the raw hash for a projection layer. A mechanism without a defined off state cannot be ablated and is flagged, since an unremovable mechanism is its own kind of finding.

```python
@dataclass
class AblationSpec:
    mechanism: str
    disabled_behavior: Callable        # what replaces it when off
    breaks_dependents: bool = False    # True if dependents cannot run without it

def ablate(pipeline, spec: AblationSpec):
    if spec.breaks_dependents:
        raise NotAblatable(spec.mechanism, dependents=pipeline.dependents_of(spec.mechanism))
    return pipeline.with_replacement(spec.mechanism, spec.disabled_behavior)
```

Six mechanisms were marked `breaks_dependents` and were measured at the level of their smallest removable sub-component instead.

### 2. Paired replay with a block bootstrap
For each mechanism, the full 90-day window is replayed twice, once with the production pipeline and once with the ablated one, and the daily outcome metrics are differenced. Days are not independent (a storm spans several of them, and traffic is autocorrelated), so a plain bootstrap would give intervals that are too narrow. A block bootstrap resamples contiguous 5-day blocks.

```python
def ablation_delta(spec, window, n_boot=2000, block=5):
    base = window.run(build_pipeline())
    abl = window.run(ablate(build_pipeline(), spec))
    daily = [composite_loss(abl.day(d)) - composite_loss(base.day(d)) for d in window.days]
    boots = []
    for _ in range(n_boot):
        sample = block_resample(daily, block)
        boots.append(mean(sample))
    lo, hi = percentile(boots, [2.5, 97.5])
    return AblationResult(mean=mean(daily), ci=(lo, hi))
```

Positive delta means the system got worse with the mechanism off. 64 mechanisms times two replays at about 40 seconds each is under two hours of compute, small next to Day 94's screening run.

### 3. A classification rule that uses the interval and an effect-size floor
An interval excluding zero by a hair around a negligible effect is not a finding. The rule needs both statistical and practical significance.

```python
def classify(result: AblationResult, floor=0.02) -> str:
    lo, hi = result.ci
    if lo > floor:
        return "load_bearing"          # removing it clearly hurts
    if hi < -floor:
        return "harmful_direction"     # removing it clearly helps
    if lo > 0 or hi < 0:
        return "marginal"              # real but small
    return "no_measurable_effect"      # interval spans zero
```

The result across all 64:

```
class                    count
load_bearing               38
marginal                   15
no_measurable_effect        8
harmful_direction           3
```

### 4. The 8 with no measurable effect, and what happens to them
Eight is where the judgment starts. A mechanism that did nothing across 90 quiet days has either been superseded, never been triggered, or insures against something that did not happen. Each was examined individually and sorted by which.

```
mechanism                                   day  verdict
code_hash_readset_versioning                 57  superseded by Day 58's AST diff; always redundant     -> DELETE
diagnostic_mode_attribution_fallback         60  fallback path never entered in 90 days; primary works -> DELETE
v1_format_adapter                            53  provider retired v1 format                            -> DELETE
duplicate_midloan_degrade_handler            45  second path shadowed by Day 46's relay probe          -> DELETE
opaque_path_pinning                          61  guards unobservable channels; none occurred           -> KEEP (insurance)
branch_family_trust_pooling                  63  needs identical-surface branches; none formed          -> KEEP (insurance)
effect_ledger_tier2_replay                   49  exercised only on non-idempotent callee drift         -> KEEP (insurance)
transition_regime_boundary_commit            68  fires only when a regime boundary falls mid-batch      -> KEEP (insurance)
```

The deletions removed about 900 lines and 11 constants. The four kept mechanisms are not "kept because they might be useful", which is the argument that has kept dead code alive in every system. They are kept under a stricter rule.

### 5. Insurance claims with an expiry and a way to be tested
A mechanism that survives ablation without measurable effect must carry an explicit claim stating which rare event it protects against, and the claim must be testable. An insurance mechanism without a test is just dead code with a hopeful comment.

```python
@dataclass
class InsuranceClaim:
    mechanism: str
    protects_against: str              # a specific rare scenario
    test: str                          # replay scenario that exercises it
    expires: float                     # review date; default 120 days
    last_exercised: float | None = None

def review_insurance(claims, now):
    for c in claims:
        if c.last_exercised is None and now > c.expires:
            return Action("delete_or_prove", c.mechanism,
                          reason="never exercised and claim expired")
        if c.test is None:
            return Action("reject", c.mechanism, reason="insurance claim without a test")
```

The four kept mechanisms each got a named scenario that exercises them, which Day 96 builds. None of them have been exercised yet, so each carries an expiry 120 days out.

### 6. The three that look better when removed
A mechanism whose removal improves outcomes is the most interesting result, and the easiest to over-read. The three:

```
mechanism                            day  delta (loss, negative=better off)   ci
provisional_resolution_at_timeout     69      -0.031                          (-0.052, -0.011)
corroboration_window_extension        66      -0.024                          (-0.041, -0.009)
branch_trust_rekey                    62      -0.022                          (-0.038, -0.006)
```

None was deleted on this evidence. Day 69's provisional resolution commits boundary data to a regime when the transition timeout fires. On quiet data it sometimes commits a regime the baseline would have settled correctly a few minutes later, which shows as a small net loss. On a fast-onset storm, though, it is the only thing that forces a commit at all. The result gets routed to Day 96's rare-event replay, where the same mechanism is tested on the kind of input it was built for.

### 7. A complexity rule for new mechanisms
The ablation machinery is used going forward as a gate input. A candidate must declare a `disabled_behavior`, and its `DesignIntent` must name either a metric it moves, measured in replay, or an insurance claim with a test. Net additions of mechanisms above the current count are paired with ablation evidence about something that can be removed.

```python
def preflight_justification_check(intent, replay_window) -> CheckResult:
    if intent.insurance_claim:
        return CheckResult(passed=bool(intent.insurance_claim.test), work_done=1, work_expected_min=1)
    delta = ablation_delta(AblationSpec(intent.name, intent.disabled_behavior), replay_window)
    return CheckResult(passed=classify(delta) in ("load_bearing", "marginal"),
                       work_done=len(replay_window.days), work_expected_min=30)
```

## Failure Modes
- **Ablating one mechanism at a time misses redundancy.** Two mechanisms that each cover for the other will both show no effect when removed individually, and both will be marked deletable. Deleting both removes the protection. The four deletions were checked pairwise against the dependency map, but the general case needs a joint ablation, which scales as the number of pairs.
- **The replay window is a quiet quarter.** "No measurable effect" means no effect on traffic that contained no extreme event, which is the condition under which almost every defensive mechanism looks unnecessary. The insurance rule is a patch for this, and it relies on Day 96 supplying the tests that make the claims real.
- **The composite loss weights are a value judgment.** A missed event counts ten times a false alarm. A different weighting moves some mechanisms across the classification boundaries. Four of the 15 marginals change class under a 5x weighting, so the classification is a statement about the loss, not about the mechanism.
- **Deletion removes code but not history.** A removed mechanism leaves behind alerts, dashboards and operator habits that depended on its output labels. Three of the four deletions needed a downstream label cleanup that the ablation never measured.

## What's Next
Four mechanisms are now being kept on the strength of a promise: they insure against rare events. Day 69's provisional resolution is under suspicion for a related reason, since it looks harmful on quiet data and may be essential on fast-onset data. Every one of those questions is about behavior on inputs the 90-day window does not contain. Day 96 builds the rare-event replay suite that has been missing from the series since Day 76: archived extreme conditions, fed through the pipeline at original timing.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
