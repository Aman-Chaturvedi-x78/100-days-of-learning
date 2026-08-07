---
date: 2026-08-08
day: 16
title: "Guardrails & Validation: Keeping Agents From Doing Something Stupid"
tags: [agents, guardrails, validation, safety, structured-output]
---

TL;DR
- Evals (Day 12) tell you the agent is good before you ship. Observability (Day 13) tells you if it's still good after you ship. **Guardrails are the layer that stops a bad output from ever reaching the user or a tool in the first place** — they run inline, on every single call, not just in a test suite or a dashboard.
- Two directions matter: **input guardrails** (is this request safe/well-formed before the model sees it) and **output guardrails** (is what the model produced safe/well-formed before it goes anywhere).
- Structured output validation (Pydantic/JSON schema) is the cheapest, highest-leverage guardrail — most "the agent did something weird" bugs are actually "the agent's output didn't match the shape the code downstream expected."
- A guardrail that silently blocks isn't enough — it needs a **defined fallback** (retry, ask for clarification, degrade gracefully) or you've just traded one failure mode for a worse, silent one.

---

## 1. Where This Fits

Stack so far:

```
Chunking (6) → Vector DB (9) → LangGraph agent (7) → Tool calling (10) → Memory (11) → Evals (12) → Observability (13) → Guardrails (16)
```

Day 12 and 13 are about *detecting* that something went wrong — offline on a golden set, or in prod after the fact. Guardrails are about *preventing* the bad output from doing damage in the moment it happens: before a malformed tool call executes, before an unsafe response reaches the user, before a hallucinated field gets written to a database.

---

## 2. Input Guardrails

The agent's input isn't just the user's message — by Day 11 it also includes retrieved memories, RAG context (Day 6/9), and tool results. Any of those can carry an injection attempt or just malformed data.

```python
from pydantic import BaseModel, field_validator

class UserRequest(BaseModel):
    message: str
    user_id: str

    @field_validator("message")
    @classmethod
    def check_length_and_injection(cls, v):
        if len(v) > 4000:
            raise ValueError("message too long")
        # crude example — real systems use a classifier, not string matching
        if "ignore previous instructions" in v.lower():
            raise ValueError("possible prompt injection")
        return v
```

This is deliberately simple — a production system would use a dedicated classifier or moderation endpoint rather than string matching — but the principle is the same: reject or flag before the message ever reaches the LLM call.

---

## 3. Output Guardrails: Structured Validation

This is the one that actually saves you the most debugging time. If a node downstream expects `{"risk_score": float, "flags": list[str]}`, don't trust the model to always return that shape — validate it, and treat a validation failure as a first-class error path, not a crash.

```python
from pydantic import BaseModel, ValidationError

class RiskAssessment(BaseModel):
    risk_score: float
    flags: list[str]
    reasoning: str

def get_validated_output(llm_response_text, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            return RiskAssessment.model_validate_json(llm_response_text)
        except ValidationError as e:
            if attempt == max_retries:
                raise
            # ask the model to fix its own output — cheap and surprisingly effective
            llm_response_text = repair_with_llm(llm_response_text, str(e))
```

The retry-with-repair pattern matters more than the validation itself. Rejecting bad output without a recovery path just turns a silent bug into a loud outage.

---

## 4. Wiring Guardrails Into the Day 7 Agent

```python
def agent_turn_guarded(state, trace_span):
    # input guardrail
    try:
        UserRequest(message=state["messages"][-1].content, user_id=state["user_id"])
    except ValidationError:
        return {"messages": state["messages"] + [refusal_message()]}

    response = agent_turn(state, trace_span)  # Days 6/7/9/10/11 logic unchanged

    # output guardrail — before a tool call executes
    if response.tool_calls:
        for call in response.tool_calls:
            if not tool_schema_valid(call):
                return {"messages": state["messages"] + [ask_for_clarification()]}

    return {"messages": state["messages"] + [response]}
}
```

Guardrails wrap the existing graph rather than replacing any of it — same instinct as wrapping nodes in spans for Day 13.

---

## 5. Failure Modes

| Failure mode | Cause | Fix |
|---|---|---|
| Guardrail blocks silently, user just sees nothing happen | No fallback path defined | Every guardrail needs a defined response: retry, clarify, or graceful degrade |
| Retry-with-repair loops forever | No max_retries cap | Hard cap (2–3 attempts), then fail loud and log it |
| Guardrail is too strict, blocks valid requests | Rules copied from a different domain/use case | Tune against real traffic samples (Day 13 observability data), not guesswork |
| Guardrail is too loose, doesn't catch the thing it was built for | Regex/string-matching instead of a real classifier | Use a purpose-built moderation/classification model for anything security-relevant |
| Validation passes but the content is still wrong (right shape, wrong facts) | Schema validation ≠ correctness | Schema validation catches malformed output; factual correctness still needs evals (Day 12) |
| Guardrails add noticeable latency | Synchronous classifier call on every turn | Cache repeat inputs, run cheap checks first and only escalate to a heavier check when needed |

---

## 6. Full Pipeline (Tying Days 6, 7, 9, 10, 11, 12, 13, 16 Together)

```python
def run_agent_turn_production(state):
    trace = Span("agent_turn", user_id=state["user_id"])
    try:
        result = agent_turn_guarded(state, trace_span=trace)  # guardrails wrap Days 6/7/9/10/11
        trace.close(status="ok")
    except Exception as e:
        trace.error = str(e)
        trace.close(status="error")
        raise
    finally:
        export_trace(trace)  # Day 13
    return result
```

Evals (12) check quality before shipping. Observability (13) checks quality after shipping. Guardrails (16) are the only layer of the three that can actually stop a bad output from reaching a real user in real time.

---

## Key Takeaways

1. Guardrails are an inline, per-call safety layer — distinct from evals (pre-ship, offline) and observability (post-ship, after the fact).
2. Input guardrails and output guardrails are two separate concerns; most tutorials only cover one.
3. Structured output validation (Pydantic/JSON schema) is the single highest-leverage guardrail for agent reliability — most "weird agent behavior" bugs are shape mismatches, not reasoning failures.
4. A guardrail without a defined fallback (retry, clarify, degrade) just converts a visible failure into a silent one — worse, not better.
5. Schema validation catches malformed output, not wrong output — you still need Day 12's evals for factual/quality correctness.

---

## Links & Resources

- [Pydantic — Validators docs](https://docs.pydantic.dev/latest/concepts/validators/)
- [OWASP — LLM Top 10 (Prompt Injection)](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
- [Anthropic — Building Effective Agents (guardrails considerations)](https://www.anthropic.com/research/building-effective-agents)

---

## Next Steps / Reflections

- [ ] Add real Pydantic schemas to the Day 7 LangGraph agent's tool-call outputs, not just the pseudocode above
- [ ] Build a small "repair with LLM" retry loop and measure how often it actually recovers a bad output vs. just failing again
- [ ] Pull the Day 13 trace data and check how often malformed output has already been happening silently
- [ ] Look into a real moderation/classification endpoint instead of string-matching for the input guardrail example
