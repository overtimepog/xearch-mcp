"""Tweet parsing and normalization tests."""

from __future__ import annotations

import asyncio

import pytest

import xearch_mcp.syndication as syn
from xearch_mcp.errors import InvalidInput
from xearch_mcp.syndication import _is_truncated, normalize_tweet, resolve_tweet_input


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1925502601757372656", "1925502601757372656"),
        ("https://x.com/seanhn/status/1925502601757372656?s=20", "1925502601757372656"),
        ("https://twitter.com/foo/status/123", "123"),
        ("https://mobile.twitter.com/foo/status/123", "123"),
        ("https://x.com/i/web/status/123", "123"),
        ("x.com/foo/status/123", "123"),
    ],
)
def test_resolve_tweet_input(value: str, expected: str) -> None:
    assert asyncio.run(resolve_tweet_input(value)) == expected


@pytest.mark.parametrize(
    "value",
    ["", "hello", "https://x.com/seanhn", "https://example.com/foo/status/123"],
)
def test_resolve_tweet_input_rejects_invalid(value: str) -> None:
    with pytest.raises(InvalidInput):
        asyncio.run(resolve_tweet_input(value))


def test_is_truncated_plain_text() -> None:
    assert not _is_truncated({"text": "hello", "display_text_range": [0, 5]})
    assert _is_truncated({"text": "hello", "display_text_range": [0, 9]})


def test_is_truncated_counts_utf16_units() -> None:
    # An emoji is two UTF-16 code units; Twitter's range reflects that.
    assert not _is_truncated({"text": "ok 😀", "display_text_range": [0, 5]})
    assert _is_truncated({"text": "ok 😀", "display_text_range": [0, 6]})


def test_normalize_short_tweet(fixture_json) -> None:
    tweet = asyncio.run(
        normalize_tweet(
            fixture_json("tweet_short.json"),
            resolve_links=False,
            enrich=False,
        )
    )
    assert tweet["id"] == "1925502601757372656"
    assert tweet["author"]["username"] == "seanhn"
    assert tweet["author"]["blue_verified"] is True
    assert tweet["text"].startswith("I wrote-up how I used o3")
    assert tweet["truncated"] is False
    assert tweet["is_note_tweet"] is False
    assert tweet["metrics"]["likes"] == 897
    assert tweet["metrics"]["replies"] == 22
    assert tweet["media"] == []
    assert tweet["card"] is None
    assert tweet["quoted_tweet"] is None
    assert tweet["url"] == "https://x.com/seanhn/status/1925502601757372656"


def test_normalize_note_tweet_without_enrichment(fixture_json) -> None:
    tweet = asyncio.run(
        normalize_tweet(
            fixture_json("tweet_note.json"),
            resolve_links=False,
            enrich=False,
        )
    )
    assert tweet["truncated"] is True
    assert tweet["is_note_tweet"] is True
    assert len(tweet["text"]) == 171
    assert any("truncated" in note.lower() for note in tweet["notes"])


def test_normalize_note_tweet_with_fxtwitter(fixture_json, monkeypatch) -> None:
    fx = fixture_json("fxtwitter_note.json")["tweet"]

    async def fake_fx(tweet_id: str):
        assert tweet_id == "2113328686342853058"
        return fx

    monkeypatch.setattr(syn, "fetch_fx_tweet", fake_fx)
    tweet = asyncio.run(
        normalize_tweet(
            fixture_json("tweet_note.json"),
            include_quoted=False,
            resolve_links=False,
            enrich=True,
        )
    )
    assert tweet["truncated"] is False
    assert tweet["text_source"] == "fxtwitter"
    assert tweet["source"] == "syndication+fxtwitter"
    assert len(tweet["text"]) == 542
    assert tweet["metrics"]["views"] > 0
    assert "notes" not in tweet


def test_normalize_media_tweet(fixture_json, monkeypatch) -> None:
    fx = fixture_json("fxtwitter_media.json")["tweet"]

    async def fake_fx(tweet_id: str):
        return fx

    monkeypatch.setattr(syn, "fetch_fx_tweet", fake_fx)
    tweet = asyncio.run(
        normalize_tweet(
            fixture_json("tweet_media.json"),
            include_quoted=False,
            resolve_links=False,
            enrich=True,
        )
    )
    assert tweet["truncated"] is False
    assert tweet["text_source"] == "fxtwitter"
    assert len(tweet["text"]) == 1645
    assert tweet["media"][0]["type"] == "photo"
    assert tweet["media"][0]["url"].startswith("https://pbs.twimg.com/media/")
    assert tweet["media"][0]["expanded_url"].endswith("/photo/1")
    assert tweet["metrics"]["views"] == 2523


def test_normalize_card_tweet(fixture_json) -> None:
    tweet = asyncio.run(
        normalize_tweet(
            fixture_json("tweet_card.json"),
            resolve_links=False,
            enrich=False,
            include_raw=True,
        )
    )
    card = tweet["card"]
    assert card is not None
    assert card["name"] == "summary_large_image"
    assert card["title"].startswith("Linux Kernel SMB")
    assert card["domain"]
    assert card["url"].startswith("https://t.co/")
    assert card["expanded_url"] is None
    assert tweet["links"] == []
    assert "raw" in tweet


def test_normalize_quoted_tweet(fixture_json) -> None:
    raw = fixture_json("tweet_quote.json")
    with_quote = asyncio.run(
        normalize_tweet(raw, include_quoted=True, resolve_links=False, enrich=False)
    )
    assert with_quote["quoted_tweet"]["id"] == "2105240703383027897"

    without_quote = asyncio.run(
        normalize_tweet(raw, include_quoted=False, resolve_links=False, enrich=False)
    )
    assert without_quote["quoted_tweet"] is None


def test_normalize_resolves_entities_without_network() -> None:
    raw = {
        "id_str": "1",
        "text": "hello https://t.co/abc",
        "display_text_range": [0, 18],
        "user": {"screen_name": "tester", "name": "Tester"},
        "entities": {
            "urls": [{"url": "https://t.co/abc", "expanded_url": "https://example.com/x"}]
        },
    }
    tweet = asyncio.run(normalize_tweet(raw, resolve_links=True, enrich=False))
    assert tweet["links"] == [
        {"short_url": "https://t.co/abc", "url": "https://example.com/x", "resolved": True}
    ]
    assert tweet["text_expanded"] == "hello https://example.com/x"


def test_normalize_excludes_media_links_from_external_links() -> None:
    raw = {
        "id_str": "2",
        "text": "pic https://t.co/media",
        "display_text_range": [0, 18],
        "user": {"screen_name": "tester", "name": "Tester"},
        "mediaDetails": [
            {
                "type": "photo",
                "url": "https://t.co/media",
                "media_url_https": "https://pbs.twimg.com/media/x.jpg",
                "expanded_url": "https://x.com/tester/status/2/photo/1",
            }
        ],
    }
    tweet = asyncio.run(normalize_tweet(raw, resolve_links=True, enrich=False))
    assert tweet["links"] == []
    assert tweet["text_expanded"] == "pic https://x.com/tester/status/2/photo/1"


def test_fetch_tweet_raw_rejects_bad_id() -> None:
    with pytest.raises(InvalidInput):
        asyncio.run(syn.fetch_tweet_raw("not-a-number"))
