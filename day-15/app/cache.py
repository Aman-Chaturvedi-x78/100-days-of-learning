"""
Redis helper functions for cache and sharded counters.
"""
import os
import hashlib
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

SHARD_COUNT = int(os.getenv("CLICK_SHARDS", "4"))


def cache_get(short_id):
    key = f"url:{short_id}"
    return redis_client.get(key)


def cache_set(short_id, original_url, ttl=None):
    key = f"url:{short_id}"
    if ttl:
        redis_client.setex(key, int(ttl), original_url)
    else:
        redis_client.set(key, original_url)


def increment_clicks(short_id):
    # simple sharded counter
    h = int(hashlib.sha256(short_id.encode()).hexdigest(), 16)
    shard = h % SHARD_COUNT
    key = f"clicks_shard:{short_id}:{shard}"
    return redis_client.incr(key)


def get_clicks(short_id):
    total = 0
    for shard in range(SHARD_COUNT):
        v = redis_client.get(f"clicks_shard:{short_id}:{shard}")
        if v:
            total += int(v)
    return total
