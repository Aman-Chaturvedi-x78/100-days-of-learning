---
date: 2026-08-12
day: 20
title: "Structured Output & TypeAdapter Validation (Claude & OpenAI)"
tags: [agents, structured-output, validation, pydantic, typeadapter, json-schema]
---

TL;DR
- **Structured Output** (Claude API, OpenAI API) guarantees the model returns valid JSON matching your schema—no parsing fallbacks needed.
- **Pydantic TypeAdapter** is the modern way to validate and parse structured responses in Python—it's faster than `model_validate()` and works with complex nested types.
- Combining structured output + TypeAdapter eliminates most tool-calling validation bugs (malformed args, missing required fields, wrong types).
- The pattern integrates seamlessly with Days 7 (LangGraph), 10 (tool calling), and 18 (multi-agent systems)—you get schema validation for free.

---

## 1. Why Structured Output Matters

**Problem (Day 10 revisited):** Tool-calling agents emit JSON that the model *intended* to be valid, but often isn't:
- Missing required fields
- Wrong data types (string instead of int)
- Hallucinated enum values
- Nesting mismatches

**Old workaround:**
```python
try:
    args = json.loads(tool_input)
    # Manual validation per field
except json.JSONDecodeError:
    # Retry or surface error
```

**New approach (2024+):**
Models can now be forced to output schema-compliant JSON via the API itself—no runtime validation needed because the model was constrained during generation.

---

## 2. Pydantic v2 TypeAdapter (The Modern Pattern)

TypeAdapter is Pydantic v2's answer to "validate complex nested types without a full model class":

```python
from pydantic import TypeAdapter, BaseModel, Field
from typing import Literal
import json

# Define your expected response shape
class ExtractedInfo(BaseModel):
    """Info extracted from user message by the agent."""
    action: Literal["search", "summarize", "analyze"]
    query: str = Field(..., min_length=1, max_length=500)
    include_citations: bool = False
    max_results: int = Field(default=5, ge=1, le=20)

# Create the adapter (reuse across calls for efficiency)
adapter = TypeAdapter(ExtractedInfo)

# Validate structured output from model
raw_json = '{"action": "search", "query": "llm agents", "include_citations": true}'
parsed = adapter.validate_json(raw_json)
print(parsed)
# ExtractedInfo(action='search', query='llm agents', include_citations=True, max_results=5)

# Also works with dict
parsed = adapter.validate_python({"action": "search", "query": "llm agents"})
```

**Key advantages over `model_validate()`:**
- Specialized for direct type validation (not full model lifecycle)
- Faster for repeated validations (adapter is stateful)
- Handles complex types (`dict`, `list`, `Union`, `Literal`) without extra boilerplate

---

## 3. Claude Structured Output (Latest)

Claude 3.5 Sonnet (2024-10 update) supports `response_format` parameter:

```python
import anthropic
from pydantic import BaseModel, Field, TypeAdapter
from typing import Literal

client = anthropic.Anthropic()

# Define schema
class WeatherQuery(BaseModel):
    """User's weather request."""
    location: str = Field(..., description="City and country, e.g. 'Berlin, Germany'")
    unit: Literal["celsius", "fahrenheit"] = Field(
        default="celsius", 
        description="Temperature unit"
    )
    forecast_days: int = Field(
        default=1, 
        ge=1, 
        le=7, 
        description="How many days ahead to forecast"
    )

# Use structured output
response = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "WeatherQuery",
            "description": "Parsed weather request",
            "schema": WeatherQuery.model_json_schema(),
            "strict": True  # Enforce exact schema compliance
        }
    },
    messages=[
        {
            "role": "user",
            "content": "I need weather for London next 3 days in Fahrenheit"
        }
    ]
)

# Parse guaranteed-valid JSON
adapter = TypeAdapter(WeatherQuery)
raw_output = response.content[0].text
parsed = adapter.validate_json(raw_output)
print(f"Location: {parsed.location}, Unit: {parsed.unit}, Days: {parsed.forecast_days}")
# Location: London, United Kingdom, Unit: fahrenheit, Days: 3
```

**Claude structured output guarantees:**
- ✅ Valid JSON structure (format errors → API error, not malformed return)
- ✅ All required fields present
- ✅ Types and enums match schema exactly
- ✅ No additional fields unless explicitly allowed
- ✅ Set `strict=True` for production — unambiguous, reproducible parsing

---

## 4. OpenAI Structured Output (with `json_schema`)

OpenAI's GPT-4o and o1 support `response_format` with JSON schema:

```python
from openai import OpenAI
from pydantic import BaseModel, Field, TypeAdapter
from typing import Literal
import json

client = OpenAI()

class ToolCall(BaseModel):
    """Structured tool request from the model."""
    tool_name: Literal["search_docs", "run_code", "send_email"]
    parameters: dict = Field(
        default_factory=dict,
        description="Tool-specific parameters"
    )
    reasoning: str = Field(..., min_length=10, max_length=200)

# GPT-4o with structured output
response = client.chat.completions.create(
    model="gpt-4o-2024-08-06",
    max_tokens=1024,
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "ToolCall",
            "schema": ToolCall.model_json_schema(),
            "strict": True
        }
    },
    messages=[
        {
            "role": "user",
            "content": "Search for recent papers on mixture of experts and explain why"
        }
    ]
)

adapter = TypeAdapter(ToolCall)
parsed = adapter.validate_json(response.choices[0].message.content)
print(f"Tool: {parsed.tool_name}, Reasoning: {parsed.reasoning}")
```

**OpenAI structured output behavior:**
- Returns `finish_reason="stop"` and JSON in `content` field
- Schema validation happens server-side
- Invalid schema → API error immediately (catches bugs early)
- Use `strict=True` for reproducible parsing across model versions

---

## 5. Multi-Agent Example: Orchestrator with Structured Tool Selection

Real-world pattern from Day 18 (multi-agent orchestration) + Day 10 (tool calling):

```python
from pydantic import BaseModel, Field, TypeAdapter
from typing import Literal, Optional
import anthropic
import json

# Define agent responsibilities
class AgentDispatch(BaseModel):
    """Orchestrator's decision on which agent handles the request."""
    target_agent: Literal["researcher", "writer", "critic"]
    task: str = Field(..., description="What the agent should do")
    priority: Literal["high", "normal", "low"] = "normal"
    fallback_agent: Optional[Literal["researcher", "writer", "critic"]] = None
    metadata: dict = Field(default_factory=dict)

class ResearcherOutput(BaseModel):
    """Researcher agent's structured response."""
    sources: list[str] = Field(..., min_items=1, max_items=10)
    summary: str
    confidence: float = Field(..., ge=0.0, le=1.0)

class CriticOutput(BaseModel):
    """Critic agent's structured feedback."""
    approved: bool
    issues: list[str] = Field(default_factory=list)
    revision_count: int = 0

# Orchestrator: dispatch to specialized agents
def orchestrate(user_input: str) -> AgentDispatch:
    """Route user request to appropriate agent."""
    client = anthropic.Anthropic()
    
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=512,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "AgentDispatch",
                "schema": AgentDispatch.model_json_schema(),
                "strict": True
            }
        },
        messages=[{
            "role": "user",
            "content": f"""Given this request, decide which agent should handle it:
            
Request: {user_input}

Respond with JSON indicating target agent, task description, and priority."""
        }]
    )
    
    adapter = TypeAdapter(AgentDispatch)
    return adapter.validate_json(response.content[0].text)

# Researcher: fetch sources and summarize
def researcher_agent(task: str) -> ResearcherOutput:
    """Simulate researcher gathering information."""
    client = anthropic.Anthropic()
    
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "ResearcherOutput",
                "schema": ResearcherOutput.model_json_schema(),
                "strict": True
            }
        },
        messages=[{
            "role": "user",
            "content": f"""Research this topic and provide structured output: {task}
            
Include real sources (URLs or citations), a summary, and confidence level."""
        }]
    )
    
    adapter = TypeAdapter(ResearcherOutput)
    return adapter.validate_json(response.content[0].text)

# Critic: validate output
def critic_agent(content: str, revision_count: int = 0) -> CriticOutput:
    """Review output and provide structured feedback."""
    client = anthropic.Anthropic()
    
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=512,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "CriticOutput",
                "schema": CriticOutput.model_json_schema(),
                "strict": True
            }
        },
        messages=[{
            "role": "user",
            "content": f"""Critically review this content (revision #{revision_count + 1}):

{content}

Respond with approval status and any issues to fix."""
        }]
    )
    
    adapter = TypeAdapter(CriticOutput)
    return adapter.validate_json(response.content[0].text)

# Full pipeline
def multi_agent_pipeline(user_input: str, max_revisions: int = 3):
    """Orchestrate researchers and critics."""
    print(f"🎯 User: {user_input}")
    
    # Step 1: Dispatch
    dispatch = orchestrate(user_input)
    print(f"📤 Dispatching to: {dispatch.target_agent} (priority: {dispatch.priority})")
    
    # Step 2: Researcher gathers
    if dispatch.target_agent == "researcher":
        research = researcher_agent(dispatch.task)
        print(f"📚 Found {len(research.sources)} sources (confidence: {research.confidence})")
        content = research.summary
        revision_count = 0
        
        # Step 3: Critique loop (Day 19 pattern: bounded with counter)
        while revision_count < max_revisions:
            critique = critic_agent(content, revision_count)
            print(f"✓ Revision {revision_count + 1}: Approved={critique.approved}")
            
            if critique.approved:
                print("🎉 Pipeline complete!")
                return {
                    "status": "success",
                    "content": content,
                    "sources": research.sources,
                    "revisions": revision_count
                }
            
            if critique.issues:
                print(f"⚠️ Issues: {critique.issues}")
                # In real system: pass issues back to researcher for revision
                revision_count += 1
            else:
                break
        
        print(f"⏹️ Max revisions ({max_revisions}) reached")
        return {
            "status": "max_revisions",
            "content": content,
            "sources": research.sources,
            "revisions": revision_count
        }

# Run it
if __name__ == "__main__":
    result = multi_agent_pipeline("What are recent breakthroughs in mixture of experts models?")
    print(json.dumps(result, indent=2))
```

**Why this pattern works:**
- ✅ Each agent's output is type-safe and validated server-side
- ✅ No manual schema validation code in your loops
- ✅ Integrates with Day 19's bounded revision counter
- ✅ Day 18's orchestrator becomes a structured dispatcher
- ✅ Tool selection from Day 10 guaranteed to have correct types

---

## 6. Validation Fallback (For Models Without Structured Output)

Older models (gpt-3.5, claude-3-opus) need client-side validation:

```python
from pydantic import ValidationError, TypeAdapter
import json
import anthropic

def validate_with_fallback(raw_text: str, schema_model, max_retries: int = 2):
    """Try to parse; retry with guidance if it fails."""
    adapter = TypeAdapter(schema_model)
    
    # Attempt 1: Direct parse
    try:
        return adapter.validate_json(raw_text)
    except ValidationError as e:
        print(f"⚠️ Validation failed: {e}")
        if max_retries == 0:
            raise
    
    # Attempt 2: Extract JSON if wrapped in text
    try:
        # Try to find JSON block
        import re
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            return adapter.validate_json(json_match.group())
    except ValidationError:
        pass
    
    # Attempt 3: Retry with explicit correction prompt
    client = anthropic.Anthropic()
    correction_response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Fix this JSON to match this schema:

Schema: {json.dumps(schema_model.model_json_schema(), indent=2)}

Broken JSON: {raw_text}

Return only valid JSON, no explanation."""
        }]
    )
    
    corrected = correction_response.content[0].text
    return adapter.validate_json(corrected)

# Usage
from pydantic import BaseModel, Field

class SimpleTask(BaseModel):
    action: str
    priority: int = Field(ge=1, le=5)

result = validate_with_fallback(
    '{"action": "search", "priority": "3"}',  # priority is string, should be int
    SimpleTask
)
print(result)
# SimpleTask(action='search', priority=3)
```

---

## 7. Comparison: Structured Output vs Manual Validation

| Aspect | Structured Output (Claude/OpenAI) | Manual TypeAdapter Validation | Legacy `json.loads()` |
|--------|-------|-------|-------|
| Schema enforcement | ✅ Server-side (guaranteed) | ✅ Client-side (can fail) | ❌ None |
| Type coercion | ✅ "3" → 3 (as per schema) | ✅ (Pydantic handles it) | ❌ Breaks on type mismatches |
| Latency | Similar (validation in API) | Minimal (local check) | Minimal (no check) |
| Debugging | Errors caught at API layer | Clear validation errors | Silent data corruption risk |
| Cost | Included in API call | No extra cost | No extra cost |
| Best for | Production agents (reliability first) | Batch/dev (speed ok) | Don't use (legacy only) |

**Recommendation:** Use structured output for production agents. Use TypeAdapter validation for local dev/testing.

---

## 8. Production Checklist

```python
# ✅ Do this:

# 1. Define schema as Pydantic model (reusable, self-documenting)
class AgentResponse(BaseModel):
    action: str
    confidence: float = Field(ge=0, le=1)

# 2. Create one adapter per schema (reuse across requests)
response_adapter = TypeAdapter(AgentResponse)

# 3. Always use structured output in production
response = client.messages.create(
    ...,
    response_format={
        "type": "json_schema",
        "json_schema": {
            "schema": AgentResponse.model_json_schema(),
            "strict": True  # ⭐ This is important
        }
    }
)

# 4. Validate parsed response (extra safety layer)
parsed = response_adapter.validate_json(response.content[0].text)

# 5. Log schema mismatches (should be zero in production)
assert parsed.confidence >= 0 and parsed.confidence <= 1

# ❌ Don't do this:

# ❌ Loose schema (optional fields, no enums)
# ❌ strict=False (allows extra fields, non-compliant models)
# ❌ No fallback handling (crash if model breaks contract)
# ❌ One-off validation per call (no schema reuse)
```

---

## Key Takeaways

1. **Structured Output is now the standard** for agent responses (2024+) — it eliminates parsing bugs at the API layer.
2. **Pydantic TypeAdapter** is the modern validation tool — use it instead of manual `json.loads()` and field-by-field checks.
3. **Combine with Day 7 (LangGraph)** — tool nodes can guarantee their return schemas are valid.
4. **Combines with Day 10 (Tool Calling)** — agent-emitted tool arguments are always schema-compliant.
5. **Bounded by Day 19** — add a retry counter in your validation loop, don't let a malformed response trigger infinite retries.

---

## Links & Resources

- [Claude Structured Output Docs (2024)](https://docs.anthropic.com/en/docs/guides/structured-outputs)
- [OpenAI JSON Mode & Schema Docs](https://platform.openai.com/docs/guides/json-mode)
- [Pydantic v2 TypeAdapter Docs](https://docs.pydantic.dev/latest/api/type_adapter/)
- [JSON Schema Guide (JSON Schema Specification)](https://json-schema.org/)
- [ReAct + Structured Output (LangGraph example)](https://langchain-ai.github.io/langgraph/how-tos/tool-calling/)

---

## Next Steps / Reflections

- [ ] Refactor Day 7 LangGraph agent nodes to return Pydantic models validated with TypeAdapter
- [ ] Add structured output to Day 10's tool-calling wrapper — guarantee all tool args pass schema validation before execution
- [ ] Build a schema registry (JSON Schema files in `/schemas/`) and auto-generate Pydantic models from them
- [ ] Wire Day 20's structured parsing into Day 19's revision loop — ensure critic feedback is itself structured
- [ ] Profile TypeAdapter validation latency on 1000+ calls to confirm overhead is negligible vs raw `json.loads()`
- [ ] Test fallback validation (Day 20 section 6) on an older model (gpt-3.5) to confirm retry pattern works
