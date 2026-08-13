---
date: 2026-08-13
day: 22
title: "Model Context Protocol (MCP): The New Standard for Agent-Tool Integration"
tags: [agents, mcp, tool-calling, protocols, interoperability]
---

TL;DR
- Every agent framework used to invent its own way to hook up tools — custom function schemas, custom auth, custom error handling per integration.
- **MCP (Model Context Protocol)** standardizes the interface between an AI app and external tools/data, the same way LSP standardized editor-to-language-server communication. Originally shipped by Anthropic, now adopted across the ecosystem (OpenAI, Google, and most agent frameworks support it).
- Architecture is **client-server**: an MCP server exposes tools/resources/prompts for one system (GitHub, Slack, Postgres); an MCP client inside the agent app discovers and calls them at runtime — no hardcoded schema per tool.
- The real unlock isn't "yet another function-calling wrapper" — it's **dynamic discovery** (`tools/list`) that lets a new server get plugged in without touching agent code.

---

## 1. Where This Fits

Stack so far:

```
LangGraph agent (7) → Tool calling (10) → Memory (11) → Eval harness (12) → 
Retry caps + HITL (19) → Observability/tracing (21) → MCP (22)
```

Every prior layer assumed tools were hardcoded functions with a name, a schema, and a Python implementation living inside the agent's own codebase. MCP removes that assumption — tools become a pluggable, discoverable interface. This is the layer that turns "an agent with three tools I wrote" into "an agent that can attach to any MCP-compliant server and immediately know how to use it."

---

## 2. Why Custom Function-Calling Doesn't Scale

```python
# What most agent projects look like today
def get_weather(location: str) -> str: ...
def create_github_issue(repo: str, title: str, body: str) -> dict: ...
def search_slack(query: str) -> list: ...

tools = [get_weather, create_github_issue, search_slack]
# Every new tool = new function, new schema, new auth, new error handling,
# duplicated across every project and every framework that wants it.
```

N agent frameworks × M external systems = N×M custom integrations. MCP flips this to N+M: each framework implements one MCP client, each system implements one MCP server. Same shape as before USB-C (one cable standard) vs. after (every device needs its own charger).

---

## 3. Client-Server Architecture

**MCP Server** — exposes capabilities for one system. Runs as a separate process, talks JSON-RPC 2.0 over stdio (local) or HTTP/SSE (remote).

**MCP Client** — lives inside the AI app (Claude, an IDE, a custom LangGraph agent). Connects to one or more servers, discovers what they offer, and calls it.

```python
# Conceptual shape of the client side — no hardcoded tool list
async def connect_and_discover(server_url):
    session = await mcp_client.connect(server_url)
    tools = await session.list_tools()      # dynamic, not hardcoded
    resources = await session.list_resources()
    return session, tools, resources

async def call_mcp_tool(session, tool_name, arguments):
    result = await session.call_tool(tool_name, arguments)
    return result.content
```

---

## 4. Three Primitives a Server Can Expose

**Tools** — functions the model can call. This is what most people reach for first, and it maps directly onto Day 10's tool-calling patterns:

```json
{
  "name": "create_issue",
  "description": "Create a new issue in a GitHub repository",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {"type": "string"},
      "title": {"type": "string"},
      "body": {"type": "string"}
    },
    "required": ["repo", "title"]
  }
}
```

**Resources** — read-only data the app can pull in as context (a file, a DB row, a config) without it being a function call the model decides to invoke. Closer to Day 9's retrieval than Day 10's tool calling.

**Prompts** — reusable prompt templates the server ships alongside its tools, so prompt logic and tool logic live together instead of scattered across every client that wants to use the server well.

Most integrations only use Tools and miss the other two — Resources in particular overlaps with what a lot of custom RAG plumbing is trying to do manually.

---

## 5. Discovery Over Hardcoding

The core mechanical difference from Day 10's approach:

```python
# Day 10 style: schema is written by hand, ships with the agent code
tools = [WEATHER_SCHEMA, GITHUB_SCHEMA, SLACK_SCHEMA]

# MCP style: schema is fetched at connect time
tools = await session.list_tools()  
# → new server plugged in, agent already knows how to use it, zero code change
```

This is what makes "swap the Researcher node's tools without touching the graph" realistic instead of aspirational.

---

## 6. Wrapping MCP Calls in Existing Safety Layers

MCP calls are remote calls, not local function calls — they inherit every failure mode a network call has, which means they need the same guardrails already built:

```python
async def call_mcp_tool_safe(session, tool_name, arguments, retry_cap=3):
    """Wraps Day 19's bounded retry logic around an MCP tool call."""
    for attempt in range(retry_cap):
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments), timeout=10
            )
            return {"success": True, "result": result, "attempts": attempt + 1}
        except asyncio.TimeoutError:
            if attempt == retry_cap - 1:
                return {"success": False, "reason": "timeout", "attempts": retry_cap}
        except Exception as e:
            if attempt == retry_cap - 1:
                return {"success": False, "reason": str(e), "attempts": retry_cap}
    return {"success": False, "reason": "exhausted retries"}
```

Write/destructive tool calls (delete, send, deploy) still need the Day 19 HITL checkpoint before execution — MCP doesn't grant permission by itself, it just standardizes the plumbing.

---

## 7. Failure Modes

| Failure mode | Cause | Fix |
|---|---|---|
| Treating MCP calls like local function calls | No timeout/retry wrapping on remote calls | Reuse Day 19's bounded-retry pattern around every MCP call, not just LLM calls |
| Over-exposing tools | Server dumps 40 tools into `tools/list` | Curate what's exposed per use case — degrades tool-selection accuracy otherwise |
| Auto-executing write actions | Treating `tool_use` output as safe to run unattended | Keep write/destructive actions behind explicit HITL confirmation |
| Silent schema drift | Server updates a tool's input shape with no version bump | Version server schemas; breaking changes should be loud, not silent |
| Poor tool descriptions | Schema written like an internal variable name, not a docstring | Write descriptions as if documenting for a junior engineer — the model only knows what the schema says |
| No tracing on MCP spans | Observability (Day 21) only covers LLM/graph calls | Extend the Day 21 span tree to wrap MCP tool calls too |

---

## 8. Full Pipeline (Tying Days 7, 10, 19, 21, 22 Together)

```python
async def researcher_node(state):
    # Day 7: this is one node in the LangGraph state machine
    session = state["mcp_session"]  # connected once, reused across the run

    # Day 22: discover tools dynamically instead of hardcoding
    available_tools = await session.list_tools()

    # Day 10: model decides which tool + args, same as before —
    # the only difference is where the schema came from
    tool_call = decide_tool_call(state["query"], available_tools)

    # Day 19 + this doc: bounded retries wrap the actual call
    result = await call_mcp_tool_safe(session, tool_call["name"], tool_call["args"])

    # Day 21: span logged for this call, correlated to the run's trace id
    log_span(node="researcher", tool=tool_call["name"], result=result)

    return {"researcher_output": result}
```

---

## Key Takeaways

1. MCP standardizes agent-to-tool communication the way LSP standardized editor-to-language-server communication — one protocol instead of N×M custom integrations.
2. Client-server split with three primitives (Tools, Resources, Prompts) — most people only use Tools and miss the other two.
3. Dynamic discovery (`tools/list` at connect time) is the actual unlock, not just "function calling with extra steps."
4. MCP calls are remote calls — every safety pattern already built for LLM calls (retry caps, timeouts, HITL on writes, tracing) needs to wrap MCP calls too.
5. Curate exposed tools and write real descriptions — an MCP server with 40 loosely-described tools degrades the model's tool-selection accuracy same as any bloated tool list would.

---

## Links & Resources

- [Model Context Protocol — official spec](https://modelcontextprotocol.io)
- [Anthropic — Introducing the Model Context Protocol](https://www.anthropic.com/news/model-context-protocol)
- [MCP GitHub organization](https://github.com/modelcontextprotocol)
- [Anthropic — Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)

---

## Next Steps / Reflections

- [ ] Stand up a minimal local MCP server (stdio transport) exposing one real tool
- [ ] Swap the Researcher node's hardcoded tool functions for an MCP client session
- [ ] Extend Day 21's span logging to cover MCP tool calls, not just LLM calls
- [ ] Test what happens when the MCP server drops mid-run — confirm Day 19's retry cap actually catches it
- [ ] Write real tool descriptions for the local server and check if tool-selection accuracy changes vs. terse ones
