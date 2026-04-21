"""Tests for LTMSavingConsolidator — wraps nanobot Consolidator to push
archived conversation turns (summary + raw user turns) into LTM.
"""
from __future__ import annotations

from typing import Any

from nanobot_runtime.hooks.ltm_consolidator import (
    LTMSavingConsolidator,
    install_ltm_saving,
)


class _FakeInner:
    """Minimal stand-in for nanobot.agent.memory.Consolidator."""

    def __init__(self, summary: str | None = "inner summary") -> None:
        self.summary: str | None = summary
        self.archive_calls: list[list[dict[str, Any]]] = []
        # sentinel attribute to prove delegation via __getattr__ works
        self.some_other_attr = "delegated"

    async def archive(self, messages: list[dict[str, Any]]) -> str | None:
        self.archive_calls.append(messages)
        return self.summary


class _FakeLTM:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.raise_on_add = False

    async def add_memory(
        self,
        content: str,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        if self.raise_on_add:
            raise RuntimeError("LTM down")
        self.add_calls.append(
            {"content": content, "user_id": user_id, "agent_id": agent_id}
        )
        return {"results": [{"id": f"m{len(self.add_calls)}"}]}


async def test_archive_forwards_messages_and_returns_inner_summary() -> None:
    inner = _FakeInner(summary="user likes coffee")
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="sangjun", agent_id="yuri")

    messages = [{"role": "user", "content": "I like coffee"}]
    result = await wrapper.archive(messages)

    assert result == "user likes coffee"
    assert inner.archive_calls == [messages]


async def test_archive_pushes_summary_to_ltm_with_identifiers() -> None:
    inner = _FakeInner(summary="user likes coffee")
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="sangjun", agent_id="yuri")

    await wrapper.archive([{"role": "user", "content": "hi"}])

    contents = [c["content"] for c in ltm.add_calls]
    assert any("user likes coffee" in c for c in contents), contents
    for call in ltm.add_calls:
        assert call["user_id"] == "sangjun"
        assert call["agent_id"] == "yuri"


async def test_archive_pushes_user_turns_but_skips_assistant_and_tool() -> None:
    inner = _FakeInner(summary="chat")
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="u1")

    await wrapper.archive([
        {"role": "user", "content": "I like coffee"},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "also tea"},
        {"role": "tool", "content": "{}"},
        {"role": "system", "content": "ignore this"},
    ])

    contents = [c["content"] for c in ltm.add_calls]
    assert "I like coffee" in contents
    assert "also tea" in contents
    assert "noted" not in contents
    assert "{}" not in contents
    assert "ignore this" not in contents


async def test_archive_skips_ltm_when_inner_returns_none() -> None:
    inner = _FakeInner(summary=None)
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="u1")

    result = await wrapper.archive([{"role": "user", "content": "hi"}])

    assert result is None
    assert ltm.add_calls == []


async def test_archive_skips_ltm_when_inner_returns_nothing_sentinel() -> None:
    """nanobot uses '(nothing)' to signal 'no archive happened' — treat as noop."""
    inner = _FakeInner(summary="(nothing)")
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="u1")

    await wrapper.archive([{"role": "user", "content": "hi"}])

    assert ltm.add_calls == []


async def test_archive_survives_ltm_failure() -> None:
    """LTM outages must not break nanobot's archive path."""
    inner = _FakeInner(summary="ok")
    ltm = _FakeLTM()
    ltm.raise_on_add = True
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="u1")

    result = await wrapper.archive([{"role": "user", "content": "x"}])

    assert result == "ok"  # inner summary still returned, no exception propagated


async def test_archive_skips_empty_user_content() -> None:
    inner = _FakeInner(summary="ok")
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="u1")

    await wrapper.archive([
        {"role": "user", "content": ""},
        {"role": "user", "content": None},
        {"role": "user"},  # missing content key
        {"role": "user", "content": "keep"},
    ])

    contents = [c["content"] for c in ltm.add_calls]
    # summary is always pushed when non-empty; user turns only include "keep"
    assert "keep" in contents
    assert "" not in contents
    assert None not in contents


async def test_archive_omits_agent_id_when_not_configured() -> None:
    inner = _FakeInner(summary="ok")
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="u1", agent_id=None)

    await wrapper.archive([{"role": "user", "content": "hi"}])

    for call in ltm.add_calls:
        assert call["user_id"] == "u1"
        assert call["agent_id"] is None


async def test_delegates_unknown_attribute_access_to_inner() -> None:
    """Wrapper must pass through everything except archive() to inner."""
    inner = _FakeInner()
    ltm = _FakeLTM()
    wrapper = LTMSavingConsolidator(inner, ltm, user_id="u1")

    assert wrapper.some_other_attr == "delegated"


async def test_install_ltm_saving_redirects_inner_archive_calls() -> None:
    """install_ltm_saving monkey-patches loop.consolidator.archive so that
    both AutoCompact._archive and Consolidator.maybe_consolidate_by_tokens
    (which call `self.archive(chunk)` internally) route through LTM save.
    """
    inner = _FakeInner(summary="installed")
    ltm = _FakeLTM()

    class _FakeLoop:
        pass

    loop = _FakeLoop()
    loop.consolidator = inner

    install_ltm_saving(loop, ltm_client=ltm, user_id="sangjun", agent_id="yuri")

    # After install, calling inner.archive directly (as maybe_consolidate_by_tokens
    # does via `self.archive`) must trigger LTM push.
    result = await loop.consolidator.archive([{"role": "user", "content": "hi"}])

    assert result == "installed"
    assert ltm.add_calls, "LTM add_memory should have been called after install"
    for call in ltm.add_calls:
        assert call["user_id"] == "sangjun"
        assert call["agent_id"] == "yuri"
