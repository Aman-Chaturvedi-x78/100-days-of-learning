# Day 59: Analyzer Trust Scoring — Earning the Right to Trust a Prediction

## TL;DR
Day 58's static extractor predicts a dependent's access surface before any code runs, but nothing yet establishes whether that prediction can actually be trusted for a given dependent's coding style. Today's fix: run the extractor in shadow mode — always compute the access surface, but don't act on it for resume decisions until a per-dependent trust score, built from historical agreement with Day 58's reconciliation checks, crosses a threshold. A single confident disagreement sharply cuts trust rather than decaying it slowly, since a wrong static prediction risks the same silent missed-cascade failure this series has flagged as worse than an unnecessary one since Day 52.

## The Problem
- Day 58's extractor produces a prediction, and reconciliation (Day 58, point 4) checks that prediction against runtime observation — but only *after* the surface-scoped version is already being used for resume decisions. There's no gate that requires the analyzer to prove itself before its output is trusted.
- Different dependents use different language features — some are flat attribute access only, others involve dynamic dispatch or reflection that the extractor may or may not resolve correctly. The extractor's reliability isn't uniform across the dependent population, but today it's treated as uniformly trustworthy the moment extraction succeeds at all.
- Reconciliation catches a bad prediction eventually, but "eventually" means some number of resume decisions have already been made on a surface hash that might be wrong — exactly the reconciliation-lag failure mode flagged on Day 58.

## Architecture

### 1. Shadow-mode extraction
For every dependent, the access surface is always computed when extractable, but a new dependent — or one whose extracted pattern hasn't been validated yet — doesn't have its resume decisions keyed on that surface until trust is earned. Reconciliation runs against the shadow prediction regardless of whether it's actively used.

```python
def resolve_version(dependent_id):
    surface_version, confident_enough = trust_gated_surface(dependent_id)
    return surface_version if confident_enough else dependent_version(dependent_id)
```

### 2. Per-dependent trust score
Trust is a running rate of reconciliation agreements over total reconciliation checks, not a one-time pass/fail. Only once enough consistent agreement has accumulated does the surface-scoped version actually get used.

```python
def update_trust(dependent_id, agreed: bool):
    score = TRUST_SCORES.setdefault(dependent_id, {"agree": 0, "total": 0})
    score["total"] += 1
    if agreed:
        score["agree"] += 1
    else:
        score["agree"] = 0  # sharp reset on disagreement, not gradual decay
```

### 3. Asymmetric response to disagreement
A single confident disagreement resets accumulated trust rather than gradually eroding it — this deliberately mirrors the series' recurring principle since Day 52 that under-triggering (a silent missed cascade) is a worse failure than over-triggering (an unnecessary but safe fallback).

```python
def trust_gated_surface(dependent_id, threshold=0.98, min_checks=30):
    score = TRUST_SCORES.get(dependent_id, {"agree": 0, "total": 0})
    if score["total"] < min_checks:
        return None, False
    return access_surface_version(dependent_id), (score["agree"] / score["total"]) >= threshold
```

## Failure Modes
- **Cold start per dependent.** Every new dependent starts from zero trust and pays the full-hash cost until it accumulates enough reconciliation checks, even if its code uses the exact same simple patterns as an already-trusted dependent elsewhere in the graph — trust doesn't generalize across dependents yet.
- **Arbitrary threshold, again.** The 98%-agreement bar is a heuristic tradeoff between false confidence and permanently-conservative dependents, not a principled derivation — the same category of judgment call flagged for Day 56's coverage threshold.
- **Reconciliation noise treated as analyzer error.** A sharp trust reset on any disagreement assumes every reconciliation mismatch reflects a genuine analyzer bug — but Day 55 already flagged a case (proxy escape) where runtime tracking itself can under-observe a field, producing a mismatch that looks like an analyzer error but isn't. Today's design can't yet tell the two apart.
- **Indefinite shadow overhead.** A dependent that never crosses the trust threshold keeps paying full reconciliation cost forever, with no benefit — safe, but a permanent tax with no escape valve.

## What's Next
Day 60: Day 55's proxy-escape failure mode resurfaces here directly — a reconciliation mismatch can come from a genuine analyzer error, or from runtime tracking itself being an unreliable witness. Before trusting a sharp trust reset, the system needs a way to tell "the analyzer was wrong" apart from "the runtime observation was wrong," instead of treating every mismatch as proof against the analyzer.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
