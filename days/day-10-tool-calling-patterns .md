---
date: 2026-07-31
day: 10
title: "Tool Calling / Function Calling Patterns for Agents"
tags: [agents, tool-calling, function-calling, react, langgraph]
---

TL;DR
- Function calling is what turns an LLM from "text predictor" into "agent" — the model doesn't call the function itself, it emits structured JSON describing which function to call and with what args; your code executes it and feeds the result back.
- Anthropic's tool use and OpenAI's function calling are structurally similar: you send a tool schema, the model returns a `tool_use`/`tool_calls` block instead of (or alongside) text, you run the tool, and send the result back as a new message.
- The ReAct pattern (Reason → Act → Observe, loop) is the mental model underneath almost every agent framework's tool-calling loop, including LangGraph's.
- Real reliability problems aren't "will the model call a tool" — it's malformed args, wrong tool choice, and infinite retry loops. Schema strictness + a hard step cap fix most of it.

---

## 1. The Core Loop

Every tool-calling agent, regardless of framework, runs the same loop:

```
1. Send user input + tool schemas to the model
2. Model responds with either:
   a. plain text (done), or
   b. a tool_use block (name + arguments as JSON)
3. Your code executes the actual function
4. Send the tool's result back to the model as a new message
5. Repeat until the model responds with plain text
```

This connects directly to Day 7 — LangGraph's conditional edges are literally built to implement step 2's branch ("did the model ask for a tool, or is it done?") as a graph routing decision instead of an if/else buried in a while loop.

---

## 2. Defining a Tool Schema

Both Anthropic and OpenAI use JSON Schema to describe tool inputs. Anthropic's format:

```python
tools = [
    {
        "name": "get_weather",
        "description": "Get current weather for a location. Use this whenever the user asks about weather, temperature, or conditions in a specific place.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City and country, e.g. 'Berlin, Germany'"
                },
                "unit": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"]
                }
            },
            "required": ["location"]
        }
    }
]
```

**The description field matters more than people think.** The model picks which tool to call (and whether to call one at all) based almost entirely on the `description` text — it's a prompt, not documentation. Vague descriptions cause wrong-tool selection way more often than bad prompting elsewhere in the system.

---

## 3. Handling the Response

```python
import anthropic

client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=1024,
    tools=tools,
    messages=[{"role": "user", "content": "What's the weather in Munich?"}]
)

for block in response.content:
    if block.type == "tool_use":
        tool_name = block.name
        tool_input = block.input
        tool_call_id = block.id
        # run the actual function
        result = run_tool(tool_name, tool_input)

        # send result back
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": str(result)
            }]
        })
```

Key detail: the tool result goes back in a `user`-role message with a `tool_result` block referencing the original `tool_use_id`. Get the ID matching wrong and the API rejects the request — this is the single most common integration bug.

---

## 4. ReAct: The Pattern Underneath

ReAct (Yao et al., 2022) formalized what most agent loops now do implicitly:

```
Thought: I need the current weather to answer this.
Action: get_weather(location="Munich, Germany")
Observation: {"temp": 18, "condition": "cloudy"}
Thought: I have what I need now.
Answer: It's 18°C and cloudy in Munich.
```

Modern tool-calling APIs collapse "Thought" into the model's internal reasoning and "Action" into the structured `tool_use` block — but the loop (reason, act, observe, repeat) is identical. Recognizing this makes it obvious why a LangGraph agent node and a raw `while` loop around the Anthropic SDK are doing the same job with different scaffolding.

---

## 5. Parallel Tool Calls

Models can request multiple tools in a single response when the calls don't depend on each other:

```python
# response.content might contain TWO tool_use blocks:
# [tool_use(get_weather, "Berlin"), tool_use(get_weather, "Munich")]
```

Run these concurrently (`asyncio.gather` / `ThreadPoolExecutor`), then return **both** results in the same follow-up message, each tagged with its own `tool_use_id`. Sending them as separate follow-up turns instead of one batched message breaks the conversation's tool-result pairing.

---

## 6. Where It Actually Breaks

| Failure mode | Cause | Fix |
|---|---|---|
| Wrong tool selected | Vague/overlapping descriptions | Rewrite descriptions to be mutually exclusive; add "use this when / don't use this when" |
| Malformed arguments | Schema too loose (e.g. no `enum`, no `required`) | Tighten schema — `enum` for fixed choices, mark fields `required` |
| Infinite retry loop | No step cap, model keeps retrying a failing tool | Hard cap (e.g. max 5 tool calls per turn) + surface the error as an observation instead of silently retrying |
| Hallucinated tool name | Tool not in the current schema list but model "remembers" it from earlier context | Always pass the full current tool list every turn — don't assume it persists |
| Silent partial failure | Tool throws, but code doesn't catch it before sending "result" back | Wrap every tool execution in try/except and return the error message as the tool_result content — let the model reason about the failure |

---

## Key Takeaways

1. Function calling isn't magic — it's structured JSON in, your code executes, structured result back. The model never touches your actual functions.
2. Tool descriptions are prompts. Treat them with the same care as a system prompt.
3. ReAct is the theoretical scaffolding; LangGraph nodes/edges and raw SDK while-loops are just different implementations of the same reason-act-observe cycle.
4. Most "the agent isn't working" bugs are schema looseness or missing step caps, not model capability.
5. This plugs directly into the Day 7 LangGraph agent as a tool-calling node — the `retrieve_node` from Day 9 is already halfway to being a tool the model can choose to call instead of a hardcoded edge.

---

## Links & Resources

- [Anthropic Tool Use docs](https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview)
- [OpenAI Function Calling docs](https://platform.openai.com/docs/guides/function-calling)
- [ReAct paper — Yao et al., 2022](https://arxiv.org/abs/2210.03629)
- [LangGraph tool-calling agent guide](https://langchain-ai.github.io/langgraph/how-tos/tool-calling/)

---

## Next Steps / Reflections

- [ ] Refactor the Day 9 `retrieve_node` into an actual tool the LangGraph agent chooses to call, instead of a fixed edge
- [ ] Add a step cap + error-surfacing wrapper to any tool-calling code before it goes in a portfolio project
- [ ] Try parallel tool calls on a multi-source lookup (e.g. weather + calendar in one turn)
