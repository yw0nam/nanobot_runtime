"""Adapter: FastMCP HTTP client wrapped as an async LTM interface.

Matches the Protocol expected by LTMInjectionHook (and future LTM hooks),
so production code uses the real ltm-mcp server while tests inject fakes.
"""
from __future__ import annotations

from typing import Any

from fastmcp import Client


class LTMMCPClient:
    """Thin async wrapper over a streamable-HTTP MCP client."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def search_memory(
        self,
        query: str,
        user_id: str,
        agent_id: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        async with Client(self._url) as c:
            result = await c.call_tool(
                "search_memory",
                {
                    "query": query,
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "limit": limit,
                },
            )
        return result.data if isinstance(result.data, dict) else {}

    async def add_memory(
        self,
        content: str,
        user_id: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        async with Client(self._url) as c:
            result = await c.call_tool(
                "add_memory",
                {"content": content, "user_id": user_id, "agent_id": agent_id},
            )
        return result.data if isinstance(result.data, dict) else {}

    async def delete_memory(
        self,
        memory_id: str,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        async with Client(self._url) as c:
            result = await c.call_tool(
                "delete_memory",
                {"memory_id": memory_id, "user_id": user_id, "agent_id": agent_id},
            )
        return result.data if isinstance(result.data, dict) else {}
