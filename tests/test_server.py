"""MCP wiring and offline tool tests."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from xearch_mcp.errors import InvalidInput
from xearch_mcp.server import mcp


async def test_tools_are_registered() -> None:
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "get_tweet",
        "get_tweets",
        "search_tweets",
        "get_user_tweets",
        "get_user",
        "status",
    }


async def test_read_only_annotations() -> None:
    tools = await mcp.list_tools()
    for tool in tools:
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.read_only_hint is True


async def test_get_tweet_schema() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["get_tweet"].input_schema
    assert "tweet" in schema["properties"]
    assert schema["required"] == ["tweet"]


async def test_search_schema_limits() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["search_tweets"].input_schema
    assert schema["properties"]["limit"]["maximum"] == 30
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["page"]["maximum"] == 9


async def test_status_offline() -> None:
    result = await mcp.call_tool("status", {"live": False})
    assert result.is_error is False
    data = result.structured_content
    assert data["name"] == "Xearch"
    assert data["version"] == "0.2.1"
    assert data["search_backend"] == "auto"
    assert data["search_backend_order"] == ["parallel", "exa", "ddg", "brave"]
    assert data["endpoints"]["search_parallel"].startswith("https://search.parallel.ai")
    assert data["endpoints"]["search_exa"].startswith("https://mcp.exa.ai")
    assert data["endpoints"]["search_ddg"].startswith("https://html.duckduckgo.com")
    assert data["endpoints"]["syndication"].startswith("https://cdn.syndication.twimg.com")


async def test_tool_error_is_readable(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise InvalidInput("Expected a tweet ID or URL.")

    monkeypatch.setattr("xearch_mcp.server._get_tweet", boom)
    with pytest.raises(ToolError, match="Expected a tweet ID"):
        await mcp.call_tool("get_tweet", {"tweet": "bad"})
