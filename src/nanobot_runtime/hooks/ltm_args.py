"""LTMArgumentsHook — wire-level override of user_id/agent_id on ltm MCP calls.

Rationale: LLMs cannot be trusted to pass the correct multi-tenant
identifiers to tool calls (observed failure: LLM supplies `chat_id` in
place of `user_id`, causing empty search results). This hook rewrites
those two arguments just before tool dispatch, after the model has
already decided which tool to call.
"""
from __future__ import annotations

from nanobot.agent.hook import AgentHook, AgentHookContext

_LTM_TOOL_PREFIX = "mcp_ltm_"


class LTMArgumentsHook(AgentHook):
    def __init__(self, user_id: str, agent_id: str | None = None) -> None:
        super().__init__()
        self._user_id = user_id
        self._agent_id = agent_id

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for call in context.tool_calls:
            if not call.name.startswith(_LTM_TOOL_PREFIX):
                continue
            call.arguments["user_id"] = self._user_id
            if self._agent_id is not None:
                call.arguments["agent_id"] = self._agent_id
