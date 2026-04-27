"""Tests for LTMArgumentsHook — overrides user_id/agent_id on mcp_ltm_* calls."""

from __future__ import annotations

from nanobot.agent.hook import AgentHookContext
from nanobot.providers.base import ToolCallRequest

from nanobot_runtime.services.hooks.ltm.args import LTMArgumentsHook


def _call(name: str, **args: object) -> ToolCallRequest:
    return ToolCallRequest(id="call_0", name=name, arguments=dict(args))


async def test_overrides_user_id_on_mcp_ltm_add_memory() -> None:
    hook = LTMArgumentsHook(user_id="sangjun", agent_id="yuri")
    call = _call("mcp_ltm_add_memory", content="hi", user_id="WRONG-chat-id")

    ctx = AgentHookContext(iteration=0, messages=[], tool_calls=[call])
    await hook.before_execute_tools(ctx)

    assert call.arguments["user_id"] == "sangjun"
    assert call.arguments["agent_id"] == "yuri"
    # original content arg preserved
    assert call.arguments["content"] == "hi"


async def test_adds_user_id_when_llm_omitted_it() -> None:
    hook = LTMArgumentsHook(user_id="sangjun", agent_id="yuri")
    call = _call("mcp_ltm_search_memory", query="coffee")  # no user_id

    ctx = AgentHookContext(iteration=1, messages=[], tool_calls=[call])
    await hook.before_execute_tools(ctx)

    assert call.arguments["user_id"] == "sangjun"
    assert call.arguments["agent_id"] == "yuri"


async def test_does_not_touch_non_ltm_tools() -> None:
    hook = LTMArgumentsHook(user_id="sangjun", agent_id="yuri")
    call = _call("mcp_other_server_frobnicate", user_id="whatever")

    ctx = AgentHookContext(iteration=0, messages=[], tool_calls=[call])
    await hook.before_execute_tools(ctx)

    # left alone — hook's scope is mcp_ltm_* only
    assert call.arguments["user_id"] == "whatever"
    assert "agent_id" not in call.arguments


async def test_omits_agent_id_field_when_hook_configured_without_it() -> None:
    hook = LTMArgumentsHook(user_id="sangjun", agent_id=None)
    call = _call("mcp_ltm_search_memory", query="x")

    ctx = AgentHookContext(iteration=0, messages=[], tool_calls=[call])
    await hook.before_execute_tools(ctx)

    assert call.arguments["user_id"] == "sangjun"
    # agent_id should not be added when hook has none configured
    assert "agent_id" not in call.arguments


async def test_handles_empty_tool_calls_list() -> None:
    hook = LTMArgumentsHook(user_id="sangjun", agent_id="yuri")
    ctx = AgentHookContext(iteration=0, messages=[], tool_calls=[])
    # Must not raise.
    await hook.before_execute_tools(ctx)
