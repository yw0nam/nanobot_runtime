"""LTMInjectionHook — injects relevant long-term memories into the system prompt.

Fires on `before_iteration` at the start of each user turn (iteration==0),
searches mem0 with the latest user utterance, and appends a summary block to
the first system message so the LLM can ground the reply in persisted facts.
"""
from __future__ import annotations

from typing import Any, Protocol

from loguru import logger
from nanobot.agent.hook import AgentHook, AgentHookContext

_SECTION_HEADER = "## Known Facts About You (from long-term memory)"
_PREAMBLE = (
    "The following durable facts were retrieved from your long-term memory "
    "store and apply to the **current user** you are talking to. Treat them "
    "as authoritative even when worded in the third person; any name or "
    "pronoun inside these bullets refers to the current user. Do NOT call "
    "the memory search tool for the same information — it has already been "
    "retrieved for this turn."
)


class LTMClient(Protocol):
    async def search_memory(
        self,
        query: str,
        user_id: str,
        agent_id: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]: ...


def _last_user_message(messages: list[dict[str, Any]]) -> str | None:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return None


def _format_memories(results: list[dict[str, Any]]) -> str:
    lines = [_SECTION_HEADER, "", _PREAMBLE, ""]
    for item in results:
        text = item.get("memory")
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


class LTMInjectionHook(AgentHook):
    """Ephemeral context injection — does not mutate persisted state."""

    def __init__(
        self,
        ltm_client: LTMClient,
        user_id: str,
        *,
        agent_id: str | None = None,
        limit: int = 5,
    ) -> None:
        super().__init__()
        self._ltm = ltm_client
        self._user_id = user_id
        self._agent_id = agent_id
        self._limit = limit

    async def before_iteration(self, context: AgentHookContext) -> None:
        # Only at the start of a user turn — re-injecting across tool loops duplicates context.
        if context.iteration != 0:
            return

        query = _last_user_message(context.messages)
        if query is None:
            return

        result = await self._ltm.search_memory(
            query=query,
            user_id=self._user_id,
            agent_id=self._agent_id,
            limit=self._limit,
        )
        results = result.get("results") if isinstance(result, dict) else None
        if not results:
            return

        block = _format_memories(results)
        _append_to_system_message(context.messages, block)
        logger.info("LTM injection: {} memories for user={}", len(results), self._user_id)


def _append_to_system_message(messages: list[dict[str, Any]], block: str) -> None:
    for msg in messages:
        if msg.get("role") == "system":
            existing = msg.get("content", "")
            if isinstance(existing, str):
                msg["content"] = f"{existing}\n\n{block}" if existing else block
            return
    # No system message found — prepend one.
    messages.insert(0, {"role": "system", "content": block})
