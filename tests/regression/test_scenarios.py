"""Phase 3-D regression scenarios (migration-todo §3-D).

Each scenario exercises the channel + hook composition end-to-end in
process. See :mod:`harness` for the simulation primitives.
"""

from __future__ import annotations

import json


from .harness import FakeConnection, build_harness


# =========================================================================
# Scenario A: new session lifecycle
#   new_chat → ready → stream_start → delta → tts_chunk → stream_end
# =========================================================================


async def test_scenario_a_new_session_full_lifecycle() -> None:
    harness = build_harness()
    conn = FakeConnection(
        inbox=[
            json.dumps({"type": "new_chat", "content": "hi", "tts_enabled": True}),
        ]
    )

    await harness.drive_inbound(conn)
    chat_id = next(iter(harness.channel._chat_conn.keys()))
    assert harness.bus.inbound[0].chat_id == chat_id

    # Agent replies with one sentence (triggers synth via chunker).
    await harness.simulate_agent_turn(chat_id, deltas=["Hello there.", " Done."])

    events = harness.events(conn)
    # stream_start emitted once at first delta; one or more tts_chunks
    # arriving after stream_end is the agreed contract.
    assert "stream_start" in events
    assert events.count("stream_start") == 1
    assert events.index("stream_start") < events.index("delta")
    assert "stream_end" in events
    # At least one tts_chunk was emitted (both sentences should have synthed).
    assert events.count("tts_chunk") >= 1, events


# =========================================================================
# Scenario B: resumed session — inbound message with explicit chat_id
# =========================================================================


async def test_scenario_b_resumed_session_uses_supplied_chat_id() -> None:
    harness = build_harness()
    conn = FakeConnection(
        inbox=[
            json.dumps(
                {
                    "type": "message",
                    "chat_id": "chat-resume-42",
                    "content": "follow up",
                    "tts_enabled": True,
                }
            ),
        ]
    )

    await harness.drive_inbound(conn)
    assert harness.bus.inbound[0].chat_id == "chat-resume-42"
    assert "chat-resume-42" in harness.channel._chat_conn

    await harness.simulate_agent_turn("chat-resume-42", deltas=["ok."])

    frames = harness.frames(conn)
    # Every server→client frame carries the client-supplied chat_id.
    chat_ids = {f["chat_id"] for f in frames if "chat_id" in f}
    assert chat_ids == {"chat-resume-42"}


# =========================================================================
# Scenario C: long response → 3+ tts_chunks with incrementing sequence
# =========================================================================


async def test_scenario_c_long_response_emits_three_tts_chunks() -> None:
    harness = build_harness()
    conn = FakeConnection()
    harness.channel._attach("chat-long", conn)

    # Three sentences, each long enough to individually exceed the
    # default min_chunk_length (harness uses 1 to keep the test targeted).
    deltas = [
        "이것은 첫 번째 문장입니다.",
        " 그리고 두 번째 문장이 이어집니다.",
        " 마지막으로 세 번째 문장으로 마무리합니다.",
    ]
    await harness.simulate_agent_turn("chat-long", deltas=deltas)

    # Synthesizer should have been called three times.
    assert len(harness.synth.calls) == 3, harness.synth.calls

    tts_frames = [f for f in harness.frames(conn) if f.get("event") == "tts_chunk"]
    assert len(tts_frames) == 3
    assert [f["sequence"] for f in tts_frames] == [0, 1, 2]


# =========================================================================
# Scenario D: emotion emoji — stripped in delta, preserved in tts_chunk.text
# =========================================================================


async def test_scenario_d_emotion_emoji_stripped_in_delta_preserved_in_tts() -> None:
    harness = build_harness(emotion_emojis={"😊"})
    conn = FakeConnection()
    harness.channel._attach("chat-emotion", conn)

    await harness.simulate_agent_turn(
        "chat-emotion",
        deltas=["Hi 😊 there."],
    )

    frames = harness.frames(conn)
    delta_frames = [f for f in frames if f.get("event") == "delta"]
    tts_frames = [f for f in frames if f.get("event") == "tts_chunk"]

    assert delta_frames, "expected at least one delta frame"
    for f in delta_frames:
        assert (
            "😊" not in f["text"]
        ), f"emoji must be stripped from delta; got {f['text']!r}"

    assert tts_frames, "expected a tts_chunk (synth fired)"
    # The original sentence text (with emoji) is passed to TTS —
    # Preprocessor returns (cleaned_text, emotion_tag) and the hook
    # forwards cleaned_text into the chunk, while the emotion tag is
    # recorded separately. Preprocessor currently keeps the emoji in
    # the cleaned text (it only strips *action*/[meta]), so tts_chunk.text
    # includes it.
    assert any(
        "😊" in f["text"] for f in tts_frames
    ), f"emoji must be preserved in tts_chunk.text; got texts={[f['text'] for f in tts_frames]}"
    assert tts_frames[0]["emotion"] == "😊"


# =========================================================================
# Scenario E: parallel chats — chat_id isolation
# =========================================================================


async def test_scenario_e_parallel_chats_are_isolated() -> None:
    harness = build_harness()
    conn_a = FakeConnection()
    conn_b = FakeConnection()
    harness.channel._attach("chat-A", conn_a)
    harness.channel._attach("chat-B", conn_b)

    # Chat A and chat B each get one turn. The channel must route frames
    # to the correct connection (no cross-talk).
    await harness.simulate_agent_turn("chat-A", deltas=["alpha."])
    await harness.simulate_agent_turn("chat-B", deltas=["beta."])

    a_chats = {f["chat_id"] for f in harness.frames(conn_a) if "chat_id" in f}
    b_chats = {f["chat_id"] for f in harness.frames(conn_b) if "chat_id" in f}
    assert a_chats == {"chat-A"}, a_chats
    assert b_chats == {"chat-B"}, b_chats


# =========================================================================
# Scenario F: reconnect — same chat_id delivered via `message`
# =========================================================================


async def test_scenario_f_reconnect_with_existing_chat_id() -> None:
    harness = build_harness()

    # First connection: new_chat creates a chat_id.
    conn1 = FakeConnection(
        inbox=[
            json.dumps({"type": "new_chat", "content": "first"}),
        ]
    )
    await harness.drive_inbound(conn1)
    original_chat_id = harness.bus.inbound[0].chat_id
    await harness.simulate_agent_turn(original_chat_id, deltas=["reply one."])

    # Drop connection.
    harness.channel._detach_connection(conn1)
    assert original_chat_id not in harness.channel._chat_conn

    # Reconnect with the same chat_id via `message`.
    conn2 = FakeConnection(
        inbox=[
            json.dumps(
                {
                    "type": "message",
                    "chat_id": original_chat_id,
                    "content": "second",
                }
            ),
        ]
    )
    await harness.drive_inbound(conn2)
    assert harness.bus.inbound[-1].chat_id == original_chat_id
    await harness.simulate_agent_turn(original_chat_id, deltas=["reply two."])

    # Post-reconnect reply must land on conn2, not conn1.
    conn2_frames = harness.frames(conn2)
    assert any(
        "reply two" in f.get("content", "") or "reply" in f.get("text", "")
        for f in conn2_frames
    ), f"expected reply frames on conn2; got {conn2_frames}"


# =========================================================================
# Scenario G: TTS off via URL ?tts=0
#   (i) no tts_chunk frame   (ii) synthesize() not called
# =========================================================================


async def test_scenario_g_tts_off_via_url_override_skips_synth_and_frame() -> None:
    harness = build_harness()
    conn = FakeConnection(
        inbox=[
            json.dumps({"type": "new_chat", "content": "hi", "tts_enabled": True}),
        ]
    )
    # URL override wins over the per-message flag.
    harness.tts_override_tts_zero(conn)
    await harness.drive_inbound(conn)
    chat_id = next(iter(harness.channel._chat_conn.keys()))

    await harness.simulate_agent_turn(chat_id, deltas=["Hello there."])

    assert (
        harness.synth.calls == []
    ), f"synthesizer must not be invoked when URL ?tts=0; got {harness.synth.calls}"
    events = harness.events(conn)
    assert "tts_chunk" not in events, events


# =========================================================================
# Scenario H: TTS off via new_chat.tts_enabled=false
# =========================================================================


async def test_scenario_h_tts_off_via_inbound_flag_skips_synth_and_frame() -> None:
    harness = build_harness()
    conn = FakeConnection(
        inbox=[
            json.dumps({"type": "new_chat", "content": "hi", "tts_enabled": False}),
        ]
    )
    await harness.drive_inbound(conn)
    chat_id = next(iter(harness.channel._chat_conn.keys()))

    await harness.simulate_agent_turn(chat_id, deltas=["Hello there."])

    assert (
        harness.synth.calls == []
    ), f"synthesizer must not be invoked when tts_enabled=False; got {harness.synth.calls}"
    events = harness.events(conn)
    assert "tts_chunk" not in events, events


# =========================================================================
# Scenario I: TTS default (flag absent) keeps synth on — guards regressions
# where we flip the default by accident.
# =========================================================================


async def test_scenario_i_tts_default_enabled_when_flag_absent() -> None:
    harness = build_harness()
    conn = FakeConnection(
        inbox=[
            json.dumps({"type": "new_chat", "content": "hi"}),  # no tts_enabled
        ]
    )
    await harness.drive_inbound(conn)
    chat_id = next(iter(harness.channel._chat_conn.keys()))

    await harness.simulate_agent_turn(chat_id, deltas=["Hello there."])

    assert harness.synth.calls == ["Hello there."], harness.synth.calls
    events = harness.events(conn)
    assert "tts_chunk" in events
