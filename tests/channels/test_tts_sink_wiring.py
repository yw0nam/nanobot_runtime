"""Tests for the channel registry + lazy TTS sink.

The hook that dispatches TTS chunks is constructed per-AgentLoop, but the
DesktopMateChannel is a singleton created by nanobot's ChannelManager on
a separate code path. This shim resolves the channel on-demand so the
two construction orders don't have to be coordinated.
"""
from __future__ import annotations

from typing import Any

import pytest

from nanobot_runtime.channels.desktop_mate import (
    DesktopMateChannel,
    DesktopMateConfig,
    LazyChannelTTSSink,
    _reset_registry_for_tests,
    get_desktop_mate_channel,
)
from nanobot_runtime.hooks.tts import TTSChunk


class _FakeBus:
    async def publish_inbound(self, _msg: Any) -> None: ...
    async def publish_outbound(self, _msg: Any) -> None: ...


def setup_function(_fn):
    _reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_get_channel_raises_before_any_instance_created():
    with pytest.raises(RuntimeError, match="DesktopMateChannel"):
        get_desktop_mate_channel()


def test_channel_registers_itself_on_construction():
    channel = DesktopMateChannel(DesktopMateConfig(), _FakeBus())
    assert get_desktop_mate_channel() is channel


def test_latest_channel_overrides_previous():
    """Re-init (e.g. hot reload / test rerun) replaces the registry entry."""
    first = DesktopMateChannel(DesktopMateConfig(port=1111), _FakeBus())
    second = DesktopMateChannel(DesktopMateConfig(port=2222), _FakeBus())
    assert get_desktop_mate_channel() is second
    assert first is not second


# ---------------------------------------------------------------------------
# Lazy TTS sink
# ---------------------------------------------------------------------------


async def test_lazy_sink_drops_chunk_when_channel_absent(caplog):
    """If ChannelManager hasn't built the channel yet, dropping must be silent
    at INFO level (FE will simply miss audio for that stream) rather than
    raising and breaking the agent loop."""
    sink = LazyChannelTTSSink()
    chunk = TTSChunk(
        sequence=0,
        text="hi",
        audio_base64=None,
        emotion=None,
        keyframes=[],
    )
    # Must not raise.
    await sink.send_tts_chunk(chunk)


async def test_lazy_sink_is_enabled_defaults_true_when_no_channel():
    """Before any channel is constructed, default is 'enabled' so the
    hook doesn't accidentally suppress TTS in test setups."""
    sink = LazyChannelTTSSink()
    assert sink.is_enabled() is True


def test_channel_reports_tts_enabled_via_current_stream():
    """DesktopMateChannel resolves current stream → chat_id → tts flag."""
    channel = DesktopMateChannel(DesktopMateConfig(), _FakeBus())
    channel._chat_conn["chat-A"] = object()
    channel._streams["s-1"] = ("chat-A", False)
    channel._current_stream_id = "s-1"

    # Default: enabled
    assert channel.is_tts_enabled_for_current_stream() is True

    # Per-chat disable
    channel._tts_enabled_per_chat["chat-A"] = False
    assert channel.is_tts_enabled_for_current_stream() is False

    # URL override re-enables even if per-chat is False
    conn = channel._chat_conn["chat-A"]
    channel._tts_enabled_per_conn[id(conn)] = True
    assert channel.is_tts_enabled_for_current_stream() is True


def test_channel_tts_enabled_is_false_when_no_active_stream():
    """If no desktop_mate stream is currently registered, return False so
    TTSHook does not synthesise for turns that belong to a different
    channel (e.g. the idle-watcher firing through ``channel=cli``).

    Previously the default was True with the rationale "hook will have
    something to ask about when the first delta arrives anyway". Phase 5
    broke that assumption by introducing cross-channel concurrency — see
    ``src/.../channels/desktop_mate.py::is_tts_enabled_for_current_stream``
    for the full context.
    """
    channel = DesktopMateChannel(DesktopMateConfig(), _FakeBus())
    assert channel.is_tts_enabled_for_current_stream() is False


async def test_lazy_sink_is_enabled_forwards_to_channel():
    channel = DesktopMateChannel(DesktopMateConfig(), _FakeBus())
    channel._chat_conn["chat-A"] = object()
    channel._streams["s-1"] = ("chat-A", False)
    channel._current_stream_id = "s-1"
    channel._tts_enabled_per_chat["chat-A"] = False

    sink = LazyChannelTTSSink()
    assert sink.is_enabled() is False


async def test_lazy_sink_forwards_to_registered_channel():
    channel = DesktopMateChannel(DesktopMateConfig(), _FakeBus())

    # Simulate an active stream so send_tts_chunk knows where to route.
    class _Conn:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, raw: str) -> None:
            self.sent.append(raw)

    conn = _Conn()
    channel._attach("chat-A", conn)
    channel._streams["s-1"] = ("chat-A", False)
    channel._current_stream_id = "s-1"

    sink = LazyChannelTTSSink()
    await sink.send_tts_chunk(TTSChunk(
        sequence=3,
        text="hello",
        audio_base64="QUJDRA==",
        emotion="happy",
        keyframes=[],
    ))

    import json
    assert len(conn.sent) == 1
    frame = json.loads(conn.sent[0])
    assert frame["event"] == "tts_chunk"
    assert frame["sequence"] == 3
    assert frame["text"] == "hello"
