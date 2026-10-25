"""Live endpoint tests. Run with XEARCH_RUN_INTEGRATION=1 pytest -m integration."""

from __future__ import annotations

import os

import pytest

from xearch_mcp.errors import SearchBlocked
from xearch_mcp.search import get_user, get_user_tweets, search_tweets
from xearch_mcp.syndication import get_tweet

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("XEARCH_RUN_INTEGRATION") != "1",
        reason="set XEARCH_RUN_INTEGRATION=1 to run live tests",
    ),
]


async def test_live_get_tweet() -> None:
    tweet = await get_tweet("1925502601757372656", resolve_links=False)
    assert tweet["author"]["username"] == "seanhn"
    assert "CVE-2025-37899" in tweet["text"]


async def test_live_get_note_tweet() -> None:
    tweet = await get_tweet("2113328686342853058", resolve_links=False)
    assert tweet["truncated"] is False
    assert len(tweet["text"]) > 400


async def test_live_profile() -> None:
    profile = await get_user("seanhn")
    assert profile["username"] == "seanhn"
    assert profile["name"] == "Sean Heelan"


async def test_live_search() -> None:
    try:
        result = await search_tweets("linux smb cve", limit=3, resolve_links=False)
    except SearchBlocked as exc:
        pytest.skip(f"search backends are rate-limiting this IP: {exc}")
    assert result["count"] >= 1
    assert result["tweets"][0]["text"]


async def test_live_user_tweets() -> None:
    try:
        result = await get_user_tweets("seanhn", limit=3, resolve_links=False)
    except SearchBlocked as exc:
        pytest.skip(f"search backends are rate-limiting this IP: {exc}")
    assert result["count"] >= 1
    assert all(t["author"]["username"] == "seanhn" for t in result["tweets"] if t.get("hydrated"))
