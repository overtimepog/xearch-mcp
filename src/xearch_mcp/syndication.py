"""Read tweets through X's public syndication endpoint.

Endpoint:
    https://cdn.syndication.twimg.com/tweet-result?id=<tweet_id>&token=a

The endpoint needs no API key, cookies, or login. It returns one tweet. Long
("note") posts are truncated; those are enriched through FxTwitter's public
API, which returns the complete text. Enrichment is optional and can be
disabled with XEARCH_ENRICH=0.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .errors import InvalidInput, TweetUnavailable, UpstreamError
from .http import HttpClient
from .runtime import get_runtime
from .text import (
    BARE_TWEET_ID_RE,
    STATUS_URL_RE,
    extract_tco_urls,
    normalize_x_url,
    replace_tco_urls,
)

STATUS_ID_RE = re.compile(r"/status(?:es)?/(\d{1,25})", re.IGNORECASE)
MAX_TWEETS_PER_CALL = 25


# ---------------------------------------------------------------------------
# Input parsing and short-link resolution
# ---------------------------------------------------------------------------


async def resolve_tweet_input(value: str) -> str:
    """Turn a tweet ID, x.com URL, or t.co link into a tweet ID."""
    raw = (value or "").strip()
    if not raw:
        raise InvalidInput("Expected a tweet ID or URL.")
    if BARE_TWEET_ID_RE.fullmatch(raw):
        return raw

    match = STATUS_URL_RE.search(raw)
    if match:
        return match.group(2)

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.netloc.lower() in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
        "m.twitter.com",
        "mobile.x.com",
    }:
        status_match = STATUS_ID_RE.search(parsed.path)
        if status_match:
            return status_match.group(1)
    if parsed.netloc.lower() in {"t.co", "www.t.co"}:
        final = await resolve_tco(raw)
        if final:
            status_match = STATUS_ID_RE.search(final)
            if status_match:
                return status_match.group(1)
        raise InvalidInput("The t.co link did not resolve to a tweet.")
    raise InvalidInput("Expected a tweet ID or a URL like https://x.com/user/status/1234567890.")


async def resolve_tco(short_url: str) -> str | None:
    """Resolve one t.co redirect without fetching the destination page.

    Only t.co is fetched, so this cannot be used as a generic SSRF primitive.
    """
    if not short_url:
        return None
    parsed = urlparse(short_url)
    if parsed.netloc.lower() not in {"t.co", "www.t.co"}:
        raise InvalidInput("Only t.co links can be resolved.")
    rt = get_runtime()
    cached = rt.links.get(short_url)
    if cached is not None:
        return cached or None

    try:
        response = await rt.http.get(
            short_url,
            headers={"Accept": "text/html,application/xhtml+xml"},
            follow_redirects=False,
            retries=1,
        )
    except UpstreamError:
        rt.links.set(short_url, "")
        return None

    location = response.headers.get("location")
    if not location:
        rt.links.set(short_url, "")
        return None
    resolved = normalize_x_url(location)
    rt.links.set(short_url, resolved)
    return resolved


# ---------------------------------------------------------------------------
# Upstream fetches
# ---------------------------------------------------------------------------


def _http(settings: Any | None = None) -> HttpClient:
    return get_runtime().http


async def fetch_tweet_raw(tweet_id: str) -> dict[str, Any]:
    """Fetch the raw syndication JSON for one tweet, with a TTL cache."""
    tweet_id = (tweet_id or "").strip()
    if not BARE_TWEET_ID_RE.fullmatch(tweet_id):
        raise InvalidInput(f"Invalid tweet ID: {tweet_id!r}")

    rt = get_runtime()
    cached = rt.tweets.get(f"tweet:{tweet_id}")
    if cached is not None:
        return cached

    settings = rt.settings
    try:
        data = await rt.http.get_json(
            settings.syndication_url,
            params={"id": tweet_id, "token": "a"},
            headers={"Accept": "application/json", "Referer": "https://x.com/"},
            retries=2,
        )
    except UpstreamError as exc:
        if exc.status in {404, 410}:
            raise TweetUnavailable(
                f"Tweet {tweet_id} is unavailable (deleted, protected, or invalid)."
            ) from exc
        if exc.status == 429:
            raise UpstreamError(
                "X's syndication endpoint is rate-limiting requests. Try again shortly."
            ) from exc
        if exc.status is None and "Expected JSON" in str(exc):
            raise TweetUnavailable(
                f"Tweet {tweet_id} is unavailable (deleted, protected, or invalid)."
            ) from exc
        raise

    if not isinstance(data, dict) or not data.get("id_str"):
        raise TweetUnavailable(f"Tweet {tweet_id} is unavailable (deleted, protected, or invalid).")

    rt.tweets.set(f"tweet:{tweet_id}", data)
    return data


async def fetch_fx_tweet(tweet_id: str) -> dict[str, Any] | None:
    """Fetch FxTwitter's JSON for a tweet, or None if unavailable."""
    rt = get_runtime()
    cached = rt.tweets.get(f"fx:{tweet_id}")
    if cached is not None:
        return cached or None

    settings = rt.settings
    try:
        data = await rt.http.get_json(
            f"{settings.fxtwitter_base}/i/status/{tweet_id}",
            headers={"Accept": "application/json"},
            retries=1,
        )
    except UpstreamError:
        rt.tweets.set(f"fx:{tweet_id}", {})
        return None

    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict) or not tweet.get("text"):
        rt.tweets.set(f"fx:{tweet_id}", {})
        return None
    rt.tweets.set(f"fx:{tweet_id}", tweet)
    return tweet


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _author(user: dict[str, Any]) -> dict[str, Any]:
    username = user.get("screen_name") or ""
    avatar = user.get("profile_image_url_https") or ""
    if avatar:
        avatar = re.sub(
            r"_(normal|200x200|400x400)(\.(?:jpg|jpeg|png|webp))$",
            r"_400x400\2",
            avatar,
            flags=re.IGNORECASE,
        )
    return {
        "id": user.get("id_str"),
        "name": user.get("name"),
        "username": username or None,
        "url": f"https://x.com/{username}" if username else None,
        "verified": bool(user.get("verified")),
        "blue_verified": bool(user.get("is_blue_verified")),
        "avatar_url": avatar or None,
    }


def _metrics(raw: dict[str, Any], fx: dict[str, Any] | None) -> dict[str, int]:
    metrics: dict[str, int] = {}
    if raw.get("favorite_count") is not None:
        metrics["likes"] = int(raw["favorite_count"])
    if raw.get("conversation_count") is not None:
        metrics["replies"] = int(raw["conversation_count"])
    if raw.get("retweet_count") is not None:
        metrics["retweets"] = int(raw["retweet_count"])
    if raw.get("reply_count") is not None:
        metrics["replies"] = int(raw["reply_count"])
    if fx:
        for key in ("likes", "replies", "retweets", "quotes", "views", "bookmarks"):
            if fx.get(key) is not None and key not in metrics:
                metrics[key] = int(fx[key])
    return metrics


def _media(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for photo in raw.get("photos") or []:
        if not isinstance(photo, dict):
            continue
        url = photo.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(
            {
                "type": "photo",
                "url": url,
                "width": photo.get("width"),
                "height": photo.get("height"),
                "expanded_url": photo.get("expandedUrl"),
            }
        )

    for media in raw.get("mediaDetails") or []:
        if not isinstance(media, dict):
            continue
        url = media.get("media_url_https")
        if not url or url in seen:
            continue
        seen.add(url)
        original = media.get("original_info") or {}
        items.append(
            {
                "type": media.get("type") or "photo",
                "url": url,
                "width": original.get("width"),
                "height": original.get("height"),
                "expanded_url": media.get("expanded_url"),
            }
        )

    video = raw.get("video")
    if isinstance(video, dict):
        variants = []
        for variant in video.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            variants.append(
                {
                    "url": variant.get("url"),
                    "content_type": variant.get("content_type") or variant.get("contentType"),
                    "bitrate": variant.get("bitrate"),
                }
            )
        poster = (
            video.get("posterImageUrl") or video.get("poster_image_url") or video.get("posterImage")
        )
        if poster or variants:
            items.append(
                {
                    "type": "video",
                    "url": poster,
                    "variants": variants,
                    "duration_ms": video.get("durationMs") or video.get("duration_ms"),
                    "aspect_ratio": video.get("aspectRatio") or video.get("aspect_ratio"),
                }
            )
    return items


def _binding(bindings: dict[str, Any], key: str) -> Any:
    item = bindings.get(key)
    if not isinstance(item, dict):
        return None
    for value_key in ("string_value", "image_value", "user_value"):
        if value_key in item:
            return item[value_key]
    return None


def _card(raw: dict[str, Any]) -> dict[str, Any] | None:
    card = raw.get("card")
    if not isinstance(card, dict):
        return None
    bindings = card.get("binding_values")
    if not isinstance(bindings, dict):
        bindings = {}

    image = (
        _binding(bindings, "thumbnail_image_large")
        or _binding(bindings, "thumbnail_image")
        or _binding(bindings, "player_image_large")
        or _binding(bindings, "player_image")
    )
    if isinstance(image, dict):
        image = image.get("url")

    url = card.get("url")
    return {
        "name": card.get("name"),
        "url": url,
        "expanded_url": None,
        "title": _binding(bindings, "title"),
        "description": _binding(bindings, "description"),
        "domain": _binding(bindings, "domain"),
        "image_url": image,
        "player_url": _binding(bindings, "player_url"),
        "vanity_url": _binding(bindings, "vanity_url"),
    }


def _utf16_len(value: str) -> int:
    """Twitter counts text in UTF-16 code units, so emoji count as two."""
    return len(value.encode("utf-16-le")) // 2


def _is_truncated(raw: dict[str, Any]) -> bool:
    text = raw.get("text") or ""
    display_range = raw.get("display_text_range")
    if isinstance(display_range, list) and len(display_range) == 2:
        try:
            return int(display_range[1]) > _utf16_len(text)
        except (TypeError, ValueError):
            return False
    return False


async def normalize_tweet(
    raw: dict[str, Any],
    *,
    include_quoted: bool = True,
    resolve_links: bool = True,
    enrich: bool | None = None,
    include_raw: bool = False,
    depth: int = 0,
) -> dict[str, Any]:
    """Turn syndication JSON into a stable, model-friendly shape."""
    rt = get_runtime()
    if enrich is None:
        enrich = rt.settings.enrich

    tweet_id = str(raw.get("id_str") or raw.get("id") or "")
    author = _author(raw.get("user") or {})
    text = raw.get("text") or ""
    text_source = "syndication"
    has_note = bool(raw.get("note_tweet"))
    # Note tweets can carry a preview whose display range matches the preview,
    # so the presence of note_tweet is itself a truncation signal.
    truncated = has_note or _is_truncated(raw)
    fx: dict[str, Any] | None = None
    notes: list[str] = []

    if truncated and enrich:
        fx = await fetch_fx_tweet(tweet_id)
        fx_text = (fx or {}).get("text") or ""
        if fx is not None:
            if len(fx_text) > len(text):
                text = fx_text
                text_source = "fxtwitter"
            # FxTwitter returns the complete text, so it is authoritative.
            truncated = False
        else:
            notes.append(
                "The syndication endpoint truncated this long post and FxTwitter "
                "enrichment did not return the full text."
            )
    elif truncated:
        notes.append(
            "The syndication endpoint truncated this long post. "
            "Set enrich=true or XEARCH_ENRICH=1 to fetch the full text."
        )

    card = _card(raw)
    media = _media(raw)

    # Resolve t.co links. `entities` is often empty in this response, so this
    # mostly uses one HEAD-style redirect lookup per unique t.co URL.
    mapping: dict[str, str] = {}
    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    for entity in entities.get("urls") or []:
        if isinstance(entity, dict) and entity.get("url") and entity.get("expanded_url"):
            mapping[str(entity["url"])] = str(entity["expanded_url"])

    media_short = {
        str(item["url"])
        for item in (raw.get("mediaDetails") or [])
        if isinstance(item, dict) and item.get("url")
    }
    card_short = card.get("url") if card else None

    links: list[dict[str, Any]] = []
    for short in extract_tco_urls(text):
        if short in media_short or short == card_short:
            continue
        expanded = mapping.get(short)
        if not expanded and resolve_links:
            expanded = await resolve_tco(short)
        links.append(
            {
                "short_url": short,
                "url": expanded,
                "resolved": bool(expanded),
            }
        )
        if expanded:
            mapping[short] = expanded

    for item in raw.get("mediaDetails") or []:
        if isinstance(item, dict) and item.get("url") and item.get("expanded_url"):
            mapping[str(item["url"])] = str(item["expanded_url"])

    if card and card.get("url"):
        expanded = None
        if resolve_links:
            expanded = await resolve_tco(str(card["url"]))
        card["expanded_url"] = expanded
        if expanded:
            mapping[str(card["url"])] = expanded

    text_expanded = replace_tco_urls(text, mapping)
    quoted_raw = raw.get("quoted_tweet")
    quoted = None
    if include_quoted and isinstance(quoted_raw, dict) and quoted_raw.get("id_str") and depth < 1:
        quoted = await normalize_tweet(
            quoted_raw,
            include_quoted=False,
            resolve_links=resolve_links,
            enrich=enrich,
            include_raw=include_raw,
            depth=depth + 1,
        )

    result: dict[str, Any] = {
        "id": tweet_id,
        "url": (
            f"https://x.com/{author['username']}/status/{tweet_id}"
            if author.get("username")
            else f"https://x.com/i/status/{tweet_id}"
        ),
        "author": author,
        "created_at": raw.get("created_at"),
        "lang": raw.get("lang"),
        "text": text,
        "text_expanded": text_expanded if text_expanded != text else None,
        "truncated": truncated,
        "is_note_tweet": has_note or bool((fx or {}).get("is_note_tweet")),
        "text_source": text_source,
        "possibly_sensitive": bool(raw.get("possibly_sensitive")),
        "metrics": _metrics(raw, fx),
        "media": media,
        "links": links,
        "card": card,
        "quoted_tweet": quoted,
        "source": "syndication+fxtwitter" if fx else "syndication",
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    if notes:
        result["notes"] = notes
    if include_raw:
        result["raw"] = raw
    return result


# ---------------------------------------------------------------------------
# Public API used by the MCP server
# ---------------------------------------------------------------------------


async def get_tweet(
    tweet: str,
    *,
    include_quoted: bool = True,
    resolve_links: bool = True,
    enrich: bool | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    tweet_id = await resolve_tweet_input(tweet)
    raw = await fetch_tweet_raw(tweet_id)
    return await normalize_tweet(
        raw,
        include_quoted=include_quoted,
        resolve_links=resolve_links,
        enrich=enrich,
        include_raw=include_raw,
    )


async def get_tweets(
    tweets: list[str],
    *,
    include_quoted: bool = True,
    resolve_links: bool = True,
    enrich: bool | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    if not tweets:
        raise InvalidInput("Provide at least one tweet ID or URL.")
    if len(tweets) > MAX_TWEETS_PER_CALL:
        raise InvalidInput(f"At most {MAX_TWEETS_PER_CALL} tweets per call.")

    rt = get_runtime()
    semaphore = asyncio.Semaphore(rt.settings.max_concurrency)

    async def one(value: str) -> dict[str, Any]:
        async with semaphore:
            try:
                tweet = await get_tweet(
                    value,
                    include_quoted=include_quoted,
                    resolve_links=resolve_links,
                    enrich=enrich,
                    include_raw=include_raw,
                )
                return {"input": value, "ok": True, "tweet": tweet}
            except (InvalidInput, TweetUnavailable, UpstreamError) as exc:
                return {"input": value, "ok": False, "error": str(exc)}

    results = await asyncio.gather(*(one(value) for value in tweets))
    return {
        "count": len(results),
        "succeeded": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "results": results,
    }
