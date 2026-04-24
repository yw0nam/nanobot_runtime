"""Tests for DesktopMate frame protocol (Pydantic models).

These pin down the exact wire format expected by the DMP frontend:
outbound frames (stream_start/delta/stream_end/tts_chunk) and inbound
envelopes (new_chat/message). The channel implementation is expected to
build these models and serialise with ``model_dump_json(exclude_none=True)``.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nanobot_runtime.channels.desktop_mate_protocol import (
    DeltaFrame,
    ImageRejectedFrame,
    InboundEnvelope,
    MessageFrame,
    NewChatFrame,
    ReadyFrame,
    StreamEndFrame,
    StreamStartFrame,
    TTSChunkFrame,
    parse_inbound,
)


# ---------------------------------------------------------------------------
# Outbound — ready
# ---------------------------------------------------------------------------


def test_ready_frame_serialises_fully():
    frame = ReadyFrame(
        connection_id="11111111-1111-1111-1111-111111111111",
        client_id="user-1",
        server_time=1_700_000_000.5,
    )
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {
        "event": "ready",
        "connection_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "user-1",
        "server_time": 1_700_000_000.5,
    }


def test_ready_frame_event_is_literal():
    with pytest.raises(ValidationError):
        ReadyFrame(  # type: ignore[call-arg]
            event="welcome",
            connection_id="x",
            client_id="y",
            server_time=0.0,
        )


# ---------------------------------------------------------------------------
# Outbound — stream_start
# ---------------------------------------------------------------------------


def test_stream_start_minimal_omits_proactive():
    frame = StreamStartFrame(chat_id="chat-A")
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {"event": "stream_start", "chat_id": "chat-A"}


def test_stream_start_with_proactive():
    frame = StreamStartFrame(chat_id="chat-A", proactive=True)
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {"event": "stream_start", "chat_id": "chat-A", "proactive": True}


def test_stream_start_event_is_literal():
    # Attempting to override event should fail validation.
    with pytest.raises(ValidationError):
        StreamStartFrame(event="stream_end", chat_id="chat-A")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Outbound — delta
# ---------------------------------------------------------------------------


def test_delta_minimal():
    frame = DeltaFrame(chat_id="chat-A", text="hello")
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {"event": "delta", "chat_id": "chat-A", "text": "hello"}


def test_delta_with_stream_id_and_proactive():
    frame = DeltaFrame(
        chat_id="chat-A",
        text="hi",
        stream_id="s-1",
        proactive=True,
    )
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {
        "event": "delta",
        "chat_id": "chat-A",
        "text": "hi",
        "stream_id": "s-1",
        "proactive": True,
    }


# ---------------------------------------------------------------------------
# Outbound — stream_end
# ---------------------------------------------------------------------------


def test_stream_end_carries_full_content():
    frame = StreamEndFrame(chat_id="chat-A", content="hello world")
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {
        "event": "stream_end",
        "chat_id": "chat-A",
        "content": "hello world",
    }


def test_stream_end_with_proactive():
    frame = StreamEndFrame(chat_id="chat-A", content="yo", proactive=True)
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {
        "event": "stream_end",
        "chat_id": "chat-A",
        "content": "yo",
        "proactive": True,
    }


# ---------------------------------------------------------------------------
# Outbound — tts_chunk
# ---------------------------------------------------------------------------


def test_tts_chunk_full_payload():
    frame = TTSChunkFrame(
        chat_id="chat-A",
        sequence=2,
        text="hello there",
        audio_base64="AAAA",
        emotion="happy",
        keyframes=[{"duration": 0.3, "targets": {"expression": 1.0}}],
    )
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {
        "event": "tts_chunk",
        "chat_id": "chat-A",
        "sequence": 2,
        "text": "hello there",
        "audio_base64": "AAAA",
        "emotion": "happy",
        "keyframes": [{"duration": 0.3, "targets": {"expression": 1.0}}],
    }


def test_tts_chunk_null_audio_retained():
    # audio_base64=None must serialise as explicit null (FE relies on it as a
    # "TTS failed, play silence" signal). exclude_none would drop it — we must
    # special-case this field.
    frame = TTSChunkFrame(
        chat_id="chat-A",
        sequence=0,
        text="hi",
        audio_base64=None,
        emotion=None,
        keyframes=[],
    )
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload["audio_base64"] is None
    assert payload["emotion"] is None
    assert payload["keyframes"] == []


def test_tts_chunk_with_proactive():
    frame = TTSChunkFrame(
        chat_id="chat-A",
        sequence=0,
        text="hi",
        audio_base64=None,
        emotion=None,
        keyframes=[],
        proactive=True,
    )
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload["proactive"] is True


# ---------------------------------------------------------------------------
# Inbound — new_chat / message discriminated union
# ---------------------------------------------------------------------------


def test_parse_inbound_new_chat():
    raw = json.dumps({"type": "new_chat", "content": "hello", "tts_enabled": True})
    env = parse_inbound(raw)
    assert isinstance(env, NewChatFrame)
    assert env.content == "hello"
    assert env.tts_enabled is True
    assert env.reference_id is None


def test_parse_inbound_message_requires_chat_id():
    raw = json.dumps({"type": "message", "content": "hi"})
    with pytest.raises(ValidationError):
        parse_inbound(raw)


def test_parse_inbound_message_with_chat_id():
    raw = json.dumps({
        "type": "message",
        "chat_id": "chat-42",
        "content": "hi",
        "tts_enabled": False,
        "reference_id": "ref-1",
    })
    env = parse_inbound(raw)
    assert isinstance(env, MessageFrame)
    assert env.chat_id == "chat-42"
    assert env.content == "hi"
    assert env.tts_enabled is False
    assert env.reference_id == "ref-1"


def test_parse_inbound_defaults_tts_enabled_true():
    raw = json.dumps({"type": "new_chat", "content": "hello"})
    env = parse_inbound(raw)
    assert env.tts_enabled is True


def test_parse_inbound_rejects_unknown_type():
    raw = json.dumps({"type": "authorize", "content": "legacy"})
    with pytest.raises(ValidationError):
        parse_inbound(raw)


def test_parse_inbound_rejects_empty_content():
    raw = json.dumps({"type": "new_chat", "content": "   "})
    with pytest.raises(ValidationError):
        parse_inbound(raw)


def test_parse_inbound_bad_json_raises():
    with pytest.raises((ValidationError, ValueError)):
        parse_inbound("not json{{")


def test_image_rejected_frame_serialises_minimal():
    frame = ImageRejectedFrame(reason="too_large")
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {"event": "image_rejected", "reason": "too_large"}


def test_image_rejected_frame_with_chat_id():
    frame = ImageRejectedFrame(chat_id="chat-A", reason="malformed")
    payload = json.loads(frame.model_dump_json(exclude_none=True))
    assert payload == {
        "event": "image_rejected",
        "chat_id": "chat-A",
        "reason": "malformed",
    }


def test_inbound_envelope_is_tagged_union():
    # Positive control: InboundEnvelope should discriminate on `type`.
    new_chat = InboundEnvelope.validate_python(
        {"type": "new_chat", "content": "hello"}
    )
    assert isinstance(new_chat, NewChatFrame)

    msg = InboundEnvelope.validate_python(
        {"type": "message", "chat_id": "c-1", "content": "hi"}
    )
    assert isinstance(msg, MessageFrame)
