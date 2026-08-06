# Day 15 — URL Shortener Demo (FastAPI)

This folder contains a small, production-like demo of the system design written during Day 15. It implements:

- FastAPI service exposing:
  - POST /shorten — create a short URL (supports custom aliases and ttl)
  - GET /r/{short_id} — redirect to original URL (cache-first, DB fallback)
  - GET /stats/{short_id} — basic stats: clicks, created_at, last_accessed
- PostgreSQL (primary mapping store)
- Redis (cache + counters)
- Snowflake-like ID generator (Base62 encoded)
- Simple event emitter (writes events to a local file in the container for demo)

This is intended as a demo for interviews or local experimentation — it is not hardened for production use.

Quick start (Docker Compose)

1. Copy .env.example to .env and adjust values if needed.
2. docker-compose up --build
3. The API will be available at http://localhost:8000

Example requests

- Create a short URL
  POST http://localhost:8000/shorten
  JSON body: { "url": "https://example.com/long/path", "custom_alias": "exmpl" }

- Redirect
  GET http://localhost:8000/r/exmpl

- Stats
  GET http://localhost:8000/stats/exmpl

Notes

- The demo uses sync SQLAlchemy for simplicity — swapping to async engines is straightforward.
- Click events are appended to /var/log/shortener/events.log inside the container for demo inspection.
