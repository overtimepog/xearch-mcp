"""A small in-memory TTL + LRU cache."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass
class CacheStats:
    entries: int
    hits: int
    misses: int
    evictions: int
    expirations: int


class TTLCache(Generic[K, V]):
    """Thread-safe enough for asyncio: operations are short and synchronous."""

    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def get(self, key: K) -> V | None:
        item = self._data.get(key)
        if item is None:
            self._misses += 1
            return None
        expires_at, value = item
        if time.monotonic() >= expires_at:
            self._data.pop(key, None)
            self._expirations += 1
            self._misses += 1
            return None
        self._data.move_to_end(key)
        self._hits += 1
        return value

    def set(self, key: K, value: V) -> None:
        if key in self._data:
            self._data.pop(key, None)
        self._data[key] = (time.monotonic() + self.ttl_seconds, value)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)
            self._evictions += 1

    def clear(self) -> None:
        self._data.clear()

    def stats(self) -> CacheStats:
        return CacheStats(
            entries=len(self._data),
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
            expirations=self._expirations,
        )


class CacheRegistry:
    """Named caches so status can report them."""

    def __init__(self) -> None:
        self._caches: dict[str, TTLCache] = {}

    def create(self, name: str, ttl_seconds: float, max_entries: int) -> TTLCache:
        cache = TTLCache(ttl_seconds, max_entries)
        self._caches[name] = cache
        return cache

    def stats(self) -> dict[str, dict[str, int]]:
        return {
            name: {
                "entries": s.entries,
                "hits": s.hits,
                "misses": s.misses,
                "evictions": s.evictions,
                "expirations": s.expirations,
            }
            for name, cache in self._caches.items()
            for s in [cache.stats()]
        }
