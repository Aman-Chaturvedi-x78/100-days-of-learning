---
date: 2026-08-02
day: 12
title: "Eval Harnesses: How Do You Know Your Agent Actually Works"
tags: [agents, evals, testing, llm-as-judge, observability]
---

TL;DR
- Traditional unit tests assert exact outputs. LLM agents are non-deterministic — same input can produce different (both valid) outputs. Evals need a different shape.
- Three levels to test: **tool-call correctness** (did it call the right tool with the right args), **trajectory correctness** (did it take a sane path to get there), **output quality** (is the final answer actually good).
- The first two are deterministic and cheap — assert them directly. The third usually needs an **LLM-as-judge**, which brings its own failure modes (position bias, verbosity bias, self-preference).
- A harness is just: a golden dataset of (input, expected-ish output) pairs + a runner + a scorer. The hard part is building the golden dataset and keeping the judge honest, not the runner.

---

## 1. Where This Fits

Stack so far:

```
Chunking (6) → Vector DB (9) → LangGraph agent (7) → Tool calling (10) → Memory (11)
```

Every layer above adds a new way for the agent to be subtly wrong: wrong chunk retrieved, wrong tool called, memory that shapes a bad response, a graph that loops forever. An eval harness is the layer that catches regressions across all of them before they hit a user — the thing that answers "did my change actually make this better, or did it just feel better on the three examples I tried by hand."

---

## 2. Why Normal Tests Don't Work Here

```python
# This works for a pure function
assert add(2, 2) == 4

# This doesn't work for an agent
assert agent.run("What's the weather in Delhi?") == "It's 34°C and sunny in Delhi."
```

The second assertion is brittle in two directions: it fails on a *correct* answer phrased differently, and it passes on a *wrong* answer that happens to pattern-match. You need assertions that check the right *properties*, not string equality.

---

## 3. Three Levels of Eval

**Level 1 — Tool-call correctness (deterministic, cheap)**

Did the agent call the right tool with reasonable arguments? This is a structural check, no LLM needed to grade it:

```python
def eval_tool_call(trace, expected_tool, required_args=None):
    tool_calls = [s for s in trace if s["type"] == "tool_call"]
    if not any(tc["name"] == expected_tool for tc in tool_calls):
        return {"pass": False, "reason": f"never called {expected_tool}"}
    matched = next(tc for tc in tool_calls if tc["name"] == expected_tool)
    if required_args:
        missing = [k for k in required_args if k not in matched["input"]]
        if missing:
            return {"pass": False, "reason": f"missing args: {missing}"}
    return {"pass": True}
```

**Level 2 — Trajectory correctness (deterministic, cheap)**

Did the agent take a sane path — right order, no redundant loops, memory retrieval happened before generation, etc? Same idea, just checking sequence instead of a single call:

```python
def eval_trajectory(trace, expected_sequence):
    actual = [s["name"] for s in trace if s["type"] == "tool_call"]
    return {"pass": actual == expected_sequence, "actual": actual, "expected": expected_sequence}
```

**Level 3 — Output quality (needs judgment, expensive)**

Is the final answer actually good — correct, relevant, appropriately scoped? No deterministic check works here; you need either an exact-match/semantic-similarity score against a reference answer, or an LLM-as-judge.

---

## 4. LLM-as-Judge

For open-ended outputs, a second LLM call scores the first one against a rubric:

```python
JUDGE_PROMPT = """
You are grading an AI agent's response.

User query: {query}
Agent response: {response}
Reference answer (for guidance, not exact match): {reference}

Score the response 1-5 on:
- correctness: is it factually right?
- relevance: does it actually answer the query?
- groundedness: does it stick to retrieved context, or does it hallucinate?

Return JSON: {{"correctness": int, "relevance": int, "groundedness": int, "reasoning": str}}
"""

def judge_response(query, response, reference):
    result = llm.invoke(JUDGE_PROMPT.format(query=query, response=response, reference=reference))
    return json.loads(result.content)
```

This works, but the judge has known biases worth knowing before you trust the numbers:

- **Position bias** — when comparing two responses, the judge tends to favor whichever is shown first. Fix: run both orderings and average.
- **Verbosity bias** — longer responses score higher independent of quality. Fix: explicitly instruct the judge to penalize unnecessary length, or normalize for it in analysis.
- **Self-preference** — a model judging its own family's outputs (e.g. Claude judging Claude) tends to score them slightly higher than a truly neutral judge would. Fix: where it matters, cross-check with a different model family or a human spot-check.

None of this means LLM-as-judge is useless — it means treat the score as a noisy signal, not ground truth, and validate it against human judgment on a sample before trusting it at scale.

---

## 5. The Golden Dataset

The harness itself is trivial. The actual work is the dataset — a set of realistic (input, expected-behavior) pairs that actually exercise the failure modes you care about:

```python
golden_set = [
    {
        "id": "mem-001",
        "query": "What's my name?",
        "setup": {"memories": ["User's name is Aman"]},
        "expected_tool": None,  # should answer from memory, no tool call needed
        "reference": "Your name is Aman.",
    },
    {
        "id": "tool-003",
        "query": "What's the weather in Delhi right now?",
        "expected_tool": "weather_fetch",
        "required_args": ["location_name"],
        "reference": None,  # output varies, don't grade content, just the tool call
    },
    # ...
]
```

Good golden sets aren't random — they deliberately include edge cases: ambiguous queries, queries that shouldn't trigger a tool call, adversarial memory (stale/contradicted facts from Day 11's failure table), multi-turn context that requires short-term state.

---

## 6. Minimal Harness (Runner + Scorer)

```python
def run_eval_suite(agent, golden_set):
    results = []
    for case in golden_set:
        trace = agent.run(case["query"], setup=case.get("setup", {}))

        checks = {}
        if case.get("expected_tool") is not None:
            checks["tool_call"] = eval_tool_call(trace, case["expected_tool"], case.get("required_args"))
        if case.get("reference"):
            checks["quality"] = judge_response(case["query"], trace["final_response"], case["reference"])

        results.append({"id": case["id"], "checks": checks})
    return results

def summarize(results):
    total = len(results)
    tool_pass = sum(1 for r in results if r["checks"].get("tool_call", {}).get("pass", True))
    avg_correctness = sum(r["checks"].get("quality", {}).get("correctness", 0) for r in results) / total
    return {"tool_call_pass_rate": tool_pass / total, "avg_correctness": avg_correctness}
```

Run this on every change to the agent (prompt, model, tool set) and diff against the last run. That diff — not a single run's score in isolation — is what tells you whether a change helped.

---

## 7. Failure Modes

| Failure mode | Cause | Fix |
|---|---|---|
| Brittle exact-match assertions | Testing agent output like a pure function | Test properties (tool called, args present) not strings |
| Judge scores don't reflect reality | Position/verbosity/self-preference bias | Randomize order, penalize length explicitly, spot-check against humans |
| Golden set doesn't catch regressions | Dataset only covers happy paths | Deliberately include edge cases, adversarial memory, ambiguous queries |
| "It works on my 3 examples" | No systematic eval, just vibes-based testing | Golden set + automated runner, run on every change |
| Eval suite too slow/expensive to run often | Every case does a full LLM judge call | Reserve judge calls for Level 3 only; Levels 1-2 are free and should run constantly |
| False confidence from a single aggregate score | One number hides which specific behaviors broke | Track per-category pass rates (tool calls, trajectories, quality) separately, not one blended score |

---

## 8. Full Pipeline (Tying Days 6, 7, 9, 10, 11, 12 Together)

```python
def evaluate_agent_change(agent, golden_set):
    # Days 6/9: retrieval quality is implicitly tested if golden set queries need RAG
    # Day 7: trajectory checks validate the LangGraph path taken
    # Day 10: tool-call checks validate correct tool selection/args
    # Day 11: memory setup in golden cases validates user-scoped recall, not leakage
    results = run_eval_suite(agent, golden_set)
    summary = summarize(results)

    regressions = [r for r in results if not r["checks"].get("tool_call", {}).get("pass", True)]
    if regressions:
        print(f"⚠️ {len(regressions)} tool-call regressions: {[r['id'] for r in regressions]}")

    return summary
```

Every layer built in Days 6-11 shows up as a specific, checkable property in the eval — the harness is what turns "I made a change and it feels fine" into "I made a change and here's the diff in pass rate."

---

## Key Takeaways

1. Don't grade agent output like a pure function — exact-match assertions are both too strict and too loose for non-deterministic systems.
2. Split evals into deterministic (tool call, trajectory) and judgment-based (output quality) — run the cheap ones constantly, reserve LLM-as-judge for what actually needs it.
3. LLM-as-judge has real, documented biases (position, verbosity, self-preference) — treat scores as a signal to validate, not ground truth.
4. The golden dataset is the actual product here, not the runner code — it needs to deliberately cover edge cases and known failure modes, not just happy paths.
5. Diff pass rates across changes, not single-run scores in isolation — that's what tells you if a change actually helped.

---

## Links & Resources

- [OpenAI Evals framework](https://github.com/openai/evals)
- [LangSmith Evaluation docs](https://docs.smith.langchain.com/evaluation)
- [Position bias in LLM-as-judge — Zheng et al., 2023 (MT-Bench / Chatbot Arena paper)](https://arxiv.org/abs/2306.05685)
- [Anthropic — Building Effective Agents (evaluation section)](https://www.anthropic.com/research/building-effective-agents)

---

## Next Steps / Reflections

- [ ] Build a real golden set (~20 cases) for the Day 7 LangGraph agent, covering tool calls, memory recall, and at least 3 adversarial/edge cases
- [ ] Wire up automatic judge-order randomization to check for position bias in my own harness
- [ ] Compare LLM-as-judge scores against my own manual grading on 10 cases to see how much they actually agree
- [ ] Add pass-rate tracking over time (CSV or simple dashboard) so regressions are visible across commits, not just single runs
