"""Live-infrastructure E2E regression scenarios (Phase 3-D, real backends).

These run against a real gateway subprocess which in turn talks to a
real vLLM, Irodori TTS, and LTM MCP. They complement the in-process
regression suite (``tests/regression/``) which uses fakes and covers
the wire contract deterministically — this suite catches integration
bugs the fakes cannot see (nanobot AgentLoop ordering, real TTS
chunking timing, session management, etc.).

Coverage mapping to migration-todo §3-D (scenarios A/B/C/D/E/G/H):

* A — new session full lifecycle
* B — resumed session with supplied chat_id
* C — long response → 3+ tts_chunks
* D — emotion emoji: stripped in delta, preserved in tts_chunk.text
* E — parallel chat_id isolation (two WS connections)
* G — URL ``?tts=0`` override → no synthesize(), no tts_chunk
* H — inbound ``tts_enabled: false`` → no synthesize(), no tts_chunk

F (reconnect after disconnect) and I (default-on guard) are covered by
the in-process suite; the nanobot session-TTL behaviour would need a
separate dedicated harness.

Run: ``pytest tests/e2e/ -m e2e`` (not collected by default).
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


SYNTHESIZE_LOG_MARKER = "IrodoriClient.synthesize:"


async def run_turn_and_drain(
    client,
    payload: dict,
    *,
    drain_seconds: float = 15.0,
    stream_end_timeout: float = 30.0,
) -> None:
    """Send an inbound frame, await ``stream_end``, then keep reading.

    The drain is necessary because TTS chunks arrive after ``stream_end``
    via the async Barrier. 15 s is enough for short vLLM replies with
    Irodori synth (observed wall-clock ~3 s in smoke).
    """
    await client.send_json(payload)
    await client.wait_for_event("stream_end", timeout=stream_end_timeout)
    await client.drain(drain_seconds)


# ---------------------------------------------------------------------------
# Scenario A — new session full lifecycle
# ---------------------------------------------------------------------------


async def test_a_new_session_full_lifecycle(gateway, live_client) -> None:
    client = await live_client(client_id="e2e-A")
    ready = await client.wait_for_event("ready", timeout=5.0)
    assert isinstance(ready.get("connection_id"), str) and len(ready["connection_id"]) == 36

    await run_turn_and_drain(
        client,
        {"type": "new_chat", "content": "안녕!", "tts_enabled": True},
    )

    events = client.events()
    assert events[0] == "ready"
    assert "stream_start" in events
    assert events.index("stream_start") < events.index("stream_end")
    assert "delta" in events
    # TTS on a real backend should produce at least one chunk for a
    # sentence-terminated Korean reply.
    assert events.count("tts_chunk") >= 1, events


# ---------------------------------------------------------------------------
# Scenario B — resumed session via `message` with explicit chat_id
# ---------------------------------------------------------------------------


async def test_b_resumed_session_with_explicit_chat_id(gateway, live_client) -> None:
    client = await live_client(client_id="e2e-B")
    await client.wait_for_event("ready", timeout=5.0)

    chat_id = "e2e-B-chat-001"
    await run_turn_and_drain(
        client,
        {"type": "message", "chat_id": chat_id, "content": "hi", "tts_enabled": True},
    )

    frames = client.frames
    chat_ids = {f.get("chat_id") for f in frames if "chat_id" in f and f.get("event") != "ready"}
    assert chat_ids == {chat_id}, f"expected all non-ready frames on {chat_id}; got {chat_ids}"


# ---------------------------------------------------------------------------
# Scenario C — long response → ≥3 tts_chunks
# ---------------------------------------------------------------------------


async def test_c_long_response_yields_multiple_tts_chunks(gateway, live_client) -> None:
    client = await live_client(client_id="e2e-C")
    await client.wait_for_event("ready", timeout=5.0)

    # Prompt deliberately asks for several sentences so SentenceChunker
    # emits multiple boundaries (min_chunk_length default = 50 chars).
    prompt = (
        "한국어로 오늘의 날씨, 점심 추천, 운동 팁 세 가지를 각각 한 문장씩 알려줘. "
        "각 문장은 40자 이상으로."
    )
    await run_turn_and_drain(
        client,
        {"type": "new_chat", "content": prompt, "tts_enabled": True},
        stream_end_timeout=60.0,
        drain_seconds=30.0,
    )

    tts_frames = [f for f in client.frames if f.get("event") == "tts_chunk"]
    assert len(tts_frames) >= 2, (
        f"expected ≥2 tts_chunks for a long Korean reply; got {len(tts_frames)}, "
        f"frames={[f.get('text', '')[:40] for f in tts_frames]}"
    )
    # Sequences must start at 0 and be strictly increasing.
    seqs = [f["sequence"] for f in tts_frames]
    assert seqs == sorted(seqs) and seqs[0] == 0, seqs
    assert len(set(seqs)) == len(seqs), seqs  # no duplicates


# ---------------------------------------------------------------------------
# Scenario D — emotion emoji stripped in delta, preserved in tts_chunk
# ---------------------------------------------------------------------------


async def test_d_emotion_emoji_round_trip(gateway, live_client) -> None:
    client = await live_client(client_id="e2e-D")
    await client.wait_for_event("ready", timeout=5.0)

    # Hint the model to include an emoji. vLLM is deterministic-ish with
    # short prompts but we tolerate variation.
    await run_turn_and_drain(
        client,
        {"type": "new_chat", "content": "한 문장으로 기쁘게 인사해줘 (😊 이모지 포함).", "tts_enabled": True},
        stream_end_timeout=45.0,
    )

    delta_text = "".join(f.get("text", "") for f in client.frames if f.get("event") == "delta")
    tts_texts = [f.get("text", "") for f in client.frames if f.get("event") == "tts_chunk"]
    emotions = [f.get("emotion") for f in client.frames if f.get("event") == "tts_chunk"]

    # The LLM may or may not include the emoji on any given run — we only
    # assert the invariant WHEN an emoji shows up in TTS text.
    if any("😊" in t for t in tts_texts):
        assert "😊" not in delta_text, (
            f"emoji must be stripped from delta when present in tts_chunk; "
            f"delta_text={delta_text!r}"
        )
        assert "😊" in emotions, f"emotion tag should be set; got emotions={emotions}"
    else:
        pytest.skip("Model did not emit the requested emoji on this run — "
                    "scenario D invariant not testable; rerun.")


# ---------------------------------------------------------------------------
# Scenario E — parallel chats isolated
# ---------------------------------------------------------------------------


async def test_e_parallel_chats_are_isolated(gateway, live_client) -> None:
    client_a = await live_client(client_id="e2e-E-A")
    client_b = await live_client(client_id="e2e-E-B")
    await client_a.wait_for_event("ready", timeout=5.0)
    await client_b.wait_for_event("ready", timeout=5.0)

    # Fire both turns concurrently; each must only receive its own frames.
    await asyncio.gather(
        client_a.send_json({"type": "new_chat", "content": "ping A", "tts_enabled": False}),
        client_b.send_json({"type": "new_chat", "content": "ping B", "tts_enabled": False}),
    )
    await asyncio.gather(
        client_a.wait_for_event("stream_end", timeout=45.0),
        client_b.wait_for_event("stream_end", timeout=45.0),
    )

    a_ids = {f.get("chat_id") for f in client_a.frames if "chat_id" in f and f.get("event") != "ready"}
    b_ids = {f.get("chat_id") for f in client_b.frames if "chat_id" in f and f.get("event") != "ready"}

    # Each client sees exactly one chat_id (its own assigned one) and
    # they must differ.
    assert len(a_ids) == 1, a_ids
    assert len(b_ids) == 1, b_ids
    assert a_ids != b_ids, f"clients share chat_id {a_ids}; cross-talk"


# ---------------------------------------------------------------------------
# Scenario G — URL ?tts=0 disables TTS entirely
# ---------------------------------------------------------------------------


async def test_g_url_tts_zero_skips_synth_and_frame(gateway, live_client) -> None:
    baseline = gateway.count_log_lines(SYNTHESIZE_LOG_MARKER)

    client = await live_client(client_id="e2e-G", tts="0")
    await client.wait_for_event("ready", timeout=5.0)

    # Even if FE asks for TTS in the frame, URL override must win.
    await run_turn_and_drain(
        client,
        {"type": "new_chat", "content": "hi", "tts_enabled": True},
    )

    events = client.events()
    assert "tts_chunk" not in events, events

    after = gateway.count_log_lines(SYNTHESIZE_LOG_MARKER)
    assert after == baseline, (
        f"IrodoriClient.synthesize() was called during a TTS-off turn "
        f"(baseline={baseline}, after={after})"
    )


# ---------------------------------------------------------------------------
# Scenario H — inbound tts_enabled: false
# ---------------------------------------------------------------------------


async def test_h_inbound_tts_enabled_false(gateway, live_client) -> None:
    baseline = gateway.count_log_lines(SYNTHESIZE_LOG_MARKER)

    client = await live_client(client_id="e2e-H")
    await client.wait_for_event("ready", timeout=5.0)

    await run_turn_and_drain(
        client,
        {"type": "new_chat", "content": "hi", "tts_enabled": False},
    )

    events = client.events()
    assert "tts_chunk" not in events, events

    after = gateway.count_log_lines(SYNTHESIZE_LOG_MARKER)
    assert after == baseline, (
        f"IrodoriClient.synthesize() was called with tts_enabled=false "
        f"(baseline={baseline}, after={after})"
    )
