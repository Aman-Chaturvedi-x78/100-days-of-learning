---
date: 2026-07-27
day: 09
title: "Vector Databases: FAISS vs Pinecone vs Chroma vs pgvector"
tags: [vector-db, rag, embeddings, faiss, pinecone, chroma, pgvector]
---

TL;DR
- **FAISS**: Local, in-process, blazing fast for small-to-mid scale. No server, no bill, but no built-in persistence/metadata filtering out of the box.
- **Pinecone**: Fully managed SaaS. Scales to billions of vectors, handles metadata filtering and namespaces well. Costs money and adds network latency.
- **Chroma**: Lightweight, embeddable, great developer experience for prototyping RAG apps locally before committing to infra.
- **pgvector**: A Postgres extension — best choice if you're already running Postgres and don't want a second database system.
- Picking a vector DB is really about where you are on the prototype → production curve, not which one is "best."

---

## 1. Where This Fits (Connecting Day 6 + Day 7)

Day 6 covered chunking strategies (fixed-size / semantic / sliding window). Day 7 covered LangGraph for orchestrating multi-step agent logic. Vector databases are the missing piece that connects them:

```
Raw Document → [Chunking: Day 6] → Embeddings → [Vector DB: Day 9] → Retrieval → [LangGraph Agent: Day 7]
```

Once a document is chunked, each chunk gets embedded into a vector, and that vector needs to live *somewhere* it can be searched quickly. That's the vector DB's job: given a query embedding, return the `k` most similar stored vectors fast, even across millions of them.

---

## 2. Core Concepts

### Similarity Metrics
```python
import numpy as np

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def euclidean_distance(a, b):
    return np.linalg.norm(a - b)

def dot_product(a, b):
    return np.dot(a, b)
```
- **Cosine similarity**: most common for text embeddings — measures angle, ignores magnitude.
- **Euclidean (L2)**: sensitive to magnitude, used less often for normalized text embeddings.
- **Dot product**: fast, equivalent to cosine if vectors are pre-normalized.

### Why Not Just Brute-Force Search?
Brute force (compare query to every stored vector) is O(n) per query. At millions of vectors, that's too slow for real-time RAG. Vector DBs use **Approximate Nearest Neighbor (ANN)** indexing to trade a small amount of accuracy for large speed gains.

Common ANN algorithms:
- **HNSW** (Hierarchical Navigable Small World) — graph-based, very fast queries, higher memory use. Used by Pinecone, Chroma, Weaviate.
- **IVF** (Inverted File Index) — clusters vectors, searches only relevant clusters. Used heavily in FAISS.

---

## 3. FAISS (Local, In-Process)

```bash
pip install faiss-cpu
```

```python
import faiss
import numpy as np

dimension = 384  # matches embedding model output size
index = faiss.IndexFlatL2(dimension)  # exact search, good for <1M vectors

# Add vectors
embeddings = np.random.random((1000, dimension)).astype("float32")
index.add(embeddings)

# Query
query = np.random.random((1, dimension)).astype("float32")
k = 5
distances, indices = index.search(query, k)
print("Nearest neighbor indices:", indices)
```

For larger datasets, swap `IndexFlatL2` for an ANN index:
```python
nlist = 100  # number of clusters
quantizer = faiss.IndexFlatL2(dimension)
index = faiss.IndexIVFFlat(quantizer, dimension, nlist)
index.train(embeddings)
index.add(embeddings)
index.nprobe = 10  # clusters to search — tune for speed/accuracy tradeoff
```

**Pros**: No server, no cost, extremely fast, full control.
**Cons**: No native metadata filtering, no persistence layer (you build that yourself), single-machine by default.

---

## 4. Pinecone (Managed SaaS)

```bash
pip install pinecone-client
```

```python
from pinecone import Pinecone, ServerlessSpec

pc = Pinecone(api_key="YOUR_API_KEY")

pc.create_index(
    name="rag-index",
    dimension=384,
    metric="cosine",
    spec=ServerlessSpec(cloud="aws", region="us-east-1")
)

index = pc.Index("rag-index")

# Upsert vectors with metadata
index.upsert(vectors=[
    {"id": "chunk-1", "values": [0.1] * 384, "metadata": {"source": "day-06.md"}},
    {"id": "chunk-2", "values": [0.2] * 384, "metadata": {"source": "day-07.md"}},
])

# Query with metadata filter
results = index.query(
    vector=[0.1] * 384,
    top_k=3,
    filter={"source": {"$eq": "day-06.md"}},
    include_metadata=True
)
```

**Pros**: Fully managed, scales without ops work, strong metadata filtering, namespaces for multi-tenant setups.
**Cons**: Recurring cost, network round-trip latency, vendor lock-in.

---

## 5. Chroma (Lightweight, Dev-Friendly)

```bash
pip install chromadb
```

```python
import chromadb

client = chromadb.Client()
collection = client.create_collection(name="rag-collection")

collection.add(
    documents=["LangGraph models agents as state graphs.", "FAISS is a local ANN library."],
    metadatas=[{"source": "day-07.md"}, {"source": "day-09.md"}],
    ids=["chunk-1", "chunk-2"]
)

results = collection.query(
    query_texts=["How does LangGraph handle branching?"],
    n_results=2
)
print(results["documents"])
```

Chroma handles embedding generation automatically if you don't pass pre-computed vectors — useful for fast prototyping.

**Pros**: Runs in-process or as a lightweight local server, minimal setup, good for prototyping RAG before deciding on production infra.
**Cons**: Less battle-tested at very large scale compared to Pinecone or dedicated vector infra.

---

## 6. pgvector (Postgres Extension)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE chunks (
    id SERIAL PRIMARY KEY,
    content TEXT,
    source TEXT,
    embedding VECTOR(384)
);

CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
```

```sql
SELECT content, source, embedding <=> '[0.1, 0.2, ...]' AS distance
FROM chunks
ORDER BY distance
LIMIT 5;
```

**Pros**: One database for relational + vector data, no new infra if you're already on Postgres, full SQL power (joins, filters, transactions).
**Cons**: Not purpose-built for vector search at extreme scale; performance depends on Postgres tuning.

---

## 7. Comparison Table

| Feature | FAISS | Pinecone | Chroma | pgvector |
|---|---|---|---|---|
| Hosting | Local/in-process | Managed SaaS | Local or light server | Postgres extension |
| Setup complexity | Low | Low (API-based) | Very low | Medium (needs Postgres) |
| Metadata filtering | Manual | Built-in | Built-in | Full SQL |
| Persistence | Manual | Built-in | Built-in | Built-in |
| Cost | Free | Pay-per-use | Free (self-hosted) | Free (if Postgres already running) |
| Scale ceiling | High (single machine) | Very high (distributed) | Medium | Medium-high |
| Best for | Prototyping, offline batch jobs | Production RAG at scale | Local dev, small-medium apps | Teams already on Postgres |

---

## 8. Full Pipeline Example (Tying Days 6, 7, 9 Together)

```python
# Day 6: chunk the document
chunks = sliding_window_chunking(document_text, chunk_size=512, overlap=128)

# Day 9: embed and store
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(chunks)

index = faiss.IndexFlatL2(384)
index.add(np.array(embeddings).astype("float32"))

# Day 7: LangGraph node that retrieves before generating
def retrieve_node(state):
    query_embedding = model.encode([state["input"]])
    _, indices = index.search(np.array(query_embedding).astype("float32"), k=3)
    retrieved_chunks = [chunks[i] for i in indices[0]]
    return {"retrieved_context": retrieved_chunks}
```

This is the core loop behind most RAG agents: chunk once, embed once, retrieve many times, and let the graph decide what to do with what comes back (retry retrieval, ask for clarification, or generate).

---

## Key Takeaways

1. Vector DB choice is about deployment stage, not raw performance — FAISS/Chroma for prototyping, Pinecone/pgvector for production.
2. ANN indexing (HNSW, IVF) is what makes million-scale similarity search feasible.
3. Metadata filtering matters more in practice than raw search speed — most real RAG queries need `WHERE source = X AND date > Y`-style filtering alongside similarity.
4. pgvector is underrated if you're not ready to add a new system to your stack.
5. This closes the loop from Day 6 (chunking) → Day 9 (storage/retrieval) → Day 7 (agent orchestration) into one working RAG pipeline.

---

## Links & Resources

- [FAISS GitHub](https://github.com/facebookresearch/faiss)
- [Pinecone Docs](https://docs.pinecone.io/)
- [Chroma Docs](https://docs.trychroma.com/)
- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [HNSW Paper — Malkov & Yashunin](https://arxiv.org/abs/1603.09320)

---

## Next Steps / Reflections

- [ ] Benchmark FAISS vs Chroma retrieval latency on the heart disease pipeline's dataset docs (reuse as a demo RAG corpus)
- [ ] Add metadata filtering to the Day 6 chunking output so retrieval can filter by source day/topic
- [ ] Try pgvector locally since it avoids adding a new service to the stack
- [ ] Wire this retrieval node into the Day 7 LangGraph agent as a real conditional edge (retry retrieval if relevance score is low)
