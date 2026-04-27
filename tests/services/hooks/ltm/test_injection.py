"""Unit tests for LTMInjectionHook.

Hook injects relevant long-term memories into the system prompt before the
first LLM call of each user turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from nanobot.agent.hook import AgentHookContext

from nanobot_runtime.services.hooks.ltm.injection import LTMInjectionHook


async def test_before_iteration_searches_last_user_message_and_injects_into_system_prompt() -> (
    None
):
    ltm = AsyncMock()
    ltm.search_memory.return_value = {
        "results": [
            {"id": "m1", "memory": "User's name is Sangjun"},
            {"id": "m2", "memory": "User prefers dark roast coffee"},
        ]
    }
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun", limit=5)

    ctx = AgentHookContext(
        iteration=0,
        messages=[
            {"role": "system", "content": "You are yuri."},
            {"role": "user", "content": "What do you remember about me?"},
        ],
    )
    await hook.before_iteration(ctx)

    ltm.search_memory.assert_awaited_once_with(
        query="What do you remember about me?",
        user_id="sangjun",
        agent_id=None,
        limit=5,
    )

    sys_msg = ctx.messages[0]["content"]
    assert "You are yuri." in sys_msg  # original preserved
    # Header tells the LLM these apply to the current user, not third parties.
    assert "Known Facts About You" in sys_msg
    # Preamble anchors identity so 3rd-person memory text is not misread.
    assert "current user" in sys_msg.lower()
    assert "Sangjun" in sys_msg
    assert "dark roast coffee" in sys_msg


async def test_skips_injection_on_non_zero_iteration() -> None:
    """Inside a tool loop (iteration>0) the hook must not re-inject — would duplicate."""
    ltm = AsyncMock()
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun")

    ctx = AgentHookContext(
        iteration=1,
        messages=[
            {"role": "system", "content": "You are yuri."},
            {"role": "user", "content": "hi"},
        ],
    )
    await hook.before_iteration(ctx)

    ltm.search_memory.assert_not_called()


async def test_skips_injection_when_no_user_message_present() -> None:
    ltm = AsyncMock()
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun")

    ctx = AgentHookContext(
        iteration=0,
        messages=[{"role": "system", "content": "You are yuri."}],
    )
    await hook.before_iteration(ctx)

    ltm.search_memory.assert_not_called()


async def test_skips_injection_on_empty_results() -> None:
    ltm = AsyncMock()
    ltm.search_memory.return_value = {"results": []}
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun")

    ctx = AgentHookContext(
        iteration=0,
        messages=[
            {"role": "system", "content": "You are yuri."},
            {"role": "user", "content": "hi"},
        ],
    )
    await hook.before_iteration(ctx)

    assert "Relevant Long-Term Memories" not in ctx.messages[0]["content"]


async def test_skips_injection_when_backend_returns_error() -> None:
    """Mem0 error path returns {'error': str} instead of {'results': [...]} — must not inject."""
    ltm = AsyncMock()
    ltm.search_memory.return_value = {"error": "qdrant down"}
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun")

    ctx = AgentHookContext(
        iteration=0,
        messages=[
            {"role": "system", "content": "You are yuri."},
            {"role": "user", "content": "hi"},
        ],
    )
    await hook.before_iteration(ctx)

    assert "Relevant Long-Term Memories" not in ctx.messages[0]["content"]


async def test_agent_id_is_forwarded_when_set() -> None:
    ltm = AsyncMock()
    ltm.search_memory.return_value = {"results": []}
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun", agent_id="yuri", limit=3)

    ctx = AgentHookContext(
        iteration=0,
        messages=[
            {"role": "system", "content": "You are yuri."},
            {"role": "user", "content": "hi"},
        ],
    )
    await hook.before_iteration(ctx)

    ltm.search_memory.assert_awaited_once_with(
        query="hi", user_id="sangjun", agent_id="yuri", limit=3
    )


async def test_double_fire_at_iteration_zero_is_idempotent() -> None:
    """Defense in depth: if `before_iteration` somehow fires twice at iteration==0
    within the same turn (future runner change, retry path, etc.), the second call
    must not re-search LTM nor append a duplicate block to the system prompt."""
    ltm = AsyncMock()
    ltm.search_memory.return_value = {
        "results": [{"id": "m1", "memory": "User prefers dark roast coffee"}]
    }
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun")

    ctx = AgentHookContext(
        iteration=0,
        messages=[
            {"role": "system", "content": "You are yuri."},
            {"role": "user", "content": "tell me what you know"},
        ],
    )
    await hook.before_iteration(ctx)
    await hook.before_iteration(ctx)

    # Exactly one search — second fire short-circuits on the marker.
    assert ltm.search_memory.await_count == 1
    sys_content = ctx.messages[0]["content"]
    # The header must appear exactly once (no duplicate block).
    assert sys_content.count("Known Facts About You") == 1
    assert sys_content.count("dark roast coffee") == 1


async def test_prepends_system_message_when_none_exists() -> None:
    """If no system message is present, hook should create one with the memories."""
    ltm = AsyncMock()
    ltm.search_memory.return_value = {"results": [{"id": "m1", "memory": "fact one"}]}
    hook = LTMInjectionHook(ltm_client=ltm, user_id="sangjun")

    ctx = AgentHookContext(
        iteration=0,
        messages=[{"role": "user", "content": "hello"}],
    )
    await hook.before_iteration(ctx)

    assert ctx.messages[0]["role"] == "system"
    assert "fact one" in ctx.messages[0]["content"]
    # original user message still there
    assert ctx.messages[1]["role"] == "user"
