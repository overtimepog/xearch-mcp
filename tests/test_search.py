"""Search query, parser, and filter tests."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from xearch_mcp.config import Settings
from xearch_mcp.errors import InvalidInput, SearchBlocked, SearchError
from xearch_mcp.search import (
    SearchHit,
    _decode_ddg_redirect,
    _parallel_fallbacks,
    apply_filters,
    backend_order,
    build_query,
    fetch_search_page,
    parse_brave_html,
    parse_ddg_html,
    parse_exa_results,
    parse_mcp_body,
    parse_parallel_results,
    search_tweets,
)


def test_parse_ddg_results(fixture_text) -> None:
    hits = parse_ddg_html(fixture_text("ddg_results.html"))
    assert len(hits) == 2
    assert hits[0].tweet_id == "2113342426584625378"
    assert hits[0].username == "DarkWebInformer"
    assert hits[0].title.startswith("Dark Web Informer on X")
    assert hits[0].snippet
    assert all("/status/" in hit.url for hit in hits)


def test_parse_brave_results(fixture_text) -> None:
    hits = parse_brave_html(fixture_text("brave_results.html"))
    assert len(hits) == 3
    assert {hit.username for hit in hits} == {"DarkWebInformer", "BlockTempo", "rohanpaul_ai"}
    assert all(hit.tweet_id.isdigit() for hit in hits)
    assert all(hit.title for hit in hits)


def test_parse_parallel_results(fixture_json) -> None:
    hits = parse_parallel_results(fixture_json("parallel_results.json"))
    assert len(hits) == 2
    assert hits[0].tweet_id == "2113315655512731872"
    assert hits[0].username == "TheHackersNews"
    assert "KSMBD" in hits[0].snippet
    assert all(hit.url.startswith("https://x.com/") for hit in hits)


def test_parse_exa_results_from_sse(fixture_text) -> None:
    payload = parse_mcp_body(fixture_text("exa_results.sse"))
    hits = parse_exa_results(payload)
    assert len(hits) == 2
    assert {hit.username for hit in hits} == {"TheHackersNews", "DarkWebInformer"}
    assert all("/status/" in hit.url for hit in hits)
    assert hits[0].title == "The Hacker News on X"


def test_mcp_query_rewrites_profile_scope() -> None:
    from xearch_mcp.search import _mcp_query

    assert _mcp_query("site:x.com/seanhn") == "site:x.com seanhn posts"
    assert _mcp_query("site:x.com/seanhn o3") == "site:x.com seanhn o3"
    assert _mcp_query("site:x.com kernel") == "site:x.com kernel"


def test_parallel_fallbacks_drop_site_scope() -> None:
    assert _parallel_fallbacks("site:x.com php2shell") == [
        "php2shell x.com",
        "php2shell twitter",
    ]
    assert _parallel_fallbacks("site:x.com seanhn o3") == [
        "seanhn o3 x.com",
        "seanhn o3 twitter",
    ]
    assert _parallel_fallbacks("site:x.com") == []


def test_fetch_search_page_returns_empty_when_backends_have_no_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xearch_mcp.search as search

    async def empty(*args, **kwargs):
        return []

    async def fail(*args, **kwargs):
        raise SearchError("blocked")

    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("XEARCH_SEARCH_BACKEND", "auto")
    monkeypatch.setattr(search, "_search_parallel", empty)
    monkeypatch.setattr(search, "_search_exa", empty)
    monkeypatch.setattr(search, "_search_ddg_html", fail)
    monkeypatch.setattr(search, "_search_brave_html", fail)

    hits, backend = asyncio.run(
        fetch_search_page("site:x.com php2shell", page=1, freshness="any", lang=None)
    )
    assert hits == []
    assert backend == "parallel"


def test_fetch_search_page_raises_when_every_backend_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xearch_mcp.search as search

    async def fail(*args, **kwargs):
        raise SearchError("rate limited")

    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("XEARCH_SEARCH_BACKEND", "auto")
    monkeypatch.setattr(search, "_search_parallel", fail)
    monkeypatch.setattr(search, "_search_exa", fail)
    monkeypatch.setattr(search, "_search_ddg_html", fail)
    monkeypatch.setattr(search, "_search_brave_html", fail)

    with pytest.raises(SearchBlocked):
        asyncio.run(
            fetch_search_page("site:x.com kernel", page=1, freshness="any", lang=None)
        )


def test_search_tweets_zero_results_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xearch_mcp.search as search

    async def empty(*args, **kwargs):
        return [], "parallel"

    monkeypatch.setattr(search, "fetch_search_page", empty)
    result = asyncio.run(search.search_tweets("php2shell", limit=3, hydrate=False))
    assert result["count"] == 0
    assert result["backend"] == "parallel"
    assert any("No matching X posts" in note for note in result["notes"])


def test_search_tweets_notes_broadened_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xearch_mcp.search as search

    hit = SearchHit(
        tweet_id="123",
        url="https://x.com/user/status/123",
        username="user",
        title="title",
        snippet="snippet",
        broadened=True,
    )

    async def broadened(*args, **kwargs):
        return [hit], "parallel"

    monkeypatch.setattr(search, "fetch_search_page", broadened)
    result = asyncio.run(search.search_tweets("php2shell", limit=3, hydrate=False))
    assert result["count"] == 1
    assert any("broadened search" in note for note in result["notes"])


def test_get_user_tweets_zero_results_has_user_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xearch_mcp.search as search

    async def empty(*args, **kwargs):
        return {"count": 0, "notes": []}

    monkeypatch.setattr(search, "search_tweets", empty)
    result = asyncio.run(search.get_user_tweets("syrex1013", limit=3))
    assert result["username"] == "syrex1013"
    assert any("No indexed posts" in note for note in result["notes"])


@pytest.mark.parametrize("parser", [parse_ddg_html, parse_brave_html])
def test_parsers_detect_challenges(parser, fixture_text) -> None:
    with pytest.raises(SearchBlocked):
        parser(fixture_text("blocked.html"))


def test_decode_ddg_redirect() -> None:
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fuser%2Fstatus%2F123%3Fs%3D20&rut=abc"
    assert _decode_ddg_redirect(wrapped) == "https://x.com/user/status/123?s=20"
    assert _decode_ddg_redirect("https://x.com/user/status/123") == (
        "https://x.com/user/status/123"
    )


def test_build_query_plain() -> None:
    plan = build_query("linux smb cve")
    assert plan.effective == "site:x.com linux smb cve"
    assert plan.filters["authors"] == []
    assert plan.filters["lang"] is None


def test_build_query_from_operator() -> None:
    plan = build_query("from:seanhn o3")
    assert plan.effective == "site:x.com/seanhn o3"
    assert plan.filters["authors"] == ["seanhn"]


def test_build_query_from_username_argument() -> None:
    plan = build_query("from:seanhn", username="seanhn")
    assert plan.effective == "site:x.com/seanhn"
    assert plan.filters["authors"] == ["seanhn"]


def test_build_query_multiple_authors() -> None:
    plan = build_query("from:user1 from:user2 kernel")
    assert plan.effective == "site:x.com kernel"
    assert plan.filters["authors"] == ["user1", "user2"]
    assert any("multiple" in note.lower() for note in plan.notes)


def test_build_query_filters() -> None:
    plan = build_query(
        "kernel since:2025-01-02 until:2025-02-03 lang:en "
        "filter:media filter:links min_faves:10 min_retweets:2"
    )
    assert plan.effective == "site:x.com kernel"
    assert plan.filters == {
        "authors": [],
        "since": "2025-01-02",
        "until": "2025-02-03",
        "lang": "en",
        "require_media": True,
        "require_links": True,
        "min_likes": 10,
        "min_retweets": 2,
    }


def test_build_query_invalid_date_is_ignored() -> None:
    plan = build_query("kernel since:not-a-date")
    assert plan.effective == "site:x.com kernel"
    assert plan.filters["since"] is None
    assert any("invalid since" in note.lower() for note in plan.notes)


def test_build_query_rejects_empty() -> None:
    with pytest.raises(InvalidInput):
        build_query("  ")


def test_apply_filters_author_and_dates() -> None:
    tweet = {
        "author": {"username": "seanhn"},
        "created_at": "2025-05-22T10:42:49.000Z",
        "lang": "en",
        "media": [],
        "links": [],
        "metrics": {"likes": 5, "retweets": 1},
    }
    assert apply_filters(
        tweet,
        {
            "authors": ["seanhn"],
            "since": "2025-01-01",
            "until": "2025-12-31",
            "lang": "en",
            "require_media": False,
            "require_links": False,
            "min_likes": 5,
            "min_retweets": 1,
        },
    ) == (True, None)
    keep, reason = apply_filters(tweet, {"authors": ["someone_else"]})
    assert not keep and reason == "author"
    keep, reason = apply_filters(tweet, {"since": "2025-06-01"})
    assert not keep and reason == "since"
    keep, reason = apply_filters(tweet, {"require_media": True})
    assert not keep and reason == "media"
    keep, reason = apply_filters(tweet, {"min_likes": 6})
    assert not keep and reason == "min_likes"


def test_apply_filters_links_accepts_card() -> None:
    tweet = {
        "author": {"username": "a"},
        "created_at": "2025-01-01T00:00:00.000Z",
        "links": [],
        "card": {"url": "https://t.co/x", "expanded_url": None},
        "metrics": {},
    }
    keep, _ = apply_filters(tweet, {"require_links": True})
    assert keep


def test_backend_order_auto_without_key() -> None:
    settings = Settings(search_backend="auto")
    assert backend_order(settings) == ["parallel", "exa", "ddg", "brave"]


def test_backend_order_auto_with_key() -> None:
    settings = Settings(search_backend="auto", brave_api_key="key")
    assert backend_order(settings) == ["parallel", "exa", "brave-api", "ddg", "brave"]


@pytest.mark.parametrize("backend", ["parallel", "exa", "ddg", "brave", "brave-api"])
def test_backend_order_explicit(backend: str) -> None:
    settings = Settings(search_backend=backend)
    assert backend_order(settings) == [backend]


def test_backend_order_invalid() -> None:
    from xearch_mcp.errors import SearchConfigError

    with pytest.raises(SearchConfigError):
        backend_order(Settings(search_backend="google"))


def test_search_tweets_validates_limit_and_page() -> None:
    import asyncio

    with pytest.raises(InvalidInput):
        asyncio.run(search_tweets("kernel", limit=0))
    with pytest.raises(InvalidInput):
        asyncio.run(search_tweets("kernel", page=10))
    with pytest.raises(InvalidInput):
        asyncio.run(search_tweets("kernel", sort="newest"))  # type: ignore[arg-type]


def test_created_date_parse() -> None:
    from xearch_mcp.search import _created_date

    assert _created_date("2025-05-22T10:42:49.000Z") == date(2025, 5, 22)
    assert _created_date("") is None
