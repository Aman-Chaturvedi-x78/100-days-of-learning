# Day 61: Opaque Path Pinning — Some Channels Can't Be Watched

## TL;DR
Day 60's boundary-level tracking narrows Day 55's proxy-escape gap but can't close it — a tracked result handed to native or compiled code, or written straight to disk through a path with no hook, escapes every witness the system has. Today's fix is a policy change rather than a technical one: stop trying to build trust for the untrackable. Dependents (or specific code paths within them) that touch a tracked result through a known-opaque channel are pinned permanently to full canonical hashing, with no attempt at read-set scoping or trust-building, because there is no reliable independent witness that could ever validate them.

## The Problem
- Day 60's diagnostic mode assumes a mismatch can eventually be attributed to either the analyzer or the runtime tracker, given a good enough second witness. But some escape channels — FFI/native calls, direct file I/O, piping the object to a subprocess — have no available hook at all, in-process or at the serialization boundary.
- For these, a reconciliation mismatch is permanently unresolvable: there is no diagnostic window, no matter how long, that would ever produce corroborating evidence. Treating this the same as an ordinary inconclusive case (Day 60, point 4 — leave trust unchanged, flag for review) means the dependent sits in permanent limbo, neither trusted nor explicitly distrusted, silently retrying an unwinnable validation forever.
- Continuing to run diagnostic mode against a channel that can never be observed is pure wasted overhead — worse, it risks a false attribution if the opaque write happens to coincide with unrelated boundary-level activity that gets misread as corroboration.

## Architecture

### 1. Opaque channel detection
Extend Day 58's AST extractor to flag calls into known-unobservable APIs — `ctypes`, FFI boundaries, `open()`/file writes, `subprocess` — that receive the tracked result or a value derived from it, rather than waiting for a diagnostic to fail before recognizing the risk.

```python
OPAQUE_CALL_PATTERNS = {"ctypes.*", "open", "subprocess.*", "os.write"}

def flag_opaque_paths(source_ast, tracked_param_name):
    opaque_nodes = []
    for node in ast.walk(source_ast):
        if is_call_on(node, OPAQUE_CALL_PATTERNS) and derives_from(node, tracked_param_name):
            opaque_nodes.append(node)
    return opaque_nodes
```

### 2. Permanent full-hash pinning
A dependent (or a specific path fingerprint within one, per Day 56) flagged as opaque skips the entire read-set and trust pipeline from Days 54–60 — no scoping attempted, no diagnostic mode queued, ever.

```python
def is_resumable_for(item, dependent_id, ledger):
    if is_opaque(dependent_id, item.path_fingerprint):
        return content_hash(canonical) == item.consumed_hash  # always full hash, no exceptions
    # ... otherwise Day 60's diagnostic-gated trust pipeline applies
```

### 3. Partial opacity
Opacity is scoped to the specific branch that touches the opaque channel, not the whole dependent — a dependent with one debug-only branch that writes to disk and several other clean branches only loses optimization on the flagged branch; the rest keep earning trust independently.

```python
def is_opaque(dependent_id, path_fingerprint):
    return path_fingerprint in OPAQUE_BRANCHES.get(dependent_id, set())
```

## Failure Modes
- **Over-flagging.** Broad heuristics can mark a branch opaque even when the disk write or subprocess call is inconsequential to what fields actually matter downstream — giving up optimization unnecessarily for paths that were actually safe to trust.
- **Under-flagging.** An indirect escape through a third-party library that internally performs file I/O or native calls the extractor doesn't recognize still evades detection — opacity detection inherits the same conservative-but-incomplete nature as every extractor decision since Day 58.
- **Declaration staleness.** Where opacity is manually declared rather than auto-detected, it can go stale the same way Day 54's original hand-declared read-sets did — a channel that used to be opaque but was refactored away keeps paying the full-hash cost with nobody noticing the pin is no longer necessary.
- **Bookkeeping overhead.** Tracking opacity per branch rather than per dependent adds another dimension to state already split across path fingerprints (Day 56), code versions (Day 57), and access surfaces (Day 58) — more state to keep consistent as dependents evolve.

## What's Next
Day 62: partial opacity raises a sharper question about Day 59's trust scoring — if one branch of a dependent is permanently opaque, should that poison trust for the dependent's other, fully analyzable branches? Trust today is scored per dependent, not per branch, which means an unrelated opaque path could be dragging down confidence for code that has nothing to do with it.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
