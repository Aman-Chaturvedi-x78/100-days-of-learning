# Day 82: Stop Finding Gaps, Start Modeling Them — A Coverage Map for the Preflight Gate

## TL;DR
Days 79 through 81 built a preflight gate, ran it against a real mechanism, and then deliberately broke it with an adversarial construction — and each time, the way we found a gap was the same: build something, throw it at the gate, see what gets through. That's a fine way to *discover* a gap. It's a terrible way to know whether you've found *all* of them, because the only evidence of completeness that method can ever produce is "we haven't found one yet," which is indistinguishable from "there isn't one" right up until the moment it isn't. Today's fix replaces that with a coverage model: an explicit taxonomy of failure-mode dimensions, a declared mapping from each preflight check to the dimensions it actually covers, and a combinatorial gap report that tells you, structurally, which dimensions have zero checks pointed at them — before an adversarial candidate ever has to find that out for you.

## The Problem
- Every check added to the gate so far — `preflight_concurrency_check` (Day 79), `preflight_latency_check` (Day 79), `preflight_adversarial_check` (Day 79), `preflight_determinism_check` (Day 81) — was added *reactively*, in response to a specific failure that a specific candidate exposed. The gate's check list is a fossil record of past surprises, not a designed-in guarantee.
- This has a subtle but serious property: the gate's apparent thoroughness is a function of how imaginative the last red-teamer was, not of how complete the underlying coverage actually is. Day 81's adversarial candidate was clever enough to expose the logical-race blind spot. A less clever candidate with a *different* kind of blind spot — say, a resource-exhaustion pattern, or a dimension nobody has thought to probe yet — would sail through just as cleanly as `combined_confidence_score` did, and nothing about the gate's current design would tell you that in advance.
- "We red-team it and it hasn't failed recently" is exactly the failure mode Day 78 called out in the test harness itself: a system that only reports confidence proportional to how hard you've tried to break it, not proportional to how hard it actually is to break.
- There's no way, today, to ask the gate a direct question like "do you have *any* check that would catch a failure in category X?" The checks exist as a flat list of functions. Nothing records what each one is actually *for*, in a vocabulary that could be compared against a list of things that could go wrong.
- Without that vocabulary, "the gate has four checks" and "the gate covers four failure modes" are being silently treated as the same claim. They are not. A fifth check that re-tests concurrency safety in a slightly different way would make the first claim true and the second claim false.

## Architecture

### 1. A failure-mode taxonomy, built from the arc's own history
The first move isn't code — it's naming things that already happened. Every failure mode this series has actually hit, from Day 74 onward, gets a stable identifier and a one-line definition. This isn't a hypothetical catalogue; it's a direct transcription of lived failures, which matters because a taxonomy invented from first principles tends to be shaped by what's easy to categorize rather than what actually goes wrong.

```python
FAILURE_MODE_TAXONOMY = {
    "DATA_RACE": "Concurrent unsynchronized access to shared mutable state "
                 "produces a corrupted or inconsistent value (Day 76, Day 80).",
    "DEADLOCK": "Concurrent lock acquisition in inconsistent order causes "
                "mutual indefinite blocking (Day 77).",
    "LOGICAL_RACE": "Concurrently-valid, individually-correct operations "
                     "combine into a result that depends on scheduling order, "
                     "with no corrupted data involved (Day 81).",
    "LATENCY_BUDGET_VIOLATION": "A mechanism's worst-case or best-case path "
                                  "exceeds the declared downstream deadline (Day 74).",
    "ADVERSARIAL_INPUT_SENSITIVITY": "Combinatorial or boundary inputs produce "
                                       "behavior outside declared bounds (Day 77).",
    "RESOURCE_EXHAUSTION": "Unbounded retry, queue growth, or allocation under "
                            "sustained load degrades the system independent of "
                            "correctness of any single operation.",
    "STALE_READ": "A read observes a value that was valid at read time but is "
                   "invalid by the time it's acted on (Day 64).",
    "UPSTREAM_CORRELATION_BLINDNESS": "Multiple inputs treated as independent "
                                        "evidence actually share a root cause (Day 71).",
}
```

Two of these — `RESOURCE_EXHAUSTION` and, to a lesser extent, `UPSTREAM_CORRELATION_BLINDNESS` — are included even though nothing in the pipeline has hit them *yet*. They're in the taxonomy because the arc's own history (Day 71's correlated sources, Day 42's migration admission control under load) establishes them as plausible failure shapes for this system, which is exactly the point: the taxonomy's job is to hold known-plausible dimensions whether or not a check currently exists for them, so the gap report in step 3 has something to measure against.

### 2. A declared coverage mapping, per check
Each preflight check now declares, explicitly, which taxonomy entries it actually tests for — not which ones it was *written in response to*, but which ones it would *actually catch* if present. This distinction matters: `preflight_concurrency_check` was written after Day 80's data race, but on inspection it also structurally covers deadlock detection, because both are instances of "the declared lock-acquisition behavior doesn't match the declared `requires`/`writes` set."

``python
CHECK_COVERAGE = {
    "preflight_concurrency_check": {"DATA_RACE", "DEADLOCK"},
    "preflight_latency_check": {"LATENCY_BUDGET_VIOLATION"},
    "preflight_adversarial_check": {"ADVERSARIAL_INPUT_SENSITIVITY"},
    "preflight_determinism_check": {"LOGICAL_RACE"},
}

def coverage_report(taxonomy: dict, check_coverage: dict) -> dict:
    covered = set()
    for dims in check_coverage.values():
        covered |= dims
    uncovered = set(taxonomy) - covered
    return {
        "covered": sorted(covered),
        "uncovered": sorted(uncovered),
        "coverage_ratio": len(covered) / len(taxonomy),
    }
``

Running this today, before writing a single new check, is the whole point of the exercise — it turns "are there blind spots" from a question you answer by getting surprised into a question you answer by reading a diff of two sets.

```
>>> coverage_report(FAILURE_MODE_TAXONOMY, CHECK_COVERAGE)
{
  "covered": ["ADVERSARIAL_INPUT_SENSITIVITY", "DATA_RACE", "DEADLOCK",
              "LATENCY_BUDGET_VIOLATION", "LOGICAL_RACE"],
  "uncovered": ["RESOURCE_EXHAUSTION", "STALE_READ",
                "UPSTREAM_CORRELATION_BLINDNESS"],
  "coverage_ratio": 0.625
}
```

That output is worth sitting with. `STALE_READ` shows up as uncovered even though Day 81's *fix* for `combined_confidence_score` used a versioned-read-and-retry pattern explicitly built to prevent stale reads — but the preflight gate itself never checks a new candidate's read pattern for staleness risk; it only checks whether concurrent writes race, and whether outputs are deterministic under interleaving. A candidate that reads a value, sleeps, and acts on it later without versioning would pass all four existing checks and still be exposed to Day 64's exact original problem.

### 3. Mandatory declaration: every new check must update the map
This is a process rule as much as a code rule, but it's the one that keeps the model honest over time. Any new check added to the gate must, at the point it's added, also update `CHECK_COVERAGE` with the taxonomy dimensions it actually covers — and a lightweight CI-style assertion rejects a check registration that doesn't declare coverage, so the map can't silently drift out of sync with reality the way the check list itself did for three days running.

```python
def register_check(name, fn, covers: set):
    if not covers:
        raise ValueError(
            f"check '{name}' registered with empty coverage declaration — "
            f"every check must declare which taxonomy dimensions it catches"
        )
    unknown = covers - set(FAILURE_MODE_TAXONOMY)
    if unknown:
        raise ValueError(
            f"check '{name}' declares coverage for undefined dimensions: {unknown}"
        )
    PREFLIGHT_CHECKS[name] = fn
    CHECK_COVERAGE[name] = covers
```

### 4. The taxonomy itself is versioned and reviewed, not static
A coverage report is only as honest as the taxonomy it's measured against, and a taxonomy that never grows is just as misleading as a check list that never grows — it quietly implies "we've enumerated everything that can go wrong," which is never true. So the taxonomy carries a version and a changelog, and every retrospective (Day 76's burst-test gap, Day 77's deadlock, Day 80's data race, Day 81's logical race) gets checked against it after the fact: did this failure map onto an existing dimension, or did it require a new one? Three of the four mapped onto dimensions that, in hindsight, could have been named *before* the failure occurred, just from knowing the system's shape. Only Day 81's logical race genuinely required the failure to happen first before anyone could have named it — which is itself useful information about which kinds of gaps this method can and can't get ahead of.

```python
TAXONOMY_CHANGELOG = [
    {"version": 1, "added": list(FAILURE_MODE_TAXONOMY), "date": "2026-10-18"},
]

def propose_taxonomy_addition(dimension: str, definition: str, evidence: str):
    """A new dimension requires a definition AND a cited reason it's plausible
    for this system — never added speculatively, mirroring Day 52's rule that
    projection fields are only promoted on replay evidence, not intuition."""
    pending_taxonomy_review.append({
        "dimension": dimension, "definition": definition, "evidence": evidence,
    })
```

## Failure Modes
- **The taxonomy can be wrong in the direction that matters most: missing entries you haven't thought of.** `coverage_ratio` measures coverage against a taxonomy that is itself an artifact of the arc's history — it cannot, by construction, catch a failure mode nobody on this project has encountered or imagined yet. A 100% coverage ratio is a statement about *known* dimensions, never a guarantee of safety. This is a genuine ceiling on the method, not a bug to be fixed, and it's the honest answer to Day 81's own closing question: coverage completeness can be reasoned about *directly* relative to a declared taxonomy, but the taxonomy itself still has to be reasoned about *indirectly*, through retrospectives and judgment.
- **Over-broad coverage declarations inflate the ratio without adding real protection.** Nothing stops a check from declaring it covers five dimensions while only meaningfully testing for one, especially under schedule pressure to make the coverage report look better. This needs a countermeasure: periodic coverage *audits* where each declared (check, dimension) pair is validated against an actual constructed failure in that dimension — if the check doesn't catch a deliberately-injected failure of the declared kind, the declaration is wrong and gets corrected, not the taxonomy.
- **Coverage is binary where reality is graded.** `preflight_adversarial_check` "covers" `ADVERSARIAL_INPUT_SENSITIVITY`, but Day 77 already showed combinatorial scenario generation can miss specific input combinations depending on how dimensions are declared — so "covered" here means "has a check aimed at this dimension," not "catches every instance of this failure mode." The coverage map answers "do we have a defense in this direction," not "is the defense airtight," and conflating the two would be exactly the overconfidence this whole exercise is trying to avoid.
- **New taxonomy dimensions can arrive faster than checks can be written for them**, leaving a known, named, tracked gap sitting in the `uncovered` list for real stretches of time. That's not a failure of the model — a visible, named gap is strictly better than an invisible one — but it means the coverage report needs a place to live where it's actually looked at regularly, not generated once and filed away.

## What's Next
`STALE_READ` and `RESOURCE_EXHAUSTION` sit uncovered in today's report, and `RESOURCE_EXHAUSTION` in particular has never been tested for anywhere in this pipeline — not in the gate, not in the original mechanisms, not even conceptually, despite federation (Day 42) and bursty loads (Day 76) being exactly the conditions that produce it. Day 83 builds a check for it, and in the process of building it, finds out the hard way that "declare a dimension in the taxonomy" and "write a check that actually catches it" are two very different amounts of work.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
