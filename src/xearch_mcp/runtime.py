"""Process-wide runtime: settings, HTTP client, and caches."""

from __future__ import annotations

from dataclasses import dataclass, field

from .cache import CacheRegistry, TTLCache
from .config import Settings, get_settings
from .http import AsyncRateLimiter, HttpClient


@dataclass
class Runtime:
    settings: Settings
    http: HttpClient
    registry: CacheRegistry = field(default_factory=CacheRegistry)
    tweets: TTLCache = field(init=False)
    links: TTLCache = field(init=False)
    search: TTLCache = field(init=False)
    profiles: TTLCache = field(init=False)
    ddg_limiter: AsyncRateLimiter = field(init=False)
    brave_limiter: AsyncRateLimiter = field(init=False)

    def __post_init__(self) -> None:
        max_entries = self.settings.cache_max_entries
        self.tweets = self.registry.create("tweets", self.settings.tweet_cache_ttl, max_entries)
        self.links = self.registry.create("links", self.settings.link_cache_ttl, max_entries)
        self.search = self.registry.create("search", self.settings.search_cache_ttl, max_entries)
        self.profiles = self.registry.create(
            "profiles", self.settings.profile_cache_ttl, max_entries
        )
        # Be polite to the keyless search endpoints.
        self.ddg_limiter = AsyncRateLimiter(1.0)
        self.brave_limiter = AsyncRateLimiter(1.0)


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(settings=get_settings(), http=HttpClient(get_settings()))
    return _runtime


async def aclose_runtime() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.http.close()
        _runtime = None


def reset_runtime() -> None:
    """Drop the singleton without closing the client (for tests)."""
    global _runtime
    _runtime = None
