"""Tests for DesktopMateChannel — DMP-compatible WS channel + TTSSink.

All unit tests use a fake `_WSConnection` (capturing sent frames in an
in-memory list) to exercise the channel without binding a real socket.
The last test binds a real ephemeral port to cover start()/stop() lifecycle.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot_runtime.channels.desktop_mate import (
    DesktopMateChannel,
    DesktopMateConfig,
)
from nanobot_runtime.hooks.tts import TTSChunk


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConnection:
    """Minimal stand-in for a websockets ServerConnection.

    Records every outbound frame in .sent. Inbound frames are produced by
    iterating .inbox (a list of strings) asynchronously.
    """

    def __init__(self, inbox: list[str] | None = None, remote: tuple[str, int] = ("127.0.0.1", 12345)):
        self.sent: list[str] = []
        self.inbox: list[str] = list(inbox or [])
        self.remote_address = remote
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send(self, raw: str) -> None:
        if self.closed:
            raise RuntimeError("connection closed")
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    def __aiter__(self):
        async def _gen():
            for item in list(self.inbox):
                yield item
        return _gen()


class FakeBus:
    def __init__(self):
        self.inbound: list[InboundMessage] = []
        self.outbound: list[OutboundMessage] = []

    async def publish_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.outbound.append(msg)


def _make_channel(*, token: str = "secret", allow_from: list[str] | None = None, emotions: set[str] | None = None) -> tuple[DesktopMateChannel, FakeBus]:
    bus = FakeBus()
    config = DesktopMateConfig(
        token=token,
        allow_from=allow_from if allow_from is not None else ["*"],
        host="127.0.0.1",
        port=0,
    )
    channel = DesktopMateChannel(
        config=config,
        bus=bus,
        emotion_emojis=emotions if emotions is not None else {"😊", "😢"},
    )
    return channel, bus


def _decode_frames(conn: FakeConnection) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in conn.sent]


# ---------------------------------------------------------------------------
# 1. stream_start via send()
# ---------------------------------------------------------------------------


async def test_send_stream_start_emits_event_frame():
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    # Nanobot signals stream_start via a message whose metadata marks _stream_delta
    # with empty content and a _stream_id, first such delta == start. But
    # DesktopMate uses explicit stream_start marker in metadata for clarity.
    msg = OutboundMessage(
        channel="desktop_mate",
        chat_id="chat-A",
        content="",
        metadata={"_stream_start": True, "_stream_id": "s-1"},
    )
    await channel.send(msg)

    frames = _decode_frames(conn)
    assert frames == [{"event": "stream_start", "chat_id": "chat-A"}]


# ---------------------------------------------------------------------------
# 2. send_delta strips emotion emojis
# ---------------------------------------------------------------------------


async def test_send_delta_strips_emotion_emojis():
    channel, _ = _make_channel(emotions={"😊"})
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    await channel.send_delta(
        "chat-A",
        "hi 😊 there",
        {"_stream_delta": True, "_stream_id": "s-1"},
    )

    frames = _decode_frames(conn)
    assert len(frames) == 1
    assert frames[0]["event"] == "delta"
    assert frames[0]["chat_id"] == "chat-A"
    assert frames[0]["text"] == "hi  there"  # emoji removed, other chars preserved
    assert frames[0]["stream_id"] == "s-1"


# ---------------------------------------------------------------------------
# 3. send_tts_chunk emits full 6-field frame
# ---------------------------------------------------------------------------


async def test_send_tts_chunk_emits_full_frame():
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    # First, register a stream so the channel knows where TTS chunks go.
    await channel.send_delta("chat-A", "warmup", {"_stream_delta": True, "_stream_id": "s-1"})
    conn.sent.clear()

    chunk = TTSChunk(
        sequence=2,
        text="hello there",
        audio_base64="AAAA",
        emotion="happy",
        keyframes=[{"duration": 0.3, "targets": {"expression": 1.0}}],
    )
    await channel.send_tts_chunk(chunk)

    frames = _decode_frames(conn)
    assert frames == [{
        "event": "tts_chunk",
        "chat_id": "chat-A",
        "sequence": 2,
        "text": "hello there",
        "audio_base64": "AAAA",
        "emotion": "happy",
        "keyframes": [{"duration": 0.3, "targets": {"expression": 1.0}}],
    }]


# ---------------------------------------------------------------------------
# 4. Frame ordering: deltas × 3, tts_chunks × 2, stream_end preserved
# ---------------------------------------------------------------------------


async def test_frame_ordering_delta_tts_stream_end():
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    meta = {"_stream_delta": True, "_stream_id": "s-1"}
    await channel.send_delta("chat-A", "one ", meta)
    await channel.send_delta("chat-A", "two ", meta)
    await channel.send_delta("chat-A", "three", meta)

    await channel.send_tts_chunk(TTSChunk(sequence=0, text="one.", audio_base64=None, emotion=None, keyframes=[]))
    await channel.send_tts_chunk(TTSChunk(sequence=1, text="two.", audio_base64=None, emotion=None, keyframes=[]))

    end_msg = OutboundMessage(
        channel="desktop_mate",
        chat_id="chat-A",
        content="one two three",
        metadata={"_stream_end": True, "_stream_id": "s-1"},
    )
    await channel.send(end_msg)

    frames = _decode_frames(conn)
    events = [f["event"] for f in frames]
    assert events == ["delta", "delta", "delta", "tts_chunk", "tts_chunk", "stream_end"]
    assert frames[-1] == {
        "event": "stream_end",
        "chat_id": "chat-A",
        "content": "one two three",
    }


# ---------------------------------------------------------------------------
# 5. Inbound parsing: new_chat + message
# ---------------------------------------------------------------------------


async def test_inbound_new_chat_generates_chat_id_and_publishes():
    channel, bus = _make_channel()
    conn = FakeConnection(inbox=[
        json.dumps({"type": "new_chat", "content": "hello", "tts_enabled": True}),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    inbound = bus.inbound[0]
    assert inbound.channel == channel.name
    assert inbound.sender_id == "user-1"
    assert inbound.chat_id  # generated, non-empty
    assert inbound.content == "hello"
    assert inbound.metadata.get("tts_enabled") is True


async def test_inbound_message_uses_supplied_chat_id():
    channel, bus = _make_channel()
    conn = FakeConnection(inbox=[
        json.dumps({"type": "message", "chat_id": "chat-42", "content": "hi", "tts_enabled": False}),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    inbound = bus.inbound[0]
    assert inbound.chat_id == "chat-42"
    assert inbound.content == "hi"
    assert inbound.metadata.get("tts_enabled") is False


# ---------------------------------------------------------------------------
# 6. Auth: bad token rejected
# ---------------------------------------------------------------------------


async def test_bad_token_is_rejected():
    channel, _ = _make_channel(token="good-token")
    # Bogus connection; auth helper should return False (or close code).
    ok = channel._authorize_token("wrong-token")
    assert ok is False

    ok_none = channel._authorize_token(None)
    assert ok_none is False

    ok_good = channel._authorize_token("good-token")
    assert ok_good is True


async def test_handshake_closes_connection_with_4003_on_bad_token():
    channel, bus = _make_channel(token="good-token")
    conn = FakeConnection()

    # process_handshake simulates the server-side handshake gate.
    accepted = await channel._handshake(conn, query={"token": ["wrong"]})

    assert accepted is False
    assert conn.closed is True
    assert conn.close_code == 4003


# ---------------------------------------------------------------------------
# 7. Proactive flag passthrough
# ---------------------------------------------------------------------------


async def test_proactive_flag_included_in_stream_start():
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    msg = OutboundMessage(
        channel="desktop_mate",
        chat_id="chat-A",
        content="",
        metadata={"_stream_start": True, "_stream_id": "s-1", "proactive": True},
    )
    await channel.send(msg)

    frames = _decode_frames(conn)
    assert frames == [{"event": "stream_start", "chat_id": "chat-A", "proactive": True}]


async def test_proactive_flag_included_in_delta_and_tts_and_end():
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    await channel.send_delta(
        "chat-A",
        "hi",
        {"_stream_delta": True, "_stream_id": "s-1", "proactive": True},
    )
    # Register current stream for tts routing with proactive flag
    await channel.send_tts_chunk(TTSChunk(sequence=0, text="hi", audio_base64=None, emotion=None, keyframes=[]))

    await channel.send(OutboundMessage(
        channel="desktop_mate",
        chat_id="chat-A",
        content="hi",
        metadata={"_stream_end": True, "_stream_id": "s-1", "proactive": True},
    ))

    frames = _decode_frames(conn)
    assert all(f.get("proactive") is True for f in frames), frames


# ---------------------------------------------------------------------------
# 8. start()/stop() lifecycle on ephemeral port
# ---------------------------------------------------------------------------


async def test_start_stop_lifecycle_binds_and_cleans_up():
    channel, _ = _make_channel()
    task = asyncio.create_task(channel.start())
    # Give the server a moment to bind
    await asyncio.sleep(0.05)
    assert channel.is_running is True
    await channel.stop()
    # Await the runner task with a short timeout to ensure clean shutdown.
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        raise
    assert channel.is_running is False
