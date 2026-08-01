---
date: 2026-08-01
day: 11
title: "Agent Memory: Short-Term State vs Long-Term Memory"
tags: [agents, memory, langgraph, vector-db, rag]
---

TL;DR
- **Short-term memory** = what's in the context window for *this* conversation/thread. It's just message history (or graph state), and it disappears when the session ends unless you checkpoint it.
- **Long-term memory** = facts/preferences that survive across sessions, usually stored outside the model in a vector DB (or plain DB) and *retrieved* back in — it's RAG, just applied to "things the agent learned about the user" instead of documents.
- The two aren't different technologies, they're different **scopes**: short-term is thread-scoped, long-term is user/agent-scoped. Same retrieval pattern from Day 9, different corpus.
- The hard part was never storage — it's deciding *what* to write to long-term memory and when, without either forgetting things that matter or hoarding noise.

---

## 1. Where This Fits

By Day 10 the stack looked like:

```
Chunking (6) → Vector DB (9) → LangGraph agent (7) → Tool calling (10)
```

Memory is the layer that makes an agent feel like it "knows you" instead of resetting every conversation:

```
Short-term: LangGraph state/checkpointer → survives within a thread
Long-term:  Vector DB (same tech as Day 9) → survives across threads
```

Concretely: short-term memory is *what did the user just say*. Long-term memory is *what do I know about this user from every conversation ever*. Both get injected into the same context window at generation time — the agent doesn't know or care which bucket a fact came from.

---

## 2. Short-Term Memory: Thread-Scoped State

In LangGraph this is just the graph's state object, made durable with a checkpointer:

```python
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

checkpointer = MemorySaver()  # in-process, dev only
graph = StateGraph(AgentState)
# ... add nodes/edges ...
app = graph.compile(checkpointer=checkpointer)

config = {"configurable": {"thread_id": "user-42-session-1"}}
app.invoke({"messages": [("user", "My name is Aman")]}, config)
app.invoke({"messages": [("user", "What's my name?")]}, config)  # remembers, same thread_id
```

For production, swap `MemorySaver` for `PostgresSaver` or `SqliteSaver` — same interface, persists to disk instead of process memory.

**The real problem with short-term memory isn't storage, it's growth.** Every turn appends to `messages`, and context windows aren't infinite. Two common fixes:

```python
# 1. Sliding window — keep only the last N messages
def trim_state(state):
    return {"messages": state["messages"][-20:]}

# 2. Summarization — collapse old turns into a running summary
def summarize_node(state):
    old_messages = state["messages"][:-10]
    summary = llm.invoke(f"Summarize this conversation concisely: {old_messages}")
    return {"messages": state["messages"][-10:], "summary": summary.content}
```

Summarization is lossier but scales indefinitely. Sliding window is lossless within the window but hard-forgets everything before it.

---

## 3. Long-Term Memory: User/Agent-Scoped

This is Day 9's retrieval pattern, just pointed at a "memories" collection instead of a document corpus:

```python
import chromadb

client = chromadb.Client()
memory_store = client.create_collection(name="user-memories")

def write_memory(user_id, fact):
    memory_store.add(
        documents=[fact],
        metadatas=[{"user_id": user_id, "timestamp": time.time()}],
        ids=[f"mem-{uuid.uuid4()}"]
    )

def retrieve_memories(user_id, query, k=5):
    results = memory_store.query(
        query_texts=[query],
        n_results=k,
        where={"user_id": user_id}  # scope to this user only — critical
    )
    return results["documents"][0]
```

That `where={"user_id": user_id}` filter is the whole ballgame. Skip it and you get memory leakage across users — agent A tells user B what user A told it. This is the same metadata-filtering pattern from Day 9's Pinecone/pgvector examples, just now load-bearing for privacy, not just relevance.

---

## 4. Deciding What to Write (the actual hard part)

Naively, you could write every message to long-term memory. In practice that's noise: "ok thanks" and "what's my name" don't need to be permanent facts. Most production agents run a separate **extraction step** — an LLM call whose only job is to decide if the turn contains something worth remembering:

```python
EXTRACTION_PROMPT = """
Given this conversation turn, extract any durable facts about the user worth
remembering long-term (preferences, identity, ongoing projects, goals).
Ignore small talk, one-off requests, and anything already likely known.
Return JSON: {"should_remember": bool, "facts": [str, ...]}
"""

def memory_extraction_node(state):
    result = llm.invoke(EXTRACTION_PROMPT + state["messages"][-1].content)
    parsed = json.loads(result.content)
    if parsed["should_remember"]:
        for fact in parsed["facts"]:
            write_memory(state["user_id"], fact)
    return state
```

This runs as a side-effect node in the LangGraph graph, in parallel with the main response — it doesn't block the user-facing answer.

---

## 5. Failure Modes

| Failure mode | Cause | Fix |
|---|---|---|
| Memory leakage across users | Missing `user_id` filter on retrieval | Always scope reads/writes by user/tenant ID, never a global collection |
| Stale/contradicted facts | Old memory never updated when the fact changes | Store `timestamp`, prefer most recent on conflict, or explicitly overwrite on new extraction |
| Context bloat | Retrieving too many memories per turn | Cap `k`, rank by relevance + recency, not just similarity |
| Noise drowning signal | Writing every message instead of extracted facts | Separate extraction step (Section 4), not raw message logging |
| Unbounded short-term growth | No trimming/summarization on long threads | Sliding window or summarization node (Section 2) |
| Silent memory that shapes behavior badly | Storing something like a stated preference that turns out to be a one-off or reflects a bad pattern | Treat memory writes as data, not instructions — an agent should still be able to push back even when memory suggests otherwise |

---

## 6. Full Pipeline (Tying Days 6, 7, 9, 10, 11 Together)

```python
def agent_turn(state):
    # Day 11: pull relevant long-term memories for this user
    memories = retrieve_memories(state["user_id"], state["messages"][-1].content)

    # Day 9: pull relevant document context (RAG)
    doc_context = retrieve_node(state)["retrieved_context"]

    # Day 10: model decides whether to call a tool, using both context sources
    response = llm.invoke(
        tools=tools,
        messages=state["messages"] + [
            {"role": "system", "content": f"Known about user: {memories}\nRelevant docs: {doc_context}"}
        ]
    )
    return {"messages": state["messages"] + [response]}

# Day 11 side-effect: extract new memories after the turn, don't block on it
def after_turn(state):
    memory_extraction_node(state)
    return state
```

Short-term state (thread) and long-term memory (vector DB, user-scoped) both feed the same prompt — the model doesn't know which layer a given piece of context came from, and it doesn't need to.

---

## Key Takeaways

1. Short-term vs long-term memory is a scoping question (thread vs user), not a different technology — both ultimately reduce to "what goes in the context window this turn."
2. Long-term memory is RAG with a different corpus: same embed → store → retrieve loop from Day 9, now applied to extracted facts about the user instead of documents.
3. The interesting engineering problem is the extraction step — deciding what's worth remembering — not the storage layer.
4. User-scoped filtering on every read/write isn't optional; it's the difference between "personalization" and a privacy bug.
5. Unbounded growth is the failure mode in both layers: trim/summarize short-term, rank/cap long-term retrieval.

---

## Links & Resources

- [LangGraph Persistence & Checkpointers](https://langchain-ai.github.io/langgraph/concepts/persistence/)
- [LangGraph Memory concepts](https://langchain-ai.github.io/langgraph/concepts/memory/)
- [Chroma Docs](https://docs.trychroma.com/)
- [MemGPT paper — Packer et al., 2023 (self-editing agent memory)](https://arxiv.org/abs/2310.08560)

---

## Next Steps / Reflections

- [ ] Add the extraction node to the Day 7 LangGraph agent as a real side-effect branch, not just pseudocode
- [ ] Test memory leakage directly — spin up two fake `user_id`s, confirm retrieval never crosses
- [ ] Try recency-weighted re-ranking on top of similarity for long-term memory retrieval (plain cosine similarity alone tends to resurface old, stale facts)
- [ ] Benchmark summarization vs sliding-window short-term memory on a long synthetic conversation
