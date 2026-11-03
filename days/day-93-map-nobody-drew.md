# Day 93: The Map Nobody Drew — Mechanism Inventory, Blast Radius, and 41 Grandfathered Mechanisms

## TL;DR
Ninety-two days of building have produced a pipeline with 64 live mechanisms, and every piece of attention paid to them since Day 79 has been to one mechanism at a time: a candidate is proposed, the gate evaluates it, it ships. Nobody has looked at the pipeline as a graph. Today builds the inventory the arc never had: every live mechanism as a node, every read, write and declared dependency as an edge, built from `DesignIntent` manifests where they exist and from runtime tracing (Day 55) where they don't. The map surfaces three things the per-mechanism view could not. One mechanism, Day 67's regime-segmented baseline, has a transitive dependent count of 23, more than any other, and has not been validated in 55 days. Two dependency cycles exist that no one designed. And 41 of the 64 mechanisms shipped before the gate did, so they have never been evaluated by any of its nine checks. Running the gate retroactively over them is the arc's most reliable habit, and it produced the usual result: something real.

## The Problem
- The preflight gate is a per-candidate instrument. It answers whether this new mechanism, as declared, is safe to add. It has no view of the existing system's shape, so it cannot say that the candidate adds a seventh consumer to a state key that six mechanisms already read, or that it closes a dependency cycle.
- That blind spot matters more as the system ages. Almost every day since Day 54 reused an earlier mechanism: Day 86 reuses Day 71's clustering and Day 67's baselines, Day 90 reuses Day 37's leases and Day 46's suspect state, Day 91 reuses Day 64's epochs. Reuse is how the arc stayed coherent, and it is also how single points of dependence form. A mechanism reused by many others becomes load-bearing without anyone deciding that it should be.
- The gate also has a historical hole. It was introduced on Day 79, and everything built between Day 30 and Day 78 predates it. Those mechanisms were reviewed at the time in the way the arc reviewed things then, with a design post and informal reasoning. Every retroactive audit since has found a real defect: Day 78 in the harness, Day 80's race, Day 83's retry queue, Day 91's stale read in a mechanism that had passed. The prior is that 41 unreviewed mechanisms contain more.
- Risk for a mechanism is a product of two things nobody has recorded together: how much breaks if it is wrong, and how much evidence exists that it is right. The first needs a graph. The second needs a ledger of when each mechanism was last validated and by what.

## Architecture

### 1. A mechanism inventory built from two sources
Mechanisms declared with a `DesignIntent` contribute their declared reads, writes and requirements. Older mechanisms have no manifest, so their edges come from runtime tracing over one week of production traffic, using the same tracer that produced read-sets in Day 55. The two sources are merged, and edges that only the tracer saw are marked `inferred` so that the graph shows how much of itself is observed rather than declared.

```python
@dataclass
class MechanismNode:
    name: str
    day_introduced: int
    declared: bool                       # has a DesignIntent manifest
    reads: set[str]
    writes: set[str]
    depends_on: set[str]                 # other mechanisms, declared or traced
    edge_provenance: dict[str, str]      # edge -> "declared" | "inferred"
    last_validated: float | None         # last passing gate eval, battery, or replay
    gate_status: str                     # "passed" | "warn" | "grandfathered" | "never_evaluated"

def build_inventory(manifests, trace_week) -> dict[str, MechanismNode]:
    nodes = {}
    for m in manifests:
        nodes[m.name] = node_from_manifest(m)
    for name, trace in trace_week.by_mechanism().items():
        node = nodes.setdefault(name, undeclared_node(name))
        for key in trace.reads_observed - node.reads:
            node.reads.add(key); node.edge_provenance[f"read:{key}"] = "inferred"
        for key in trace.writes_observed - node.writes:
            node.writes.add(key); node.edge_provenance[f"write:{key}"] = "inferred"
    link_by_state_keys(nodes)            # A writes k, B reads k  =>  B depends_on A
    return nodes
```

The inventory found 64 live mechanisms. 23 had manifests. 41 did not, and 17 of those had edges visible only to the tracer, meaning they touched shared state their own design posts never mentioned.

### 2. Blast radius: transitive dependents
Blast radius is the number of mechanisms that would be affected, directly or indirectly, if a given mechanism produced wrong output. It is computed as the size of the transitive closure over reverse dependency edges.

```python
def blast_radius(nodes: dict[str, MechanismNode]) -> dict[str, int]:
    reverse = defaultdict(set)
    for n in nodes.values():
        for dep in n.depends_on:
            reverse[dep].add(n.name)

    def closure(name, seen=None):
        seen = seen if seen is not None else set()
        for child in reverse[name]:
            if child not in seen:
                seen.add(child)
                closure(child, seen)
        return seen

    return {name: len(closure(name)) for name in nodes}
```

The top of the ranking:

```
mechanism                               day   blast_radius  last_validated   gate_status
regime_segmented_baselines               67        23         55 days ago     grandfathered
effect_ledger                            48        19         41 days ago     grandfathered
threshold_registry                       73        17          9 days ago     passed
content_hash_versioning                  51        14         88 days ago     grandfathered
upstream_epoch_invalidation              64        12         38 days ago     grandfathered
source_correlation_clustering            71        11         22 days ago     grandfathered
```

Day 67 is the central finding. It was built to stop event data polluting the quiet baseline, and over the following weeks it became the input to regime transitions (Days 68, 69), drift detection (Days 65, 72), correlation estimation (Days 86, 87) and the canary's health baselines (Day 84). It sits underneath nearly every statistical decision the system makes, and it has not been looked at since the week it was built.

### 3. Cycle detection
A dependency cycle means two mechanisms each consume the other's output, so an error in either can feed itself. The inventory runs Tarjan's strongly-connected-components algorithm over the graph.

```python
def find_cycles(nodes) -> list[list[str]]:
    index, low, on_stack, stack, sccs, counter = {}, {}, set(), [], [], [0]
    def visit(v):
        index[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); on_stack.add(v)
        for w in nodes[v].depends_on:
            if w not in index:
                visit(w); low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); on_stack.discard(w); comp.append(w)
                if w == v: break
            if len(comp) > 1: sccs.append(comp)
    for v in nodes:
        if v not in index: visit(v)
    return sccs
```

It found two. The first couples Day 59's analyzer trust scoring to Day 64's upstream epoch invalidation: trust scores are invalidated by epochs, and epoch bumps are throttled by the trust score of the producer's analyzer. The second couples Day 72's correlation-drift detection to Day 86's clustering: drift in correlation triggers re-clustering, and the cluster assignment determines which signals the drift detector watches. Neither cycle is necessarily wrong. Both were created by reuse, neither was designed as a feedback loop, and neither has a stability analysis.

### 4. A risk score that combines blast radius and evidence
Each mechanism gets a score that rises with blast radius and with the absence of recent evidence. Evidence is binary at the boundaries and decays in between: a mechanism validated by a passing gate evaluation, a battery fixture or a replay within 30 days counts as fresh.

```python
def evidence_score(node: MechanismNode, now: float) -> float:
    if node.last_validated is None:
        return 0.0
    age_days = (now - node.last_validated) / 86400
    return max(0.0, 1.0 - age_days / 90)          # linear decay over 90 days

def risk(node, radius, now):
    return radius * (1.0 - evidence_score(node, now))
```

With this definition the regime baselines score 23 × (1 − 0.39) ≈ 14.0, the highest in the system. Content-hash versioning is close behind at 14 × (1 − 0.02) ≈ 13.7 because its last validation was 88 days ago, and the effect ledger scores 19 × (1 − 0.54) ≈ 8.7 despite its larger radius because it was exercised more recently. The ranking is sensitive to how evidence is defined, which is itself a finding.

### 5. Retroactive gate run over the grandfathered 41
Mechanisms without manifests cannot be evaluated by a gate that requires one. For each, a manifest was reconstructed from the traced reads and writes, marked `reconstructed=True`, and the gate was run in a read-only mode. Results:

```
verdict            count   note
PASS                 24
WARN                  9    interval straddles a ceiling
BLOCK                 5    see below
CHECK_ERROR           3    insufficient trace data to build a manifest
```

The five blocks were three stale reads (the Day 80 pattern repeated in two older threshold consumers and one baseline reader), one unbounded in-memory structure in Day 48's effect ledger index, and one unlocked write to a counter introduced in Day 46's suspect-state tracker. None had caused an incident that anyone had attributed to it. All five were fixed using patterns the arc already had, and the fixes are in the usual form: versioned reads (Day 81), a bounded deque (Day 83), a per-key lock (Day 75).

### 6. The inventory as a standing artifact
The map is regenerated weekly and diffed. A candidate's `DesignIntent` is now evaluated against it on submission, producing a graph-aware report that sits alongside the nine checks: new edges added, blast radius of every state key touched, any cycle closed, and whether the candidate adds a consumer to a mechanism whose evidence is stale.

```python
def graph_impact(candidate_intent, nodes):
    touched = candidate_intent.reads | candidate_intent.writes
    new_deps = {n.name for n in nodes.values() if n.writes & candidate_intent.reads}
    return {
        "depends_on": sorted(new_deps),
        "max_upstream_risk": max((risk(nodes[d], blast_radius(nodes)[d], now()) for d in new_deps), default=0),
        "closes_cycle": would_close_cycle(nodes, candidate_intent),
    }
```

## Failure Modes
- **Traced edges only cover one week of traffic.** A mechanism that reads a state key only during a rare regime (an event path, a failover) never shows up in that edge, so the map understates dependencies exactly where they matter most. This is the rare-event blind spot the arc has hit before, and the 41 reconstructed manifests inherit it.
- **Blast radius counts dependents, not their importance.** The Day 67 baselines have 23 dependents and one of them is a dashboard-only metric. A weighted version would use severity of the dependent's output, and weighting needs a judgment about which outputs matter that the inventory does not currently have.
- **The evidence decay is a straight line with a 90-day horizon chosen for convenience.** It treats a gate evaluation and a nightly battery fixture as equally strong evidence, which they are not, and it treats time as the only way evidence goes stale, ignoring that a mechanism's evidence goes stale the instant something upstream of it changes.
- **Reconstructed manifests are the tracer's account of what the code did, not what the author intended.** A gate run over a reconstructed manifest can pass a mechanism whose real defect is in an access it never made during the observed week. The 24 passes are weaker evidence than the 24 passes a declared manifest would give.

## What's Next
The inventory exposed something the individual mechanism posts never could: how many tunable numbers the system has accumulated. The threshold registry from Day 73 holds 118 constants now, and the map shows several of them feeding the same downstream decision, tuned on different days by reasoning that never considered the others. Day 94 asks which of the 118 actually matter, and whether any pair of them interacts in a way that nobody tested.

*Orbital Watch: a multi-agent system watching the sky, one failure mode at a time.*
