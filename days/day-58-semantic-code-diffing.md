# Day 58: Semantic Code Diffing — Not Every Redeploy Should Cost a Re-Learn

## TL;DR
Day 57 closed the read-set arc by versioning read-sets against a content hash of the dependent's deployed code — but that hash is byte-level, so a comment change or a reformatted file costs the exact same expensive re-learning cycle as a genuine change to what fields the code reads. Today's fix mirrors what Day 52 did for upstream results: extract only the access-relevant subset of a dependent's code via static analysis, hash *that*, and only invalidate the read-set when the part of the code that actually touches the tracked result has changed. A static prediction is never trusted blindly — it's reconciled against what runtime tracking has actually observed.

## The Problem
- Day 57's `dependent_version()` hashes the dependent's entire deployed code or config. Any diff — whitespace, comments, an unrelated function elsewhere in the same module, a docstring update — produces a new version and retires a perfectly good, hard-won read-set, along with all its accumulated coverage confidence from Day 56.
- For dependents under active development but with stable field-access logic (the access-relevant code is small and rarely touched, even if the surrounding module changes often), this means the scoped-hash optimization from Day 54 almost never gets to run — the system pays Day 56's full re-learning cost on every deploy, regardless of relevance to what's actually being measured.
- This is exactly the raw-vs-semantic gap Day 52 solved for upstream results (hash the meaningful content, not the whole blob) — just never applied to the dependent's own source, where the same asymmetry exists: most of a file's bytes are irrelevant to what fields get read from a tracked result, yet all of them currently count equally toward invalidation.
- Runtime tracking (Day 55) already knows, empirically, which fields get touched — but it only discovers a change *after* it happens, by re-observing behavior from scratch under the new version. There's no way today to predict, before a single run, whether a given code change is even capable of altering the read-set, which means every deploy is treated as maximally risky by default even when most deploys touch nothing relevant.
- The problem compounds with deploy frequency: teams shipping multiple times a day (the exact failure mode Day 57 flagged) are disproportionately penalized, since their dependents rarely stay on one code version long enough to build meaningful coverage confidence before the next unrelated change resets everything.

## Architecture

### 1. Access-relevant code extraction
Statically parse the dependent's code (AST-level) and identify the subset of expressions that touch the `TrackedResult` object from Day 55 — attribute accesses, subscript operations, and any call sites that pass the tracked object onward to a helper function. Everything else in the file — unrelated functions, logging statements, comments — is excluded from what gets hashed.

```python
def extract_access_surface(source_ast, tracked_param_name):
    access_nodes = []
    for node in ast.walk(source_ast):
        if is_access_on(node, tracked_param_name):
            access_nodes.append(normalize(node))
    return access_nodes
```

`normalize()` strips things like variable renames and comment/whitespace differences at the node level, so cosmetically different but structurally identical access expressions hash the same. It also canonicalizes expression order where the surrounding code is provably order-independent (e.g. two field reads with no data dependency between them), so a harmless reordering during refactoring doesn't register as a change.

### 2. Two-tier versioning
The full byte-level code hash (Day 57) is still tracked for general deploy bookkeeping and audit purposes — it's useful context when debugging why a dependent behaved differently after a release — but read-set invalidation now keys off the narrower access-surface hash instead.

```python
def access_surface_version(dependent_id):
    source_ast = parse_dependent_source(dependent_id)
    surface = extract_access_surface(source_ast, tracked_param_name=infer_param(dependent_id))
    return content_hash(surface)

def is_resumable_for(item, dependent_id, ledger):
    version = access_surface_version(dependent_id)
    key = (item.operation_shape, dependent_id, version)
    # ... unchanged from Day 57 from this point
```

### 3. Conservative fallback for non-analyzable code
Dynamic dispatch, reflection-based field access, or decorators that obscure the real access pattern can defeat static extraction outright. When the extractor can't confidently identify the access surface — or identifies it but flags low confidence due to dynamic constructs it can't fully resolve — fall back to Day 57's full code hash. Same conservative-default pattern that's carried this entire stretch since Day 52: no evidence of safety, no shortcut taken.

```python
def access_surface_version(dependent_id):
    source_ast = parse_dependent_source(dependent_id)
    surface = try_extract_access_surface(source_ast)
    if surface is None:  # extraction failed or low-confidence
        return dependent_version(dependent_id)  # Day 57's full hash
    return content_hash(surface)
```

### 4. Reconciliation against runtime observation
A statically-derived access surface is a prediction, not a proof — it's cross-checked against the runtime-observed read-set from Day 55/56. If static analysis claims a field is touched but runtime tracking has never observed it, or the reverse (runtime observes a field the static surface doesn't account for) after enough confident runs, the mismatch is flagged and the dependent's version falls back to full-code hashing until the discrepancy is understood and, ideally, the extractor is corrected.

```python
def reconcile(dependent_id, static_surface_fields, observed_read_set, confident):
    if confident and not static_surface_fields.issuperset(observed_read_set):
        flag_analyzer_mismatch(dependent_id)
        return False  # don't trust the surface-scoped version yet
    return True
```

### 5. Lazy, cached extraction
Running full AST extraction on every `is_resumable_for` call would be wasteful — the access surface only needs recomputing when the underlying code actually changes. Extraction results are cached against Day 57's full byte-level code hash, so a redeploy triggers exactly one extraction pass per new version, not one per resume check.

```python
def access_surface_version(dependent_id):
    full_hash = dependent_version(dependent_id)
    if full_hash in EXTRACTION_CACHE:
        return EXTRACTION_CACHE[full_hash]
    surface = try_extract_access_surface(parse_dependent_source(dependent_id))
    version = content_hash(surface) if surface is not None else full_hash
    EXTRACTION_CACHE[full_hash] = version
    return version
```

## Failure Modes
- **Static analysis imprecision.** Dynamic languages make exact extraction hard — string-keyed field access computed at runtime (`result[some_variable]`) can't always be resolved statically, producing false negatives in the extracted surface that reconciliation (point 4) is meant to catch, but only after enough runs to notice, which reopens a version of Day 56's coverage-lag problem one layer over.
- **Over-invalidation within the access surface.** Even normalized AST hashing can still treat a semantically irrelevant reordering of independent access expressions as a change if normalization isn't thorough enough — under-solving this just reproduces Day 57's original problem at a smaller scale, with the added complexity of a static analyzer that now needs its own correctness guarantees.
- **New pipeline surface area.** This requires an extraction pass integrated into the deploy pipeline (or run lazily at first-use per point 5), which is new infrastructure with its own failure modes — a broken or outdated extractor is itself unmonitored unless reconciliation catches it, and reconciliation is a lagging indicator, not a preventive one.
- **Reconciliation lag.** Cross-checking against runtime observation is inherently after-the-fact — a newly deployed access-surface hash can pass several runs before a mismatch is detected, during which the system may already be trusting an incorrect surface-scoped version and skipping cascades it shouldn't.
- **Cache invalidation edge case.** The extraction cache (point 5) is keyed on the full code hash, so a redeploy that happens to produce the exact same full hash as a previous version (unlikely but not impossible with certain build/packaging setups) would incorrectly reuse a stale cached extraction — a low-probability but real edge case given how central the full hash is to this design.

## What's Next
Open question for Day 59: the extraction pass and the reconciliation check are both new pieces of logic that can themselves be wrong — nothing yet validates the *analyzer's* correctness independent of the dependents it's analyzing. The system now has confidence machinery for read-sets (Day 56) and versioning machinery for code (Day 57–58), but no equivalent confidence signal for whether the static analyzer itself is trustworthy for a given dependent's coding style, language features in use, or framework conventions.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
