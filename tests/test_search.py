"""Search query, parser, and filter tests."""

from __future__ import annotations

from datetime import date

import pytest

from xearch_mcp.config import Settings
from xearch_mcp.errors import InvalidInput, SearchBlocked
from xearch_mcp.search import (
    _decode_ddg_redirect,
    apply_filters,
    backend_order,
    build_query,
    parse_brave_html,
    parse_ddg_html,
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
    assert backend_order(settings) == ["ddg", "brave"]


def test_backend_order_auto_with_key() -> None:
    settings = Settings(search_backend="auto", brave_api_key="key")
    assert backend_order(settings) == ["brave-api", "ddg", "brave"]


@pytest.mark.parametrize("backend", ["ddg", "brave", "brave-api"])
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
