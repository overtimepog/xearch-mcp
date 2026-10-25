"""Runtime configuration, all via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Xearch/0.1"
)

DEFAULT_HEALTH_TWEET_ID = "1925502601757372656"  # @seanhn, stable public post


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable settings snapshot."""

    timeout_seconds: float = 20.0
    tweet_cache_ttl: int = 600
    search_cache_ttl: int = 180
    link_cache_ttl: int = 3600
    profile_cache_ttl: int = 900
    cache_max_entries: int = 512
    max_concurrency: int = 4
    user_agent: str = DEFAULT_USER_AGENT
    enrich: bool = True
    resolve_links: bool = True
    brave_api_key: str | None = None
    search_backend: str = "auto"
    brave_country: str = "us"
    health_tweet_id: str = DEFAULT_HEALTH_TWEET_ID
    log_level: str = "WARNING"
    syndication_url: str = "https://cdn.syndication.twimg.com/tweet-result"
    fxtwitter_base: str = "https://api.fxtwitter.com"
    ddg_search_url: str = "https://html.duckduckgo.com/html/"
    brave_search_url: str = "https://search.brave.com/search"
    brave_api_url: str = "https://api.search.brave.com/res/v1/web/search"
    x_profile_url: str = "https://x.com"
    extra: dict[str, str] = field(default_factory=dict)

    def configured_search_backend(self) -> str:
        """The configured backend, normalized to lower case."""
        return self.search_backend.strip().lower() or "auto"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings once per process."""
    return Settings(
        timeout_seconds=_env_float("XEARCH_TIMEOUT", 20.0),
        tweet_cache_ttl=_env_int("XEARCH_TWEET_CACHE_TTL", 600),
        search_cache_ttl=_env_int("XEARCH_SEARCH_CACHE_TTL", 180),
        link_cache_ttl=_env_int("XEARCH_LINK_CACHE_TTL", 3600),
        profile_cache_ttl=_env_int("XEARCH_PROFILE_CACHE_TTL", 900),
        cache_max_entries=_env_int("XEARCH_CACHE_MAX_ENTRIES", 512),
        max_concurrency=max(1, _env_int("XEARCH_MAX_CONCURRENCY", 4)),
        user_agent=os.getenv("XEARCH_USER_AGENT", DEFAULT_USER_AGENT),
        enrich=_env_bool("XEARCH_ENRICH", True),
        resolve_links=_env_bool("XEARCH_RESOLVE_LINKS", True),
        brave_api_key=(os.getenv("BRAVE_API_KEY") or None),
        search_backend=os.getenv("XEARCH_SEARCH_BACKEND", "auto"),
        brave_country=os.getenv("XEARCH_BRAVE_COUNTRY", "us"),
        health_tweet_id=os.getenv("XEARCH_HEALTH_TWEET_ID", DEFAULT_HEALTH_TWEET_ID),
        log_level=os.getenv("XEARCH_LOG_LEVEL", "WARNING").upper(),
    )
