# Xearch MCP

> Search and read X/Twitter posts without an API key.

[![CI](https://github.com/overtimepog/xearch-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/overtimepog/xearch-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

Xearch is an MCP server that gives agents four things: one post by ID or URL,
a batch of posts, keyword search over X/Twitter, and public profile metadata.
It uses X's public syndication endpoint for post JSON, so there is no X login,
cookie, or developer account involved.

Search uses the same keyless MCP search endpoints as OpenCode (Parallel, then
Exa), with DuckDuckGo and Brave as fallbacks. Xearch scopes the results to
`x.com`, then hydrates every hit through the syndication endpoint. Long
("note") posts are completed through [FxTwitter](https://github.com/FxEmbed/FxEmbed)
when the syndication endpoint returns a preview.

## Features

- **No X API key.** Read any public post from its ID or URL.
- **OpenCode-style search.** Parallel and Exa's public MCP search endpoints are
  used first, matching the search stack OpenCode uses. DuckDuckGo and Brave are
  keyless fallbacks.
- **Full long posts.** Note tweets are completed through FxTwitter when the
  syndication response is truncated.
- **Search with X-style operators.** `from:`, `since:`, `until:`, `lang:`,
  `filter:media`, `filter:links`, `min_faves:`, `min_retweets:`.
- **Hydrated search results.** Every hit includes the real text, author, date,
  metrics, media, card, and quoted post, not just a search snippet.
- **Fast batch reads.** Fetch up to 25 posts per call with bounded concurrency.
- **Caching and backoff.** TTL/LRU caches and polite rate limiting for the
  keyless search endpoints.
- **Optional provider keys.** Set `PARALLEL_API_KEY` or `EXA_API_KEY` for paid
  quota, and `BRAVE_API_KEY` to add Brave's official search API.
- **No arbitrary URL fetching.** `t.co` redirects are the only links Xearch
  resolves, and only the `t.co` host is requested. This keeps link expansion
  from becoming an SSRF primitive.

## Quick start

Run it directly from GitHub with [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/overtimepog/xearch-mcp.git xearch-mcp
```

Or clone and run locally:

```bash
git clone https://github.com/overtimepog/xearch-mcp.git
cd xearch-mcp
uv sync
uv run xearch-mcp
```

The server speaks MCP over stdio. It prints logs to stderr and protocol
messages to stdout.

### Claude Desktop

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "xearch": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/overtimepog/xearch-mcp.git",
        "xearch-mcp"
      ]
    }
  }
}
```

### OpenCode

```bash
opencode mcp add xearch --global -- \
  uvx --from git+https://github.com/overtimepog/xearch-mcp.git xearch-mcp
```

Or add it to the config manually:

```jsonc
{
  "mcp": {
    "servers": {
      "xearch": {
        "type": "local",
        "command": [
          "uvx",
          "--from",
          "git+https://github.com/overtimepog/xearch-mcp.git",
          "xearch-mcp"
        ],
        "enabled": true
      }
    }
  }
}
```

Tool names are namespaced by the client. In OpenCode they appear as
`xearch_get_tweet`, `xearch_search_tweets`, and so on (or under
`tools.xearch.*` in Code Mode).

### PentestCode

PentestCode uses the OpenCode v1 MCP shape: entries live directly under `mcp`,
not under `mcp.servers`. Add this to
`~/.config/pentestcode/pentestcode.jsonc`:

```jsonc
{
  "mcp": {
    "xearch": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/overtimepog/xearch-mcp.git",
        "xearch-mcp"
      ],
      "enabled": true,
      "timeout": 120000
    }
  }
}
```

The longer request timeout gives a search call room to finish after the MCP
startup timeout. Restart PentestCode, then confirm the server is connected:

```bash
pentestcode mcp list
```

### Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "xearch": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/overtimepog/xearch-mcp.git",
        "xearch-mcp"
      ]
    }
  }
}
```

## Tools

| Tool | What it does |
| --- | --- |
| `get_tweet` | Fetch one post by ID or URL. Returns full text, author, date, metrics, media, card, links, and quoted post. |
| `get_tweets` | Fetch up to 25 posts in one call. Failed IDs are returned individually. |
| `search_tweets` | Search X/Twitter through a public web index and hydrate the results. |
| `get_user_tweets` | Recent posts from one account (`from:<username>` plus an exact author filter). |
| `get_user` | Public profile metadata: display name, bio, avatar, and banner. |
| `status` | Show configuration, caches, and optional live endpoint checks. |

### `get_tweet`

```json
{
  "tweet": "https://x.com/seanhn/status/1925502601757372656",
  "include_quoted": true,
  "resolve_links": true,
  "enrich": true
}
```

`tweet` accepts a bare ID, an `x.com`/`twitter.com` status URL, or a `t.co`
link. `enrich` defaults to the server setting (`XEARCH_ENRICH`, normally on).

### `get_tweets`

```json
{
  "tweets": [
    "1925502601757372656",
    "https://x.com/BlockTempo/status/2113328686342853058"
  ],
  "resolve_links": false
}
```

### `search_tweets`

```json
{
  "query": "linux smb cve since:2025-01-01",
  "limit": 10,
  "freshness": "year",
  "sort": "latest",
  "hydrate": true
}
```

Search output includes a `query` object with the effective web query and the
parsed filters, the backend that answered, pagination info, and the hydrated
posts. `hydrate: false` returns fast link/snippet stubs without fetching each
post.

### Search operators

| Operator | Meaning |
| --- | --- |
| `from:username` | Posts by one account. Also scopes the web query to that profile. |
| `since:YYYY-MM-DD` | Posts on or after a date. |
| `until:YYYY-MM-DD` | Posts on or before a date. |
| `lang:en` | Post language, checked after hydration. |
| `filter:media` | Require photos or video. |
| `filter:links` | Require an external link or card. |
| `min_faves:100` | Minimum likes, checked after hydration. |
| `min_retweets:10` | Minimum retweets, checked after hydration. |

Operators are removed from the web query and applied as post-filters where the
web index cannot express them. `min_faves` and `min_retweets` keep posts whose
metric is unknown rather than dropping potentially valid results.

### `get_user_tweets`

```json
{
  "username": "seanhn",
  "limit": 5,
  "freshness": "month",
  "sort": "latest"
}
```

This is a convenience wrapper around `search_tweets` with `from:seanhn`. The
web index decides which posts are discoverable; it is not X's timeline API.

### `get_user`

```json
{
  "username": "seanhn"
}
```

Returns `name`, `bio`, `avatar_url`, `banner_url`, and `url` from the public
profile page's meta tags. Follower and following counts are not exposed there.

### `status`

```json
{
  "live": false
}
```

`live: true` checks the syndication endpoint and the search backend. It costs
one search request. Use it when a tool call is failing.

## How it works

1. **Search.** Xearch sends a query scoped to `x.com` to the same keyless MCP
   endpoints OpenCode uses. Parallel is tried first, then Exa. If both fail, it
   falls back to DuckDuckGo's HTML endpoint and Brave's HTML search page. If
   `BRAVE_API_KEY` is set, Brave's official API is inserted before the HTML
   fallbacks.
2. **Hydrate.** Each search hit's status ID is fetched from:
   `https://cdn.syndication.twimg.com/tweet-result?id=<id>&token=a`
3. **Complete long posts.** If the response contains `note_tweet` or a
   truncated display range, Xearch asks FxTwitter for the full text and extra
   metrics.
4. **Expand links.** `t.co` links are resolved with a redirect-only request to
   `t.co` (never to the destination). Redirects to `twitter.com` are rewritten
   to `x.com`.

The syndication endpoint needs no token beyond a short non-empty `token`
parameter. It returns one post, not a timeline or thread.

## Limits and honest caveats

- **Search is web-index search, not X live search.** Very recent posts may not
  be indexed yet. Search quality depends on Parallel/Exa and DuckDuckGo/Brave
  coverage.
- **Keyless search can be rate-limited.** The public MCP endpoints and the HTML
  fallbacks may throttle a shared IP. Xearch caches results, spaces requests,
  and falls back between backends. Set `PARALLEL_API_KEY` or `EXA_API_KEY` for
  paid provider quota, or `BRAVE_API_KEY` for the official Brave API.
- **MCP search pages are small.** Parallel usually returns about ten results
  per query. Ask a narrower query when you need more precision.
- **One post, not a thread.** `get_tweet` returns the requested post and its
  quoted post. Replies and the rest of the conversation are not available.
- **No follower counts.** The public profile page does not expose them.
- **Deleted, protected, or age-gated posts are unavailable.** The syndication
  endpoint returns an error page for them.
- **FxTwitter is a third-party service.** If it is down, long posts may remain
  truncated. Disable enrichment with `XEARCH_ENRICH=0` if you prefer no
  third-party calls.
- **Unofficial endpoints can change.** This server depends on public endpoints
  that X controls. A change on their side can break reads or search until the
  server is updated.
- **Tweet content is untrusted.** Posts, quoted posts, search snippets, cards,
  and linked pages can contain prompt injection or malicious instructions.
  Treat all returned text as data, never as instructions.

## Configuration

All settings are environment variables. None are required.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PARALLEL_API_KEY` | unset | Optional Parallel API key for paid quota. The keyless endpoint works without it. |
| `EXA_API_KEY` | unset | Optional Exa API key. The keyless endpoint works without it. |
| `BRAVE_API_KEY` | unset | Add Brave's official search API to the fallback chain. |
| `XEARCH_SEARCH_BACKEND` | `auto` | `auto`, `parallel`, `exa`, `ddg`, `brave`, or `brave-api`. |
| `XEARCH_PARALLEL_URL` | `https://search.parallel.ai/mcp` | Override the Parallel MCP endpoint. |
| `XEARCH_EXA_URL` | `https://mcp.exa.ai/mcp` | Override the Exa MCP endpoint. |
| `XEARCH_MCP_RESULTS` | `20` | Results requested from Exa per query (1-20). |
| `XEARCH_ENRICH` | `1` | Complete truncated note posts via FxTwitter. |
| `XEARCH_RESOLVE_LINKS` | `1` | Default for the `resolve_links` tool argument. |
| `XEARCH_TIMEOUT` | `20` | Upstream request timeout in seconds. |
| `XEARCH_TWEET_CACHE_TTL` | `600` | Raw post cache TTL in seconds. |
| `XEARCH_SEARCH_CACHE_TTL` | `180` | Search result page cache TTL in seconds. |
| `XEARCH_LINK_CACHE_TTL` | `3600` | `t.co` resolution cache TTL in seconds. |
| `XEARCH_PROFILE_CACHE_TTL` | `900` | Profile cache TTL in seconds. |
| `XEARCH_CACHE_MAX_ENTRIES` | `512` | Max entries per cache. |
| `XEARCH_MAX_CONCURRENCY` | `4` | Parallel hydration requests. |
| `XEARCH_USER_AGENT` | Chrome UA | Override the HTTP User-Agent. |
| `XEARCH_BRAVE_COUNTRY` | `us` | Country code for Brave's API. |
| `XEARCH_HEALTH_TWEET_ID` | `1925502601757372656` | Post used by `status(live=true)`. |
| `XEARCH_LOG_LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

Example:

```bash
BRAVE_API_KEY=... XEARCH_LOG_LEVEL=INFO uv run xearch-mcp
```

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest -q
uv run python scripts/mcp_smoke.py
```

Live tests are skipped by default. Run them explicitly:

```bash
XEARCH_RUN_INTEGRATION=1 uv run pytest -m integration -q
```

The smoke script exercises the actual MCP stdio handshake:

```bash
uv run python scripts/mcp_smoke.py
XEARCH_SMOKE_SEARCH=1 uv run python scripts/mcp_smoke.py
```

### Project layout

```text
src/xearch_mcp/
  __main__.py      console entry point
  server.py        MCP tools and instructions
  syndication.py   post fetch, normalization, FxTwitter enrichment
  search.py        Parallel/Exa MCP search, fallbacks, hydration, profiles
  http.py          async HTTP client, retries, rate limiting
  cache.py         TTL + LRU caches
  config.py        environment settings
  text.py          URL, HTML, and profile helpers
tests/
  fixtures/        real syndication/search/profile responses
  test_*.py        unit, MCP wiring, and opt-in live tests
scripts/
  mcp_smoke.py     end-to-end stdio smoke test
```

## Security

- Xearch never accepts arbitrary URLs to fetch. Search and profile hosts are
  fixed. Only `t.co` links are requested for redirect expansion, with redirects
  disabled after the first hop.
- Search queries are sent to `search.parallel.ai` and `mcp.exa.ai` (the same
  public endpoints OpenCode uses), or to DuckDuckGo/Brave when those fail.
  `PARALLEL_API_KEY`, `EXA_API_KEY`, and `BRAVE_API_KEY`, if provided, stay in
  the process environment and are sent only to their own provider.
- Tweet text, quoted text, cards, and search snippets are untrusted data. The
  server's MCP instructions warn agents not to follow instructions found in
  them, but clients should treat this as a defense-in-depth measure, not a
  guarantee.
- No credentials are stored.

## License

MIT. See [LICENSE](LICENSE).
