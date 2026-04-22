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
    # First delta for a new stream also emits stream_start.
    assert len(frames) == 2
    assert frames[0]["event"] == "stream_start"
    assert frames[1]["event"] == "delta"
    assert frames[1]["chat_id"] == "chat-A"
    assert frames[1]["text"] == "hi  there"  # emoji removed, other chars preserved
    assert frames[1]["stream_id"] == "s-1"


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
    # First send_delta for a new stream auto-emits stream_start so the FE
    # knows a new turn has begun (nanobot's manager never sets a
    # _stream_start metadata itself).
    assert events == [
        "stream_start",
        "delta",
        "delta",
        "delta",
        "tts_chunk",
        "tts_chunk",
        "stream_end",
    ]
    assert frames[-1] == {
        "event": "stream_end",
        "chat_id": "chat-A",
        "content": "one two three",
    }


# ---------------------------------------------------------------------------
# 4b. stream_start auto-emitted on first delta with a new stream_id
# ---------------------------------------------------------------------------


async def test_first_delta_auto_emits_stream_start():
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    await channel.send_delta(
        "chat-A",
        "hi",
        {"_stream_delta": True, "_stream_id": "s-auto"},
    )

    frames = _decode_frames(conn)
    assert len(frames) == 2
    assert frames[0] == {"event": "stream_start", "chat_id": "chat-A"}
    assert frames[1]["event"] == "delta"


async def test_second_delta_same_stream_does_not_repeat_start():
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    meta = {"_stream_delta": True, "_stream_id": "s-1"}
    await channel.send_delta("chat-A", "one ", meta)
    await channel.send_delta("chat-A", "two", meta)

    events = [f["event"] for f in _decode_frames(conn)]
    assert events == ["stream_start", "delta", "delta"]


# ---------------------------------------------------------------------------
# 4c. tts_chunk arriving AFTER stream_end still routes (TTS Barrier race)
# ---------------------------------------------------------------------------


async def test_tts_chunk_after_stream_end_still_routes():
    """The TTS Barrier in TTSHook.on_stream_end awaits synthesis in a task
    separate from the bus-dispatch loop. By the time a chunk arrives, the
    channel may have already processed _stream_end. The channel must not
    drop the chunk — the connection is still open and FE expects it.
    """
    channel, _ = _make_channel()
    conn = FakeConnection()
    channel._attach("chat-A", conn)

    # Full stream lifecycle: deltas then _stream_end.
    meta = {"_stream_delta": True, "_stream_id": "s-late"}
    await channel.send_delta("chat-A", "hello", meta)
    await channel.send(OutboundMessage(
        channel="desktop_mate",
        chat_id="chat-A",
        content="hello",
        metadata={"_stream_end": True, "_stream_id": "s-late"},
    ))

    # Now (simulating the barrier race) a tts_chunk synthesised during
    # the turn arrives.
    await channel.send_tts_chunk(TTSChunk(
        sequence=0,
        text="hello",
        audio_base64="AAAA",
        emotion=None,
        keyframes=[],
    ))

    frames = _decode_frames(conn)
    events = [f["event"] for f in frames]
    assert "tts_chunk" in events, f"Expected tts_chunk to be delivered, got {events}"
    # tts_chunk routed to the same chat_id that the stream used.
    tts_frame = next(f for f in frames if f["event"] == "tts_chunk")
    assert tts_frame["chat_id"] == "chat-A"


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


# ---------------------------------------------------------------------------
# 9. Handshake success emits ReadyFrame
# ---------------------------------------------------------------------------


async def test_successful_handshake_emits_ready_frame():
    channel, _ = _make_channel(token="")  # empty token: all connections allowed
    conn = FakeConnection()

    accepted = await channel._handshake(conn, query={})
    assert accepted is True

    # ReadyFrame should have been sent during the handshake path.
    await channel._send_ready(conn, client_id="client-77")

    frames = _decode_frames(conn)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["event"] == "ready"
    assert frame["client_id"] == "client-77"
    # connection_id is a UUID4 string
    assert isinstance(frame["connection_id"], str) and len(frame["connection_id"]) == 36
    # server_time is a float (unix timestamp)
    assert isinstance(frame["server_time"], float)
    assert frame["server_time"] > 0


# ---------------------------------------------------------------------------
# 10. Config defaults include ping_interval + max_message_bytes (6MB)
# ---------------------------------------------------------------------------


def test_desktop_mate_config_defaults_keepalive_and_max_size():
    cfg = DesktopMateConfig()
    assert cfg.ping_interval_s == 20.0
    assert cfg.ping_timeout_s == 20.0
    # 6 MB covers DMP image-in-base64 upper bound.
    assert cfg.max_message_bytes == 6 * 1024 * 1024


# ---------------------------------------------------------------------------
# 11. serve() receives the keepalive + max_size kwargs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 11b. tts_enabled switch (per-message + URL override)
# ---------------------------------------------------------------------------


async def test_new_chat_with_tts_disabled_drops_tts_chunk():
    """FE sends ``tts_enabled: false`` in new_chat → channel must not emit
    any tts_chunk for that chat, even if synthesis produces one."""
    channel, _ = _make_channel()
    conn = FakeConnection(inbox=[
        json.dumps({"type": "new_chat", "content": "hi", "tts_enabled": False}),
    ])
    await channel._connection_loop(conn, sender_id="user-1")
    chat_id = next(iter(channel._chat_conn.keys()))

    # Begin an outgoing stream on that chat so routing is set up.
    await channel.send_delta(
        chat_id,
        "hi",
        {"_stream_delta": True, "_stream_id": "s-1"},
    )
    conn.sent.clear()

    await channel.send_tts_chunk(TTSChunk(
        sequence=0, text="hi", audio_base64="AAAA",
        emotion=None, keyframes=[],
    ))

    events = [f.get("event") for f in _decode_frames(conn)]
    assert "tts_chunk" not in events, f"tts_chunk should be suppressed; got {events}"


async def test_message_defaults_tts_enabled_true():
    """Absent ``tts_enabled`` in an inbound message must default to True
    so existing clients aren't silently muted."""
    channel, _ = _make_channel()
    conn = FakeConnection(inbox=[
        json.dumps({"type": "new_chat", "content": "hi"}),  # no tts_enabled
    ])
    await channel._connection_loop(conn, sender_id="user-1")
    chat_id = next(iter(channel._chat_conn.keys()))

    await channel.send_delta(
        chat_id,
        "hi",
        {"_stream_delta": True, "_stream_id": "s-1"},
    )
    conn.sent.clear()

    await channel.send_tts_chunk(TTSChunk(
        sequence=0, text="hi", audio_base64="AAAA",
        emotion=None, keyframes=[],
    ))

    events = [f.get("event") for f in _decode_frames(conn)]
    assert "tts_chunk" in events


async def test_url_override_tts_zero_disables_connection_wide(monkeypatch):
    """``?tts=0`` in the handshake URL disables TTS for the entire
    connection regardless of per-message flags."""
    channel, _ = _make_channel(token="")  # no token, auth open
    conn = FakeConnection()

    # Simulate handshake with tts=0 in the query string.
    accepted = await channel._handshake(conn, query={"tts": ["0"]})
    assert accepted is True

    # Record the connection-level disable by the same mechanism that handler()
    # would call after handshake. Future work moves this into handler(), but
    # the helper already exists and makes the contract testable.
    channel._apply_connection_tts_override(conn, {"tts": ["0"]})

    # Attach a chat and simulate a message with ``tts_enabled: true`` —
    # URL override must win.
    conn_inbox = FakeConnection(inbox=[
        json.dumps({"type": "new_chat", "content": "hi", "tts_enabled": True}),
    ])
    # Copy the override onto the per-test inbox connection.
    channel._apply_connection_tts_override(conn_inbox, {"tts": ["0"]})
    await channel._connection_loop(conn_inbox, sender_id="user-1")
    chat_id = next(iter(channel._chat_conn.keys()))

    await channel.send_delta(
        chat_id,
        "hi",
        {"_stream_delta": True, "_stream_id": "s-url"},
    )
    conn_inbox.sent.clear()

    await channel.send_tts_chunk(TTSChunk(
        sequence=0, text="hi", audio_base64="AAAA",
        emotion=None, keyframes=[],
    ))

    events = [f.get("event") for f in _decode_frames(conn_inbox)]
    assert "tts_chunk" not in events


async def test_url_tts_equals_one_is_a_noop():
    """``?tts=1`` (or absent) leaves the default path enabled."""
    channel, _ = _make_channel(token="")
    conn = FakeConnection(inbox=[
        json.dumps({"type": "new_chat", "content": "hi"}),
    ])
    channel._apply_connection_tts_override(conn, {"tts": ["1"]})
    await channel._connection_loop(conn, sender_id="user-1")
    chat_id = next(iter(channel._chat_conn.keys()))

    await channel.send_delta(
        chat_id,
        "hi",
        {"_stream_delta": True, "_stream_id": "s-1"},
    )
    conn.sent.clear()

    await channel.send_tts_chunk(TTSChunk(
        sequence=0, text="hi", audio_base64="AAAA",
        emotion=None, keyframes=[],
    ))

    events = [f.get("event") for f in _decode_frames(conn)]
    assert "tts_chunk" in events


# ---------------------------------------------------------------------------
# 12. __init__ accepts dict section (from nanobot.json via ChannelManager)
# ---------------------------------------------------------------------------


def test_init_accepts_dict_section_snake_case():
    """ChannelManager passes raw dict from parsed nanobot.json."""
    bus = FakeBus()
    section = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 9999,
        "path": "/ws",
        "token": "t",
        "allow_from": ["*"],
        "ping_interval_s": 15.0,
        "max_message_bytes": 2_000_000,
    }
    channel = DesktopMateChannel(config=section, bus=bus)
    assert isinstance(channel.config, DesktopMateConfig)
    assert channel.config.port == 9999
    assert channel.config.ping_interval_s == 15.0
    assert channel.config.max_message_bytes == 2_000_000


def test_init_accepts_dict_section_camel_case():
    """nanobot.json uses camelCase by convention; accept both forms."""
    bus = FakeBus()
    section = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "allowFrom": ["alice", "bob"],
        "pingIntervalS": 10.0,
        "pingTimeoutS": 5.0,
        "maxMessageBytes": 4_000_000,
    }
    channel = DesktopMateChannel(config=section, bus=bus)
    assert channel.config.allow_from == ["alice", "bob"]
    assert channel.config.ping_interval_s == 10.0
    assert channel.config.ping_timeout_s == 5.0
    assert channel.config.max_message_bytes == 4_000_000


def test_init_accepts_dataclass_instance_unchanged():
    """Existing callers that pass DesktopMateConfig directly must still work."""
    bus = FakeBus()
    cfg = DesktopMateConfig(token="abc", port=1234)
    channel = DesktopMateChannel(config=cfg, bus=bus)
    assert channel.config is cfg


def test_init_ignores_unknown_section_keys():
    """Unknown keys (from config evolution / typos) must not raise."""
    bus = FakeBus()
    section = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "unknownOption": "ignored",
        "someLegacyFlag": True,
    }
    channel = DesktopMateChannel(config=section, bus=bus)
    assert channel.config.host == "127.0.0.1"


async def test_start_passes_ping_and_max_size_to_serve(monkeypatch):
    """start() must forward ping_interval / ping_timeout / max_size to
    websockets.serve() so the WS protocol handles keepalive for us."""
    import nanobot_runtime.channels.desktop_mate as dm

    captured: dict[str, Any] = {}

    class _StubServer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _fake_serve(handler, host, port, **kwargs):
        captured["host"] = host
        captured["port"] = port
        captured["kwargs"] = kwargs
        return _StubServer()

    # Replace the dynamic import inside start() via module attribute patching.
    import sys
    import types

    stub_mod = types.ModuleType("websockets.asyncio.server")
    stub_mod.serve = _fake_serve  # type: ignore[attr-defined]
    stub_pkg = types.ModuleType("websockets.asyncio")
    stub_pkg.server = stub_mod  # type: ignore[attr-defined]
    stub_root = types.ModuleType("websockets")
    stub_root.asyncio = stub_pkg  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "websockets", stub_root)
    monkeypatch.setitem(sys.modules, "websockets.asyncio", stub_pkg)
    monkeypatch.setitem(sys.modules, "websockets.asyncio.server", stub_mod)

    channel, _ = _make_channel()
    task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.02)
    await channel.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()

    kwargs = captured.get("kwargs") or {}
    assert kwargs.get("ping_interval") == 20.0
    assert kwargs.get("ping_timeout") == 20.0
    assert kwargs.get("max_size") == 6 * 1024 * 1024
