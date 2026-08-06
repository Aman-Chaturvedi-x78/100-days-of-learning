# System Design: Scalable URL Shortener

Date: 2026-08-06

Summary
- Goal: Design a highly available, low-latency URL shortening service that supports custom aliases, analytics, and handles large scale traffic.

Requirements
- Functional
  - Create short URLs for long URLs.
  - Redirect short URLs to original long URLs.
  - Support custom aliases and TTL for links.
  - Provide basic analytics: click counts, created_at, last_accessed.
- Non-functional
  - Handle 10k QPS reads and 1k QPS writes.
  - Redirect latency < 100ms p95.
  - High availability and horizontal scalability.
  - Reasonable cost and operational simplicity.

Capacity & Traffic Estimates
- Stored URLs: 100M primary mappings.
- Traffic: avg 10k QPS reads, 1k QPS writes.
- Data size: average original URL 200 bytes -> ~20GB raw (plus indexes/metadata).

API Design
- POST /shorten
  - Request: { url, custom_alias?, ttl?, owner_id? }
  - Response: { short_id, short_url }
- GET /r/{short_id}
  - Behavior: 302 redirect to original_url
- GET /stats/{short_id}
  - Response: { clicks, created_at, last_accessed, owner_id }

Data Model
- Primary mapping (Key-Value):
  - Key: short_id
  - Value: { original_url, created_at, owner_id, ttl }
- Metadata/Analytics:
  - Click counters (sharded counters or separate counter store)
  - Events: write click events (short_id, timestamp, client_ip, user_agent) to streaming system

Storage choices
- Use a horizontally scalable KV store (e.g., DynamoDB, Cassandra, Scylla) for primary mappings.
- Use Redis (cluster) as a hot-cache for very popular short_ids.
- Use cold storage / blob for archival if full history required.

ID generation
- Option A: Base62 encode a monotonically-increasing 64-bit ID (Snowflake-like generator) per region to generate short_ids.
- Option B: Hash approach + collision resolution (use random suffixes).
- Custom aliases validated for uniqueness and strong consistency (use conditional write/compare-and-set).

High-level Architecture
Clients -> CDN / LB -> API Gateway -> API Servers (stateless) ->
  - Cache (Redis)
  - Primary KV store (Cassandra/DynamoDB)
  - Streaming (Kafka) -> Analytics pipeline -> Time-series DB / Warehouse

Detailed behavior
- Shorten flow:
  1. Client calls POST /shorten.
  2. API validates URL, checks custom_alias.
  3. Generate short_id (via ID service) or use custom_alias.
  4. Write mapping to primary KV store (conditional write for custom aliases).
  5. Return short_url. Optionally update cache.
- Redirect flow:
  1. GET /r/{short_id} hits CDN or LB.
  2. CDN edge can perform lookup if using edge-KV (Cloudflare Workers KV) or forward to API servers.
  3. API server checks Redis cache -> if miss, read from KV store -> serve 302.
  4. Emit click event to Kafka asynchronously; increment click counter (sharded increments).

Scaling & Bottlenecks
- Hot keys (very popular short_ids): use edge-caching, TTL, and cache pre-warming; use sharded counters for click increments.
- Storage growth: partition data by short_id hash; use TTL for expiring links.
- ID generation throttling: distribute ID generation across multiple nodes/shards.
- Analytics throughput: use partitioned Kafka topics and stream processors; downsample or aggregate for long-term storage.

Consistency & Availability Tradeoffs
- Use strong consistency for custom alias creation (conditional writes), eventual consistency for counters and analytics.
- Multi-region replication for read availability; writes can be region-local with async cross-region replication.

Operational Concerns
- Security: rate-limiting per IP/API key, spam/malware URL scanning (using third-party APIs), abuse detection.
- Monitoring: track latency P50/P95/P99, error rates, cache hit ratio, DB latency, queue lag.
- Backups & retention: periodic snapshot of mapping DB; retention policy for analytics.

Optimizations
- Use Bloom filters to reject non-existent short_ids quickly before cache/DB.
- Use TTL and compact storage for old/unused URLs.
- Use CDN edge functions for fastest redirects when feasible.

Tradeoffs
- Simplicity vs latency: using a single-region KV store is simpler but increases latency for global users.
- Cost vs consistency: strongly-consistent databases cost more; eventual consistency cheaper for analytics.

Checklist before an interview demo
- Prepare capacity numbers & calculations.
- Explain why chosen DB and ID generation approach.
- Be ready to discuss hot-key mitigation and monitoring.
- Sketch a simple diagram showing components and request flow.

Notes / Further reading
- Snowflake ID generation pattern
- Bloom filters for membership checks
- Sharded counters and Redis best practices

---
If you want a different system-design topic (e.g., chat service, notification pipeline) or a different filename (day number), tell me which and I can add or update the file.