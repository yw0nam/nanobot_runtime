"""LTMSavingConsolidator — wraps nanobot Consolidator.archive() to also
persist archived conversation content into long-term memory.

Two-tier save per archive() call:
  1. Summary tier — the LLM-generated summary returned by inner.archive()
  2. Raw tier     — each user turn's raw content (mem0 does its own fact
                    extraction; assistant/tool/system messages are skipped)

LTM writes run after inner.archive() returns so a failure there never
breaks nanobot's consolidation path. LTM writes themselves are guarded:
an add_memory exception is logged and swallowed.

install_ltm_saving() monkey-patches the bound `archive` method on
loop.consolidator in place. This matters because:
  - AutoCompact holds a reference to the same Consolidator instance
  - Consolidator.maybe_consolidate_by_tokens() calls `self.archive(chunk)`
Both paths therefore route through the LTM-saving wrapper without needing
to reconstruct the inner Consolidator or re-wire AutoCompact.
"""
import asyncio
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger


class _LTMAddClient(ABC):
    @abstractmethod
    async def add_memory(
        self,
        content: str,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]: ...


_SUMMARY_SKIP_SENTINEL = "(nothing)"


class LTMSavingConsolidator:
    """Wraps a nanobot Consolidator; adds LTM push on successful archive."""

    def __init__(
        self,
        inner: Any,
        ltm_client: _LTMAddClient,
        user_id: str,
        agent_id: str | None = None,
    ) -> None:
        self._inner = inner
        # Capture the bound method now — install_ltm_saving may later rebind
        # `inner.archive` to this wrapper, which would cause infinite recursion
        # if we looked the attribute up lazily.
        self._inner_archive = inner.archive
        self._ltm = ltm_client
        self._user_id = user_id
        self._agent_id = agent_id

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails — safe fallthrough
        # to the wrapped Consolidator for every method we don't override.
        return getattr(self._inner, name)

    async def archive(self, messages: list[dict[str, Any]]) -> str | None:
        summary = await self._inner_archive(messages)
        if summary and summary != _SUMMARY_SKIP_SENTINEL:
            await self._push_to_ltm(messages, summary)
        return summary

    async def _push_to_ltm(
        self, messages: list[dict[str, Any]], summary: str
    ) -> None:
        tasks = [self._safe_add(f"[conversation summary] {summary}")]
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not content or not isinstance(content, str):
                continue
            tasks.append(self._safe_add(content))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_add(self, content: str) -> None:
        try:
            await self._ltm.add_memory(
                content=content,
                user_id=self._user_id,
                agent_id=self._agent_id,
            )
        except Exception:
            logger.exception(
                "LTM add_memory failed (content[:60]={!r})", content[:60]
            )


def install_ltm_saving(
    loop: Any,
    *,
    ltm_client: _LTMAddClient,
    user_id: str,
    agent_id: str | None = None,
) -> LTMSavingConsolidator:
    """Monkey-patch loop.consolidator.archive to also push to LTM.

    We rebind the bound method on the existing Consolidator instance rather
    than swapping the instance, so AutoCompact's reference and any
    `self.archive` calls inside the Consolidator stay valid.
    """
    wrapper = LTMSavingConsolidator(
        inner=loop.consolidator,
        ltm_client=ltm_client,
        user_id=user_id,
        agent_id=agent_id,
    )
    loop.consolidator.archive = wrapper.archive
    logger.info(
        "nanobot_runtime: LTM-saving consolidator installed (user_id={}, agent_id={})",
        user_id,
        agent_id,
    )
    return wrapper
