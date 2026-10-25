"""Search X/Twitter through public web indexes, then hydrate tweets.

Search uses the same keyless MCP search endpoints as OpenCode: Parallel first,
then Exa. Both return ordinary web results, which Xearch scopes to x.com and
turns into status IDs before hydrating each post through the public syndication
endpoint. DuckDuckGo and Brave remain as fallbacks, and Brave's official API is
used when BRAVE_API_KEY is set.

Result pages are cached, so pagination through the MCP backends does not repeat
the same upstream search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .errors import (
    InvalidInput,
    ProfileUnavailable,
    SearchBlocked,
    SearchConfigError,
    SearchError,
    UpstreamError,
    XearchError,
)
from .runtime import get_runtime
from .syndication import get_tweet
from .text import (
    STATUS_URL_RE,
    USERNAME_RE,
    clean_inline,
    parse_profile_html,
)

Freshness = Literal["any", "day", "week", "month", "year"]
SortOrder = Literal["relevance", "latest"]

BRAVE_FRESHNESS = {"any": None, "day": "pd", "week": "pw", "month": "pm", "year": "py"}
BRAVE_API_FRESHNESS = {"any": None, "day": "pd", "week": "pw", "month": "pm", "year": "py"}
DDG_FRESHNESS = {"any": None, "day": "d", "week": "w", "month": "m", "year": "y"}
BACKENDS = {"parallel", "exa", "ddg", "brave", "brave-api"}
MCP_PAGE_SIZE = 10

MIRROR_STATUS_URL_RE = re.compile(
    r"https?://[^\s/]+/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{1,25})",
    re.IGNORECASE,
)
MCP_BLOCK_RE = re.compile(r"\n\s*\n---\s*\n\s*\n")
MCP_URL_RE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
MCP_TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)
MCP_BODY_RE = re.compile(r"^(?:Highlights|Text):\s*\n?([\s\S]*)$", re.MULTILINE)

FROM_RE = re.compile(r"(?:^|\s)from:([A-Za-z0-9_]{1,15})\b", re.IGNORECASE)
SINCE_RE = re.compile(r"(?:^|\s)since:(\S+)", re.IGNORECASE)
UNTIL_RE = re.compile(r"(?:^|\s)until:(\S+)", re.IGNORECASE)
LANG_RE = re.compile(r"(?:^|\s)lang:([A-Za-z]{2,3})\b", re.IGNORECASE)
FILTER_MEDIA_RE = re.compile(r"(?:^|\s)filter:media\b", re.IGNORECASE)
FILTER_LINKS_RE = re.compile(r"(?:^|\s)filter:links\b", re.IGNORECASE)
MIN_FAVES_RE = re.compile(r"(?:^|\s)min_faves:(\d+)\b", re.IGNORECASE)
MIN_RETWEETS_RE = re.compile(r"(?:^|\s)min_retweets:(\d+)\b", re.IGNORECASE)
SITE_RE = re.compile(r"(?:^|\s)site:\S+", re.IGNORECASE)

logger = logging.getLogger("xearch")


@dataclass(frozen=True)
class SearchHit:
    tweet_id: str
    url: str
    username: str | None
    title: str
    snippet: str


@dataclass
class QueryPlan:
    input: str
    effective: str
    cleaned: str
    filters: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def parse_brave_html(page_html: str) -> list[SearchHit]:
    """Extract x.com status results from a Brave search page."""
    if not page_html or not page_html.strip():
        raise SearchBlocked("Brave returned an empty response.")

    soup = BeautifulSoup(page_html, "html.parser")
    blocks = soup.select('[data-type="web"]')
    hits: dict[str, SearchHit] = {}

    for block in blocks:
        anchor = None
        match = None
        for candidate in block.find_all("a", href=True):
            href = urljoin("https://search.brave.com", str(candidate["href"]))
            found = STATUS_URL_RE.search(href)
            if found:
                anchor = candidate
                match = found
                break
        if not anchor or not match:
            continue

        username, tweet_id = match.group(1), match.group(2)
        title_el = block.select_one(".title") or block.select_one("div[class*='title']")
        snippet_el = (
            block.select_one(".generic-snippet .content")
            or block.select_one(".snippet-description")
            or block.select_one(".snippet-content")
        )
        title = clean_inline(
            title_el.get_text(" ", strip=True) if title_el else anchor.get("title") or ""
        )
        snippet = clean_inline(snippet_el.get_text(" ", strip=True) if snippet_el else "")

        existing = hits.get(tweet_id)
        if existing is None or len(snippet) > len(existing.snippet):
            hits[tweet_id] = SearchHit(
                tweet_id=tweet_id,
                url=f"https://x.com/{username}/status/{tweet_id}",
                username=username,
                title=title,
                snippet=snippet,
            )

    if not hits and _looks_blocked(page_html):
        raise SearchBlocked(
            "Brave served a challenge page instead of results. Set BRAVE_API_KEY to use "
            "the official Brave API, or try again later."
        )
    return list(hits.values())


def _decode_ddg_redirect(href: str) -> str:
    """Decode DuckDuckGo's /l/?uddg= redirect wrapper."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


def parse_ddg_html(page_html: str) -> list[SearchHit]:
    """Extract x.com status results from DuckDuckGo's HTML endpoint."""
    if not page_html or not page_html.strip():
        raise SearchBlocked("DuckDuckGo returned an empty response.")

    soup = BeautifulSoup(page_html, "html.parser")
    blocks = soup.select("div.result.web-result") or soup.select("div.result")
    hits: dict[str, SearchHit] = {}

    for block in blocks:
        anchor = block.select_one("a.result__a")
        if not anchor:
            continue
        href = _decode_ddg_redirect(str(anchor.get("href") or ""))
        match = STATUS_URL_RE.search(href)
        if not match:
            continue
        username, tweet_id = match.group(1), match.group(2)
        snippet_el = block.select_one(".result__snippet")
        title = clean_inline(anchor.get_text(" ", strip=True))
        snippet = clean_inline(snippet_el.get_text(" ", strip=True) if snippet_el else "")
        existing = hits.get(tweet_id)
        if existing is None or len(snippet) > len(existing.snippet):
            hits[tweet_id] = SearchHit(
                tweet_id=tweet_id,
                url=f"https://x.com/{username}/status/{tweet_id}",
                username=username,
                title=title,
                snippet=snippet,
            )

    if not hits and _looks_blocked(page_html):
        raise SearchBlocked(
            "DuckDuckGo served a challenge page instead of results. Try again later."
        )
    return list(hits.values())


def _record_hit(url: str, title: str, snippet: str) -> SearchHit | None:
    """Turn a search result URL into a canonical X status hit."""
    match = STATUS_URL_RE.search(url or "") or MIRROR_STATUS_URL_RE.search(url or "")
    if not match:
        return None
    username, tweet_id = match.group(1), match.group(2)
    return SearchHit(
        tweet_id=tweet_id,
        url=f"https://x.com/{username}/status/{tweet_id}",
        username=username,
        title=clean_inline(title),
        snippet=clean_inline(snippet)[:800],
    )


def _dedupe_hits(hits: list[SearchHit]) -> list[SearchHit]:
    deduped: dict[str, SearchHit] = {}
    for hit in hits:
        existing = deduped.get(hit.tweet_id)
        if existing is None or len(hit.snippet) > len(existing.snippet):
            deduped[hit.tweet_id] = hit
    return list(deduped.values())


def _mcp_content_text(result: dict[str, Any]) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return item["text"]
    return ""


def _unwrap_mcp_result(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or "unknown MCP error"
        raise SearchError(f"Search MCP endpoint returned an error: {message}")
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise SearchError("Search MCP endpoint returned an unexpected response.")
    if result.get("isError"):
        message = _mcp_content_text(result) or "unknown MCP error"
        raise SearchError(f"Search MCP tool failed: {message}")
    return result


def parse_mcp_body(body: str) -> dict[str, Any]:
    """Parse a JSON or SSE JSON-RPC response from an MCP search endpoint."""
    trimmed = (body or "").strip()
    if not trimmed:
        raise SearchError("Search MCP endpoint returned an empty response.")
    if trimmed.startswith("{"):
        try:
            payload = json.loads(trimmed)
        except ValueError as exc:
            raise SearchError("Search MCP endpoint returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise SearchError("Search MCP endpoint returned an unexpected response.")
        return payload
    for line in trimmed.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except ValueError:
            continue
        if isinstance(payload, dict) and ("result" in payload or "error" in payload):
            return payload
    preview = trimmed[:160].replace("\n", " ")
    raise SearchError(f"Search MCP endpoint returned an unreadable response: {preview!r}")


def parse_parallel_results(payload: dict[str, Any]) -> list[SearchHit]:
    """Extract x.com status links from Parallel's MCP response."""
    result = _unwrap_mcp_result(payload)
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        text = _mcp_content_text(result)
        try:
            structured = json.loads(text)
        except ValueError:
            structured = {}
    records = structured.get("results") if isinstance(structured, dict) else None
    hits: list[SearchHit] = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        excerpts = item.get("excerpts") or []
        snippet = " ".join(str(part) for part in excerpts if part)
        hit = _record_hit(str(item.get("url") or ""), str(item.get("title") or ""), snippet)
        if hit:
            hits.append(hit)
    return _dedupe_hits(hits)


def parse_exa_results(payload: dict[str, Any]) -> list[SearchHit]:
    """Extract X status links from Exa's MCP text response."""
    result = _unwrap_mcp_result(payload)
    text = _mcp_content_text(result)
    hits: list[SearchHit] = []
    for block in MCP_BLOCK_RE.split(text):
        url_match = MCP_URL_RE.search(block)
        if not url_match:
            continue
        title_match = MCP_TITLE_RE.search(block)
        body_match = MCP_BODY_RE.search(block)
        hit = _record_hit(
            url_match.group(1),
            title_match.group(1) if title_match else "",
            body_match.group(1) if body_match else "",
        )
        if hit:
            hits.append(hit)
    return _dedupe_hits(hits)


def _looks_blocked(page_html: str) -> bool:
    lowered = page_html.lower()
    markers = (
        "captcha",
        "unusual traffic",
        "verify you are human",
        "verify you are not a robot",
        "challenge",
        "access denied",
    )
    return any(marker in lowered for marker in markers)


def build_query(query: str, *, username: str | None = None) -> QueryPlan:
    """Translate X-style operators into a Brave query plus post-filters."""
    original = (query or "").strip()
    if not original and not username:
        raise InvalidInput("Search query cannot be empty.")

    cleaned = original
    notes: list[str] = []

    from_users = [m.group(1) for m in FROM_RE.finditer(cleaned)]
    cleaned = FROM_RE.sub(" ", cleaned)

    since_match = SINCE_RE.search(cleaned)
    until_match = UNTIL_RE.search(cleaned)
    since = _parse_date(since_match.group(1)) if since_match else None
    until = _parse_date(until_match.group(1)) if until_match else None
    if since_match:
        cleaned = SINCE_RE.sub(" ", cleaned)
    if until_match:
        cleaned = UNTIL_RE.sub(" ", cleaned)
    if since_match and since is None:
        notes.append("Ignored an invalid since: date (expected YYYY-MM-DD).")
    if until_match and until is None:
        notes.append("Ignored an invalid until: date (expected YYYY-MM-DD).")

    lang_match = LANG_RE.search(cleaned)
    lang = lang_match.group(1).lower() if lang_match else None
    if lang_match:
        cleaned = LANG_RE.sub(" ", cleaned)

    require_media = bool(FILTER_MEDIA_RE.search(cleaned))
    require_links = bool(FILTER_LINKS_RE.search(cleaned))
    cleaned = FILTER_MEDIA_RE.sub(" ", cleaned)
    cleaned = FILTER_LINKS_RE.sub(" ", cleaned)

    faves_match = MIN_FAVES_RE.search(cleaned)
    retweets_match = MIN_RETWEETS_RE.search(cleaned)
    min_likes = int(faves_match.group(1)) if faves_match else None
    min_retweets = int(retweets_match.group(1)) if retweets_match else None
    if faves_match:
        cleaned = MIN_FAVES_RE.sub(" ", cleaned)
    if retweets_match:
        cleaned = MIN_RETWEETS_RE.sub(" ", cleaned)

    cleaned = SITE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    authors: list[str] = []
    if username:
        authors.append(username.lstrip("@"))
    else:
        authors.extend(user.lstrip("@") for user in from_users)

    if username:
        site = f"site:x.com/{username.lstrip('@')}"
    elif len(authors) == 1:
        site = f"site:x.com/{authors[0]}"
    else:
        site = "site:x.com"
        if len(authors) > 1:
            notes.append(
                "Brave can only scope one profile path; multiple from: authors are "
                "matched after hydration."
            )

    effective = f"{site} {cleaned}".strip()
    filters = {
        "authors": authors,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "lang": lang,
        "require_media": require_media,
        "require_links": require_links,
        "min_likes": min_likes,
        "min_retweets": min_retweets,
    }
    return QueryPlan(
        input=original,
        effective=effective,
        cleaned=cleaned,
        filters=filters,
        notes=notes,
    )


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _filters_active(filters: dict[str, Any]) -> bool:
    return any(
        filters.get(key)
        for key in (
            "authors",
            "since",
            "until",
            "lang",
            "require_media",
            "require_links",
            "min_likes",
            "min_retweets",
        )
    )


def apply_filters(tweet: dict[str, Any], filters: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (keep, reason_removed)."""
    authors = {str(a).lower() for a in filters.get("authors") or []}
    if authors:
        author = str((tweet.get("author") or {}).get("username") or "").lower()
        if author not in authors:
            return False, "author"

    created_at = tweet.get("created_at") or ""
    created_date = _created_date(created_at)
    since = filters.get("since")
    until = filters.get("until")
    if since and created_date and created_date < date.fromisoformat(since):
        return False, "since"
    if until and created_date and created_date > date.fromisoformat(until):
        return False, "until"

    lang = filters.get("lang")
    if lang and str(tweet.get("lang") or "").lower() != lang:
        return False, "lang"

    if filters.get("require_media") and not tweet.get("media"):
        return False, "media"

    if filters.get("require_links"):
        card = tweet.get("card") or {}
        if not tweet.get("links") and not (card.get("expanded_url") or card.get("url")):
            return False, "links"

    metrics = tweet.get("metrics") or {}
    min_likes = filters.get("min_likes")
    if min_likes is not None and "likes" in metrics and metrics["likes"] < min_likes:
        return False, "min_likes"

    min_retweets = filters.get("min_retweets")
    if min_retweets is not None and "retweets" in metrics and metrics["retweets"] < min_retweets:
        return False, "min_retweets"

    return True, None


def _created_date(created_at: str) -> date | None:
    try:
        return date.fromisoformat(created_at[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------


async def _search_ddg_html(
    query: str,
    *,
    page: int,
    freshness: Freshness,
    lang: str | None,
) -> list[SearchHit]:
    rt = get_runtime()
    data: dict[str, Any] = {"q": query, "kl": "us-en"}
    df = DDG_FRESHNESS.get(freshness)
    if df:
        data["df"] = df
    if page > 1:
        # Mirror the hidden fields in DuckDuckGo's own "Next" form.
        data.update(
            {
                "s": str((page - 1) * 10),
                "dc": str((page - 1) * 10 + 1),
                "v": "l",
                "o": "json",
                "api": "d.js",
                "nextParams": "",
            }
        )

    await rt.ddg_limiter.wait()
    try:
        response = await rt.http.post_form(
            rt.settings.ddg_search_url,
            data=data,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://html.duckduckgo.com",
                "Referer": "https://html.duckduckgo.com/",
                "Content-Type": "application/x-www-form-urlencoded",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Upgrade-Insecure-Requests": "1",
            },
            retries=1,
        )
    except UpstreamError as exc:
        if exc.status == 429:
            raise SearchBlocked(
                "DuckDuckGo is rate-limiting the HTML endpoint. Try again in a few minutes."
            ) from exc
        raise SearchError(f"DuckDuckGo search failed: {exc}") from exc

    if response.status_code == 202:
        raise SearchBlocked("DuckDuckGo served a bot challenge (HTTP 202). Try again later.")
    return parse_ddg_html(response.text)


async def _search_brave_html(
    query: str,
    *,
    page: int,
    freshness: Freshness,
    lang: str | None,
) -> list[SearchHit]:
    rt = get_runtime()
    params: dict[str, Any] = {
        "q": query,
        "source": "web",
        "spellcheck": "0",
        "offset": max(0, page - 1),
    }
    tf = BRAVE_FRESHNESS.get(freshness)
    if tf:
        params["tf"] = tf
    if lang:
        params["search_lang"] = lang

    await rt.brave_limiter.wait()
    try:
        page_html = await rt.http.get_text(
            rt.settings.brave_search_url,
            params=params,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Referer": "https://search.brave.com/",
            },
            retries=1,
        )
    except UpstreamError as exc:
        if exc.status == 429:
            raise SearchBlocked(
                "Brave is rate-limiting the HTML endpoint. Set BRAVE_API_KEY or try again "
                "in a few minutes."
            ) from exc
        raise SearchError(f"Brave search failed: {exc}") from exc

    return parse_brave_html(page_html)


async def _search_brave_api(
    query: str,
    *,
    page: int,
    freshness: Freshness,
    lang: str | None,
) -> list[SearchHit]:
    rt = get_runtime()
    settings = rt.settings
    if not settings.brave_api_key:
        raise SearchConfigError(
            "XEARCH_SEARCH_BACKEND=brave-api requires BRAVE_API_KEY. Remove the setting "
            "to use the keyless HTML backend."
        )
    params: dict[str, Any] = {
        "q": query,
        "count": 20,
        "offset": max(0, page - 1),
        "safesearch": "off",
        "country": settings.brave_country.upper(),
    }
    tf = BRAVE_API_FRESHNESS.get(freshness)
    if tf:
        params["freshness"] = tf
    if lang:
        params["search_lang"] = lang

    try:
        data = await rt.http.get_json(
            settings.brave_api_url,
            params=params,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": settings.brave_api_key,
            },
            retries=1,
        )
    except UpstreamError as exc:
        if exc.status in {401, 403}:
            raise SearchConfigError(
                "Brave API rejected BRAVE_API_KEY (401/403). Check the key or unset it to "
                "use the HTML backend."
            ) from exc
        if exc.status == 429:
            raise SearchBlocked("Brave API rate limit reached. Try again shortly.") from exc
        raise SearchError(f"Brave API search failed: {exc}") from exc

    results = ((data or {}).get("web") or {}).get("results") or []
    hits: dict[str, SearchHit] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        match = STATUS_URL_RE.search(url)
        if not match:
            continue
        username, tweet_id = match.group(1), match.group(2)
        hits[tweet_id] = SearchHit(
            tweet_id=tweet_id,
            url=f"https://x.com/{username}/status/{tweet_id}",
            username=username,
            title=clean_inline(str(item.get("title") or "")),
            snippet=clean_inline(str(item.get("description") or "")),
        )
    return list(hits.values())


def _mcp_query(query: str) -> str:
    """Rewrite a profile-scoped query into the shape Parallel/Exa handle best.

    Both endpoints accept ``site:x.com`` but return profiles instead of posts
    when the query points at ``site:x.com/<user>``. Replacing the path with the
    username as a term yields actual status URLs, which are filtered by author
    after hydration anyway.
    """
    match = re.match(r"site:x\.com/([A-Za-z0-9_]+)(.*)", query.strip(), re.IGNORECASE)
    if not match:
        return query
    username, rest = match.group(1), match.group(2).strip()
    return f"site:x.com {username} {rest}".strip() if rest else f"site:x.com {username} posts"


async def _search_mcp(
    provider: Literal["parallel", "exa"],
    query: str,
    *,
    page: int,
    freshness: Freshness,
    lang: str | None,
) -> list[SearchHit]:
    """Search through the keyless Exa/Parallel MCP endpoints OpenCode uses."""
    rt = get_runtime()
    settings = rt.settings
    query = _mcp_query(query)
    cache_key = f"mcp-full:{provider}:{query}:{freshness}:{lang}:{settings.mcp_results}"
    cached = rt.search.get(cache_key)
    if isinstance(cached, list):
        hits: list[SearchHit] = cached
    else:
        await rt.mcp_limiter.wait()
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": settings.user_agent,
        }
        if provider == "parallel":
            url = settings.parallel_mcp_url
            arguments: dict[str, Any] = {
                "objective": f"Find X/Twitter posts matching: {query}",
                "search_queries": [query],
                "session_id": rt.mcp_session_id,
            }
            tool = "web_search"
            if settings.parallel_api_key:
                headers["Authorization"] = f"Bearer {settings.parallel_api_key}"
        else:
            url = settings.exa_mcp_url
            arguments = {"query": query, "numResults": max(10, settings.mcp_results)}
            tool = "web_search_exa"
            if settings.exa_api_key:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}{urlencode({'exaApiKey': settings.exa_api_key})}"

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            response = await rt.http.post_json(
                url, json=payload, headers=headers, retries=1
            )
        except UpstreamError as exc:
            label = "Parallel" if provider == "parallel" else "Exa"
            if exc.status == 429:
                raise SearchBlocked(
                    f"{label} search is rate-limiting this IP. Try again shortly."
                ) from exc
            if exc.status in {401, 403}:
                key_name = (
                    "PARALLEL_API_KEY" if provider == "parallel" else "EXA_API_KEY"
                )
                raise SearchConfigError(
                    f"{label} rejected the request (HTTP {exc.status}). Check {key_name} "
                    "or unset it to use the keyless endpoint."
                ) from exc
            raise SearchError(f"{label} search failed: {exc}") from exc

        data = parse_mcp_body(response.text)
        hits = (
            parse_parallel_results(data)
            if provider == "parallel"
            else parse_exa_results(data)
        )
        rt.search.set(cache_key, hits)

    start = max(0, page - 1) * MCP_PAGE_SIZE
    return hits[start : start + MCP_PAGE_SIZE]


async def _search_parallel(
    query: str,
    *,
    page: int,
    freshness: Freshness,
    lang: str | None,
) -> list[SearchHit]:
    return await _search_mcp(
        "parallel", query, page=page, freshness=freshness, lang=lang
    )


async def _search_exa(
    query: str,
    *,
    page: int,
    freshness: Freshness,
    lang: str | None,
) -> list[SearchHit]:
    return await _search_mcp("exa", query, page=page, freshness=freshness, lang=lang)


def backend_order(settings: Any) -> list[str]:
    """Return the backends to try, in order.

    Auto mode mirrors OpenCode's search stack: Parallel and Exa first, then the
    HTML fallbacks. Brave's official API is used when a key is configured.
    """
    configured = settings.configured_search_backend()
    if configured != "auto":
        if configured not in BACKENDS:
            raise SearchConfigError(
                "XEARCH_SEARCH_BACKEND must be auto, parallel, exa, ddg, brave, "
                "or brave-api."
            )
        return [configured]
    order: list[str] = ["parallel", "exa"]
    if settings.brave_api_key:
        order.append("brave-api")
    order.extend(["ddg", "brave"])
    return order


async def fetch_search_page(
    query: str,
    *,
    page: int,
    freshness: Freshness,
    lang: str | None,
) -> tuple[list[SearchHit], str]:
    """Return one page of search hits and the backend name.

    In auto mode, prefer the backend that worked last, then DuckDuckGo, then
    Brave. A blocked backend falls through to the next one.
    """
    rt = get_runtime()
    settings = rt.settings
    cache_key = f"search:{query}:{page}:{freshness}:{lang}"
    cached = rt.search.get(cache_key)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached

    order = backend_order(settings)
    auto = settings.configured_search_backend() == "auto"
    if auto:
        preferred = rt.search.get("backend:auto")
        if preferred in order:
            order = [preferred] + [backend for backend in order if backend != preferred]

    errors: list[tuple[str, str]] = []
    for backend in order:
        try:
            if backend == "parallel":
                hits = await _search_parallel(
                    query, page=page, freshness=freshness, lang=lang
                )
            elif backend == "exa":
                hits = await _search_exa(
                    query, page=page, freshness=freshness, lang=lang
                )
            elif backend == "ddg":
                hits = await _search_ddg_html(query, page=page, freshness=freshness, lang=lang)
            elif backend == "brave":
                hits = await _search_brave_html(query, page=page, freshness=freshness, lang=lang)
            else:
                hits = await _search_brave_api(query, page=page, freshness=freshness, lang=lang)
        except SearchConfigError:
            if not auto:
                raise
            errors.append((backend, "not configured"))
            continue
        except SearchError as exc:
            errors.append((backend, str(exc)))
            logger.warning("Search backend %s failed: %s", backend, exc)
            continue

        if not hits and auto and backend != order[-1]:
            errors.append((backend, "no x.com results"))
            continue

        rt.search.set(cache_key, (hits, backend))
        if auto:
            rt.search.set("backend:auto", backend)
        return hits, backend

    details = "; ".join(f"{backend}: {message}" for backend, message in errors)
    if any("challenge" in message.lower() or "rate" in message.lower() for _, message in errors):
        raise SearchBlocked(f"All search backends failed. {details}")
    raise SearchError(f"All search backends failed. {details}")


# ---------------------------------------------------------------------------
# Hydration and public API
# ---------------------------------------------------------------------------


def _stub(hit: SearchHit, *, hydrated: bool, error: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": hit.tweet_id,
        "url": hit.url,
        "author": {"username": hit.username},
        "text": None,
        "hydrated": hydrated,
        "search": {"title": hit.title, "snippet": hit.snippet},
    }
    if error:
        item["error"] = error
    return item


async def _hydrate(
    hits: list[SearchHit],
    *,
    include_quoted: bool,
    resolve_links: bool,
    enrich: bool | None,
) -> list[dict[str, Any]]:
    rt = get_runtime()
    semaphore = asyncio.Semaphore(rt.settings.max_concurrency)

    async def one(index: int, hit: SearchHit) -> dict[str, Any]:
        async with semaphore:
            try:
                tweet = await get_tweet(
                    hit.tweet_id,
                    include_quoted=include_quoted,
                    resolve_links=resolve_links,
                    enrich=enrich,
                )
                tweet["hydrated"] = True
                tweet["search_rank"] = index
                return tweet
            except XearchError as exc:
                return _stub(hit, hydrated=False, error=str(exc))

    return list(await asyncio.gather(*(one(i, hit) for i, hit in enumerate(hits))))


async def search_tweets(
    query: str,
    *,
    limit: int = 10,
    page: int = 1,
    sort: SortOrder = "relevance",
    freshness: Freshness = "any",
    hydrate: bool = True,
    include_quoted: bool = False,
    resolve_links: bool = False,
    enrich: bool | None = None,
    username: str | None = None,
) -> dict[str, Any]:
    """Search x.com via Brave and optionally hydrate each result."""
    if limit < 1 or limit > 30:
        raise InvalidInput("limit must be between 1 and 30.")
    if page < 1 or page > 9:
        raise InvalidInput("page must be between 1 and 9.")
    if freshness not in BRAVE_FRESHNESS:
        raise InvalidInput("freshness must be one of: any, day, week, month, year.")
    if sort not in {"relevance", "latest"}:
        raise InvalidInput("sort must be 'relevance' or 'latest'.")
    if username and not USERNAME_RE.fullmatch(username.lstrip("@")):
        raise InvalidInput("Invalid X username.")

    plan = build_query(query, username=username)
    active_filters = _filters_active(plan.filters)
    content_filters = _filters_active(
        {key: value for key, value in plan.filters.items() if key != "authors"}
    )
    many_authors = len(plan.filters.get("authors") or []) > 1

    target = limit
    if content_filters or many_authors:
        target = min(40, max(limit, 20))

    all_hits: list[SearchHit] = []
    backend = "unknown"
    page_size = 10
    raw_page_counts: dict[int, int] = {}
    current_page = page
    while current_page <= 9:
        hits, backend = await fetch_search_page(
            plan.effective,
            page=current_page,
            freshness=freshness,
            lang=plan.filters.get("lang"),
        )
        raw_page_counts[current_page] = len(hits)
        all_hits.extend(hits)
        page_size = 10 if backend in {"ddg", "parallel", "exa"} else 20
        if len(all_hits) >= target:
            break
        if len(hits) < page_size:
            break
        current_page += 1

    deduped: list[SearchHit] = []
    seen: set[str] = set()
    for hit in all_hits:
        if hit.tweet_id not in seen:
            seen.add(hit.tweet_id)
            deduped.append(hit)

    candidate_hits = deduped[:target]
    has_more = raw_page_counts.get(current_page, 0) >= page_size and current_page < 9

    notes = [
        "Search uses public web indexes scoped to x.com, not X's live search. "
        "Very recent posts may not be indexed yet."
    ]
    notes.extend(plan.notes)

    removed: dict[str, int] = {}
    if not hydrate:
        tweets = []
        for hit in candidate_hits:
            if plan.filters.get("authors"):
                authors = {a.lower() for a in plan.filters["authors"]}
                if (hit.username or "").lower() not in authors:
                    removed["author"] = removed.get("author", 0) + 1
                    continue
            tweets.append(_stub(hit, hydrated=False))
        if active_filters:
            notes.append(
                "hydrate=false: only the author filter could be applied; content filters "
                "need hydrated tweets."
            )
    else:
        hydrated = await _hydrate(
            candidate_hits,
            include_quoted=include_quoted,
            resolve_links=resolve_links,
            enrich=enrich,
        )
        kept: list[dict[str, Any]] = []
        for tweet in hydrated:
            if not tweet.get("hydrated", True):
                kept.append(tweet)
                continue
            keep, reason = apply_filters(tweet, plan.filters)
            if keep:
                kept.append(tweet)
            elif reason:
                removed[reason] = removed.get(reason, 0) + 1

        if sort == "latest":
            kept.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        else:
            kept.sort(key=lambda item: item.get("search_rank", 10**9))
        tweets = kept[:limit]

        if removed:
            notes.append(
                "Removed by filters: "
                + ", ".join(f"{reason}={count}" for reason, count in sorted(removed.items()))
                + "."
            )
        failed = sum(1 for tweet in hydrated if not tweet.get("hydrated", True))
        if failed:
            notes.append(f"{failed} search result(s) could not be hydrated; stubs are included.")

    return {
        "query": {
            "input": plan.input,
            "effective": plan.effective,
            "filters": plan.filters,
        },
        "backend": backend,
        "page": page,
        "pages_fetched": sorted(raw_page_counts),
        "next_page": current_page + 1 if has_more else None,
        "freshness": freshness,
        "sort": sort,
        "count": len(tweets),
        "has_more": has_more,
        "tweets": tweets,
        "notes": notes,
    }


async def get_user_tweets(
    username: str,
    *,
    limit: int = 10,
    page: int = 1,
    sort: SortOrder = "latest",
    freshness: Freshness = "any",
    include_quoted: bool = False,
    resolve_links: bool = False,
    enrich: bool | None = None,
) -> dict[str, Any]:
    handle = (username or "").strip().lstrip("@")
    if not USERNAME_RE.fullmatch(handle):
        raise InvalidInput("Invalid X username.")
    result = await search_tweets(
        f"from:{handle}",
        limit=limit,
        page=page,
        sort=sort,
        freshness=freshness,
        hydrate=True,
        include_quoted=include_quoted,
        resolve_links=resolve_links,
        enrich=enrich,
        username=handle,
    )
    result["username"] = handle
    return result


async def get_user(username: str) -> dict[str, Any]:
    """Read public profile metadata from the x.com profile page."""
    handle = (username or "").strip().lstrip("@")
    if not USERNAME_RE.fullmatch(handle):
        raise InvalidInput("Invalid X username.")

    rt = get_runtime()
    cache_key = f"profile:{handle.lower()}"
    cached = rt.profiles.get(cache_key)
    if cached is not None:
        return cached

    url = f"{rt.settings.x_profile_url.rstrip('/')}/{handle}"
    try:
        page_html = await rt.http.get_text(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            retries=1,
        )
    except UpstreamError as exc:
        if exc.status in {404, 410}:
            raise ProfileUnavailable(f"Profile @{handle} is unavailable.") from exc
        raise

    try:
        profile = parse_profile_html(page_html, handle)
    except ValueError as exc:
        raise ProfileUnavailable(
            f"Could not read public metadata for @{handle}. The profile may not exist."
        ) from exc

    profile["fetched_at"] = datetime.now(UTC).isoformat()
    rt.profiles.set(cache_key, profile)
    return profile
