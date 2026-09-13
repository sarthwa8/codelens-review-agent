from functools import lru_cache

import redis
import redis.asyncio as aioredis

from app.config import get_settings


@lru_cache
def get_async_redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


@lru_cache
def get_sync_redis() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)
