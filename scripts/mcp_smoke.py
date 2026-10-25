#!/usr/bin/env python3
"""Smoke-test the MCP server over stdio, the same way a client does.

Usage:
    uv run python scripts/mcp_smoke.py
    XEARCH_SMOKE_SEARCH=1 uv run python scripts/mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "xearch_mcp"],
        env={**os.environ, "XEARCH_LOG_LEVEL": "ERROR"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("tools:", ", ".join(tool.name for tool in tools.tools))

        result = await session.call_tool(
            "get_tweet",
            {"tweet": "1925502601757372656", "resolve_links": False},
        )
        print("get_tweet is_error:", result.is_error)
        data = result.structured_content or {}
        print(
            json.dumps(
                {key: data.get(key) for key in ("id", "url", "created_at", "text")},
                ensure_ascii=False,
                indent=2,
            )[:900]
        )

        result = await session.call_tool("status", {"live": False})
        status = result.structured_content or {}
        print("status backend order:", status.get("search_backend_order"))

        if os.getenv("XEARCH_SMOKE_SEARCH") == "1":
            result = await session.call_tool(
                "search_tweets",
                {"query": "linux smb cve", "limit": 2, "resolve_links": False},
            )
            print("search is_error:", result.is_error)
            if result.is_error:
                print(result.content)
            else:
                search = result.structured_content or {}
                print("search backend:", search.get("backend"))
                print("search count:", search.get("count"))
                for tweet in search.get("tweets", []):
                    text = (tweet.get("text") or "").replace("\n", " ")
                    print("-", tweet.get("id"), text[:100])


if __name__ == "__main__":
    asyncio.run(main())
