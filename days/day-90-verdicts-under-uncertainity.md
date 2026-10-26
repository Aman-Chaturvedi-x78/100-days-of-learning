# Day 90: Pass, Block, or Probation — Gate Verdicts When the Check Itself Is Uncertain

## TL;DR
Day 89 calibrated the validity harness against real rollouts and came back with an uncomfortable result: harness trust of 0.79 with a 95% interval of [0.52, 0.92], and a feature-level fidelity score of 0.64. Every check built since Day 79 returns a boolean, and a boolean is the wrong shape for that answer. If the gate says PASS, it's claiming more confidence than the evidence supports. If it says BLOCK, it's rejecting mechanisms on the strength of an instrument whose reliability might be 52%. Today replaces the two-state verdict with three states, PASS, WARN and BLOCK, driven by intervals instead of point estimates. A WARN is not a soft pass: it ships the mechanism into a probation regime with tighter canary limits and a hard expiry, and the evidence the probation generates feeds straight back into the corpus that Day 89 built. The design reuses the suspect state from Day 46 and the TTL leases from Day 37, because "not enough evidence yet, but we can't wait forever" is a shape this system has already solved twice.

## The Problem
- A boolean check result throws away the one thing Day 89 just worked to produce. `preflight_statistical_validity_check` computes realized alpha and beta, compares them to claimed ceilings, and returns `passed=True/False`. The realized rates come from a harness with a sampling error of its own (4,000 scenarios) and a fidelity error (0.64, with a trust interval spanning 0.52 to 0.92). Collapsing all of that to one bit means a mechanism measured at alpha 0.058 against a ceiling of 0.0625 and one measured at 0.031 get the same PASS, even though only one of them has real margin.
- Treating uncertainty as a threshold decision produces a predictable pathology. Pick a cut-off and mechanisms cluster just on either side of it, with the side determined by noise. A mechanism that scores 0.0620 against 0.0625 ships, and one that scores 0.0630 is blocked and sent back for rework it may not need. The gate's output is then more a function of sampling luck than of mechanism quality, which is the worst property a gate can have because it also teaches authors to rerun it until it passes.
- The cost of the two errors is asymmetric and not the same for every mechanism. Blocking a good mechanism costs a delay and an investigation. Passing a bad one costs a canary rollback at best (Day 84 exists for exactly this) and a production incident at worst. A mechanism that touches the NOAA alert-tier bypass path from Day 74 has a much higher cost of a missed defect than one that adjusts a logging cadence, but the gate currently applies the same cut-off to both.
- There's an operational problem on the other side. If uncertain results simply block, then mechanisms touching anything statistical can never ship until the corpus is large enough to tighten the intervals, and the corpus only grows when mechanisms ship. That's a deadlock built out of evidence requirements: the system needs real rollouts to trust the harness and needs the harness to permit real rollouts. Day 89's corpus has 14 entries because 14 mechanisms shipped, and a gate that reflexively blocks under uncertainty would stop that number from ever increasing.

## Architecture

### 1. Checks return intervals, not booleans
Every check result now carries an estimate and a confidence interval, and deterministic checks (concurrency, deadlock, latency bounds from the registry) simply return a degenerate interval where lower equals upper. This keeps the gate uniform without pretending that a lock-order violation has statistical uncertainty.

```python
@dataclass
class CheckEstimate:
    metric: str
    point: float
    lower: float
    upper: float
    ceiling: float              # the value above which the mechanism is unacceptable
    deterministic: bool = False

    def classify(self) -> str:
        if self.upper <= self.ceiling:
            return "PASS"       # even the pessimistic bound is acceptable
        if self.lower > self.ceiling:
            return "BLOCK"      # even the optimistic bound is unacceptable
        return "WARN"           # the interval straddles the ceiling
```

The classification rule is the entire idea in three lines. A mechanism passes only when its worst plausible value is acceptable, blocks only when its best plausible value is unacceptable, and everything between is an honest "we don't know yet."

### 2. Combining sampling error with harness uncertainty
The interval around realized alpha needs to account for two sources: the sampling noise from running a finite number of scenarios, and the harness's own unreliability as measured in Day 89. Treating them as independent and adding in quadrature is a simplification, but it is a conservative one in the direction that matters.

```python
def validity_estimate(rates: dict, n_scenarios: int, harness_trust: tuple[float, float, float],
                      fidelity: float, ceiling: float) -> CheckEstimate:
    p = rates["realized_alpha"]
    # sampling error of the proportion
    se = math.sqrt(p * (1 - p) / n_scenarios)
    # harness error: the less the harness resembles reality, the wider the band.
    # (1 - fidelity) scales how much of the point estimate we are willing to trust.
    _, trust_lo, _ = harness_trust
    harness_slack = p * (1 - trust_lo) * (1 - fidelity)
    half = 1.96 * se + harness_slack
    return CheckEstimate("realized_alpha", p, max(p - half, 0.0), p + half, ceiling)
```

At the Day 88 budgeted design's measured alpha of 0.052 against a ceiling of 0.0625:

```
sampling half-width:   1.96 * sqrt(0.052 * 0.948 / 4000)      = 0.0069
harness slack:         0.052 * (1 - 0.52) * (1 - 0.64)        = 0.0090
interval:              [0.036, 0.068]    ceiling 0.0625   ->  WARN
```

Under Day 88's boolean rule this mechanism passed cleanly. Under today's rule it's a WARN, because its pessimistic bound (0.068) is above the ceiling. That's the correct reading: the point estimate looked fine, and the evidence that the point estimate is trustworthy is not strong enough to rest a shipping decision on.

### 3. A cost-weighted ceiling and a criticality tier
The ceiling itself shouldn't be one number for the whole system. Each `DesignIntent` declares a criticality tier, and the tier scales both the ceiling and the strictness of the classification. This reuses the severity-estimation idea from Day 74, where the NOAA alert-tier path earned a bypass because its latency cost was categorically higher.

```python
CRITICALITY_POLICY = {
    # tier: (ceiling multiplier, WARN allowed?, max probation traffic)
    "alert_path":   (0.6, False, None),      # tight ceiling, uncertainty -> BLOCK
    "core":         (1.0, True,  10.0),
    "peripheral":   (1.5, True,  50.0),
}

def gate_verdict(estimates: list[CheckEstimate], tier: str) -> str:
    mult, warn_ok, _ = CRITICALITY_POLICY[tier]
    classes = []
    for e in estimates:
        scaled = dataclasses.replace(e, ceiling=e.ceiling * mult)
        classes.append(scaled.classify())
    if "BLOCK" in classes:
        return "BLOCK"
    if "WARN" in classes:
        return "WARN" if warn_ok else "BLOCK"     # alert-path mechanisms never get probation
    return "PASS"
```

An alert-path mechanism with an uncertain result is blocked, full stop: the cost of being wrong there is high enough that unresolved uncertainty counts as a failure. A peripheral mechanism with the same uncertain result gets probation. Same evidence, different action, because the downside differs.

### 4. WARN means probation, implemented as a lease
A mechanism that receives WARN is allowed to ship, but into a constrained regime: a capped traffic ceiling from the tier policy, a stricter canary (Day 88's budget with a smaller `max_epochs`), and a probation lease with a TTL. The lease is Day 37's mechanism applied to a governance state rather than a process: it expires unless renewed by evidence.

```python
@dataclass
class ProbationLease:
    mechanism: str
    granted_at: float
    ttl_days: int = 21
    max_traffic_percent: float = 10.0
    evidence_required: int = 3             # labeled rollouts or stage-completions needed to resolve
    evidence_collected: int = 0

def probation_tick(lease: ProbationLease, now: float, new_labeled_outcomes: list[RolloutRecord]) -> str:
    lease.evidence_collected += sum(1 for r in new_labeled_outcomes if effective_truth(r) is not None)
    if any(effective_truth(r) is True for r in new_labeled_outcomes):
        return "REVOKE"                                 # real bad outcome: pull it now
    if lease.evidence_collected >= lease.evidence_required:
        return "RESOLVE"                                # rerun the gate with the enlarged corpus
    if now - lease.granted_at > lease.ttl_days * 86400:
        return "EXPIRE_TO_BLOCK"                        # no evidence arrived: treat unknown as unsafe
    return "CONTINUE"
```

The expiry default is the safety property. A probation that can run forever is a pass with extra steps. Expiring to BLOCK means a mechanism that never generates enough evidence to resolve eventually loses its place, which is the same stance the lease machinery took in Day 37: silence is not consent.

### 5. Probation evidence flows back into the corpus
Each outcome produced by a probationary mechanism is a labeled rollout (via Day 89's `RolloutRecord`), so the corpus grows precisely where uncertainty is highest. On `RESOLVE`, the gate reruns with the enlarged corpus, the harness trust interval narrows, and the verdict usually moves to PASS or BLOCK.

```python
def resolve_probation(lease: ProbationLease, intent: DesignIntent, candidate_factory):
    corpus = rollout_store.labeled_records()
    trust = recompute_harness_trust(corpus)               # Day 89 step 5, with the new rows
    fidelity = harness_fidelity(real_streams(corpus), synthetic_streams())["overall"]
    scenarios = reweight_scenarios(intent.statistical_claim.scenario_generator(4000),
                                   real_feature_dist(corpus), n_real=len(corpus))
    rates = measure_error_rates_weighted(candidate_factory, scenarios)
    est = validity_estimate(rates, 4000, trust, fidelity, intent.statistical_claim.claimed_alpha * 1.25)
    return est.classify()    # PASS, BLOCK, or WARN again (bounded: one renewal, then block)
```

A second WARN after a full probation cycle converts to BLOCK. Otherwise a mechanism that sits on the boundary could renew indefinitely, and the lease would stop meaning anything.

### 6. A cap on concurrent probation
Probation is a limited resource, and Day 83's rule about declaring bounds applies to it. The number of mechanisms simultaneously on probation is capped, because each one lowers the amount of clean traffic available for evidence and each one is a standing exception to the gate's normal guarantee.

```python
MAX_CONCURRENT_PROBATION = 3

def admit_to_probation(mechanism: str, active: list[ProbationLease]) -> bool:
    if len(active) >= MAX_CONCURRENT_PROBATION:
        incident_log.record(event="probation_full", mechanism=mechanism, active=len(active))
        return False       # caller treats this as BLOCK until a slot frees
    return True
```

## Failure Modes
- **The interval-combination formula in step 2 is a heuristic, not a derived result.** Scaling harness slack by `(1 - trust_lo) * (1 - fidelity)` produces an interval that widens when the harness is untrustworthy, which is the right qualitative behavior, but the exact functional form was chosen because it's monotone and conservative, not because anyone proved it has the stated coverage. A properly calibrated version would be validated by the same ground-truth loop it depends on, which is circular until the corpus is much bigger.
- **WARN invites a dumping-ground dynamic.** If probation is easier to get than a clean pass and easier to survive than a block, authors have an incentive to land in it. The concurrency cap and the expiry limit the exposure, but a queue of mechanisms waiting for a probation slot is just a different kind of block, and the cap value of 3 was set by judgment, not by measurement.
- **Probationary mechanisms accumulate evidence slowly by construction.** A mechanism capped at 10% traffic generates a tenth of the observations per stage, so the evidence that would resolve its probation arrives at a tenth of the rate. The 21-day TTL and 3-outcome requirement interact: a low-traffic mechanism may be unable to produce three labeled outcomes in 21 days at all, and would expire into BLOCK through no fault of its own. That is a self-slowing loop, and the right TTL probably depends on the mechanism's traffic share.
- **The criticality tier is declared by the author, with the same incentive problem Day 88 identified for statistical claims.** An author who labels a mechanism "peripheral" gets a looser ceiling and eligibility for probation. Nothing today audits tier assignment against what the mechanism actually reads and writes, though the declared `reads` and `writes` sets from Day 79 would make such an audit mechanical.

## What's Next
The gate now has a graded verdict and a way to resolve uncertainty by shipping carefully. Its coverage report still shows one named dimension with no check: `STALE_READ`, the first gap Day 82 identified, and the one that has stayed on the board for nine days while newer problems took priority. Day 91 builds that check. Applying it retroactively to mechanisms already in the pipeline is also the arc's most reliable source of real findings, because every previous retroactive audit (Days 78, 80, 83) has turned something up.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
