"""Integration test for the LTM MCP client adapter.

Requires a running ltm-mcp at http://127.0.0.1:7777/mcp/ (see
agents/mcp_servers/ltm/).
"""

from __future__ import annotations

import pytest

from nanobot_runtime.clients.ltm import LTMMCPClient

pytestmark = pytest.mark.integration

LTM_URL = "http://127.0.0.1:7777/mcp/"


async def test_roundtrip_add_search_delete_via_adapter() -> None:
    client = LTMMCPClient(url=LTM_URL)
    user_id = "test-nanobot-runtime-integration"

    add = await client.add_memory(
        content="Integration test: nanobot_runtime LTM client adapter.",
        user_id=user_id,
    )
    assert "results" in add, f"add_memory should return results key, got: {add}"

    found = await client.search_memory(
        query="adapter integration", user_id=user_id, limit=5
    )
    assert "results" in found

    # Cleanup
    for item in found.get("results", []):
        mid = item.get("id")
        if mid:
            await client.delete_memory(memory_id=mid, user_id=user_id)
