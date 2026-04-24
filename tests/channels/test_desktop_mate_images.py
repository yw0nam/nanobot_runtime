"""Tests for image intake on DesktopMateChannel.

Covers the FE→server path added in #8: inbound ``new_chat`` / ``message``
frames carry an optional ``images: list[str]`` field, where each entry is
a ``data:<mime>;base64,<payload>`` URL. On success the channel decodes
each entry to disk and forwards the resulting local paths through
``BaseChannel._handle_message(..., media=...)``. On any failure the
channel rejects the whole turn with an ``image_rejected`` error frame
and does **not** publish to the bus.

Uses the same ``FakeConnection`` + ``FakeBus`` shape as
``test_desktop_mate.py`` to keep the fixtures comparable.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot_runtime.channels.desktop_mate import (
    DesktopMateChannel,
    DesktopMateConfig,
)
from nanobot_runtime.channels.desktop_mate_protocol import (
    InboundEnvelope,
    MessageFrame,
    NewChatFrame,
    parse_inbound,
)
from pydantic import ValidationError


# 1x1 transparent PNG — smallest legal image payload, reused across tests.
_TINY_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO"
    b"+ip1sAAAAASUVORK5CYII="
)
_TINY_PNG_B64 = base64.b64encode(_TINY_PNG_BYTES).decode("ascii")
TINY_PNG_DATA_URL = f"data:image/png;base64,{_TINY_PNG_B64}"
TINY_JPG_DATA_URL = (
    "data:image/jpeg;base64,"
    + base64.b64encode(b"\xff\xd8\xff\xd9").decode("ascii")
)


# ---------------------------------------------------------------------------
# Fakes (mirror test_desktop_mate.py so fixtures read consistently)
# ---------------------------------------------------------------------------


class FakeConnection:
    def __init__(
        self,
        inbox: list[str] | None = None,
        remote: tuple[str, int] = ("127.0.0.1", 12345),
    ) -> None:
        self.sent: list[str] = []
        self.inbox: list[str] = list(inbox or [])
        self.remote_address = remote
        self.closed = False

    async def send(self, raw: str) -> None:
        if self.closed:
            raise RuntimeError("connection closed")
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def __aiter__(self):
        async def _gen():
            for item in list(self.inbox):
                yield item
        return _gen()


class FakeBus:
    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []
        self.outbound: list[OutboundMessage] = []

    async def publish_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.outbound.append(msg)


def _make_channel(tmp_path: Path) -> tuple[DesktopMateChannel, FakeBus]:
    bus = FakeBus()
    cfg = DesktopMateConfig(
        token="",
        allow_from=["*"],
        host="127.0.0.1",
        port=0,
    )
    channel = DesktopMateChannel(config=cfg, bus=bus)
    # Re-point media dir so tests don't dirty the user's ~/.nanobot/.
    channel._media_dir = tmp_path / "media"
    channel._media_dir.mkdir(parents=True, exist_ok=True)
    return channel, bus


def _decode_frames(conn: FakeConnection) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in conn.sent]


# ---------------------------------------------------------------------------
# Protocol layer: images field on the inbound frames
# ---------------------------------------------------------------------------


def test_inbound_message_accepts_images_field() -> None:
    raw = json.dumps({
        "type": "message",
        "chat_id": "chat-1",
        "content": "see",
        "images": [TINY_PNG_DATA_URL],
    })
    env = parse_inbound(raw)
    assert isinstance(env, MessageFrame)
    assert env.images == [TINY_PNG_DATA_URL]


def test_inbound_new_chat_accepts_images_field() -> None:
    raw = json.dumps({
        "type": "new_chat",
        "content": "see",
        "images": [TINY_PNG_DATA_URL, TINY_JPG_DATA_URL],
    })
    env = parse_inbound(raw)
    assert isinstance(env, NewChatFrame)
    assert env.images == [TINY_PNG_DATA_URL, TINY_JPG_DATA_URL]


def test_inbound_images_defaults_to_none() -> None:
    env = InboundEnvelope.validate_python(
        {"type": "message", "chat_id": "c-1", "content": "hi"}
    )
    assert env.images is None


def test_inbound_images_schema_does_not_cap_count() -> None:
    """Count enforcement lives at the channel layer, not Pydantic.

    A Pydantic ``max_length`` would raise ``ValidationError`` at parse
    time and be silently dropped by the inbound loop — the FE would never
    learn the turn was rejected. Regression: make sure nobody reintroduces
    a schema-level cap.
    """
    raw = json.dumps({
        "type": "message",
        "chat_id": "c-1",
        "content": "hi",
        "images": [TINY_PNG_DATA_URL] * 5,
    })
    env = parse_inbound(raw)
    assert isinstance(env, MessageFrame)
    assert env.images is not None and len(env.images) == 5


def test_inbound_images_rejects_wrong_item_type() -> None:
    raw = json.dumps({
        "type": "message",
        "chat_id": "c-1",
        "content": "hi",
        "images": [{"url": TINY_PNG_DATA_URL}],
    })
    with pytest.raises(ValidationError):
        parse_inbound(raw)


# ---------------------------------------------------------------------------
# Channel wiring: happy path
# ---------------------------------------------------------------------------


async def test_single_valid_image_is_persisted_and_forwarded(tmp_path: Path) -> None:
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "new_chat",
            "content": "describe",
            "images": [TINY_PNG_DATA_URL],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    inbound = bus.inbound[0]
    assert inbound.content == "describe"
    assert len(inbound.media) == 1
    path = Path(inbound.media[0])
    assert path.is_file()
    assert path.read_bytes() == _TINY_PNG_BYTES


async def test_multiple_valid_images_preserve_order(tmp_path: Path) -> None:
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [TINY_PNG_DATA_URL, TINY_JPG_DATA_URL],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    media = bus.inbound[0].media
    assert len(media) == 2
    # Order must match the inbound list so downstream can reason about
    # which image corresponds to which mention in the text.
    assert Path(media[0]).suffix == ".png"
    assert Path(media[1]).suffix in (".jpe", ".jpeg", ".jpg")


async def test_image_only_turn_accepted(tmp_path: Path) -> None:
    channel, bus = _make_channel(tmp_path)
    # content is blank-ish — allowed so long as at least one image rides along.
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": " ",  # whitespace-only
            "images": [TINY_PNG_DATA_URL],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    assert len(bus.inbound[0].media) == 1


async def test_images_absent_is_no_op(tmp_path: Path) -> None:
    """Regression: omitting ``images`` keeps the legacy text-only path."""
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({"type": "new_chat", "content": "hi"}),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    assert bus.inbound[0].media == []
    # No image_rejected frame emitted — just silent-happy path.
    events = [f.get("event") for f in _decode_frames(conn)]
    assert "image_rejected" not in events


async def test_images_null_is_no_op(tmp_path: Path) -> None:
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({"type": "new_chat", "content": "hi", "images": None}),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    assert bus.inbound[0].media == []


# ---------------------------------------------------------------------------
# Channel wiring: rejection paths
# ---------------------------------------------------------------------------


async def test_malformed_data_url_rejected(tmp_path: Path) -> None:
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            # Missing the ``data:<mime>;base64,`` prefix — raw base64 only.
            "images": [_TINY_PNG_B64],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert bus.inbound == []
    frames = _decode_frames(conn)
    assert frames, "channel must emit an image_rejected frame"
    assert frames[-1]["event"] == "image_rejected"
    assert frames[-1]["reason"] == "malformed"


async def test_oversized_image_rejected(tmp_path: Path) -> None:
    channel, bus = _make_channel(tmp_path)
    # Force the per-image cap very low so we don't actually allocate ~10MB
    # just to test the guard.
    channel._max_image_bytes = 32
    big_payload = base64.b64encode(b"X" * 256).decode("ascii")
    big_url = f"data:image/png;base64,{big_payload}"
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [big_url],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert bus.inbound == []
    frames = _decode_frames(conn)
    assert frames[-1]["event"] == "image_rejected"
    assert frames[-1]["reason"] == "too_large"


async def test_unsupported_mime_rejected(tmp_path: Path) -> None:
    channel, bus = _make_channel(tmp_path)
    svg_url = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [svg_url],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert bus.inbound == []
    frames = _decode_frames(conn)
    assert frames[-1]["event"] == "image_rejected"
    assert frames[-1]["reason"] == "unsupported_mime"


async def test_too_many_images_emits_rejection_frame(tmp_path: Path) -> None:
    """5 images ⇒ ``too_many`` rejection frame, not silent drop.

    Guards against the dead-code regression where a Pydantic ``max_length``
    would raise ``ValidationError`` pre-channel and be swallowed by the
    inbound loop's generic handler — leaving FE without any rejection.
    """
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [TINY_PNG_DATA_URL] * 5,
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert bus.inbound == []
    frames = _decode_frames(conn)
    assert frames[-1]["event"] == "image_rejected"
    assert frames[-1]["reason"] == "too_many"
    # Nothing should have been written to disk yet — the count check
    # short-circuits before any decode work begins.
    assert list((tmp_path / "media").iterdir()) == []


async def test_partial_ingress_cleanup_on_later_failure(tmp_path: Path) -> None:
    """First image succeeds, second exceeds cap ⇒ first file must be unlinked.

    The channel promises whole-turn rejection with no partial state on
    disk. Without explicit coverage a future refactor could silently drop
    the ``_abort`` unlink loop and accumulate leaked bytes per rejection.
    """
    channel, bus = _make_channel(tmp_path)
    channel._max_image_bytes = 32
    oversized_payload = base64.b64encode(b"X" * 256).decode("ascii")
    oversized_url = f"data:image/png;base64,{oversized_payload}"
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [TINY_PNG_DATA_URL, oversized_url],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert bus.inbound == []
    frames = _decode_frames(conn)
    assert frames[-1]["reason"] == "too_large"
    # Crucial: the earlier-decoded tiny PNG must not be left behind.
    media_dir = tmp_path / "media"
    assert list(media_dir.iterdir()) == []


async def test_io_error_during_persist_surfaces_as_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk-write failures are ``io_error``, not ``malformed``.

    Masking OSError as malformed misleads operators into debugging user
    input when the actual fault is server-side (disk full, permission).
    """
    channel, bus = _make_channel(tmp_path)

    def _raise_oserror(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk write failed (simulated)")

    monkeypatch.setattr(
        "nanobot_runtime.channels.desktop_mate.save_base64_data_url",
        _raise_oserror,
    )
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [TINY_PNG_DATA_URL],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert bus.inbound == []
    frames = _decode_frames(conn)
    assert frames[-1]["event"] == "image_rejected"
    assert frames[-1]["reason"] == "io_error"


async def test_reference_id_echoed_on_rejection(tmp_path: Path) -> None:
    """FE needs ``reference_id`` to correlate rejections with pending sends.

    Especially important for ``new_chat`` where ``chat_id`` is ``None`` on
    rejection (session was never created) — without reference_id the FE
    has no way to match the rejection to an in-flight request.
    """
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "new_chat",
            "content": "see",
            "reference_id": "req-42",
            "images": [_TINY_PNG_B64],  # missing data: prefix => malformed
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    frames = _decode_frames(conn)
    rejection = frames[-1]
    assert rejection["event"] == "image_rejected"
    assert rejection["reason"] == "malformed"
    assert rejection["reference_id"] == "req-42"
    assert rejection.get("chat_id") is None


async def test_exact_boundary_four_images_accepted(tmp_path: Path) -> None:
    """Inclusive count cap: exactly ``_MAX_IMAGES_PER_MESSAGE`` must pass.

    Guards against an off-by-one flip (``>=`` vs ``>``) that the
    over-cap test alone would not catch.
    """
    channel, bus = _make_channel(tmp_path)
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [TINY_PNG_DATA_URL] * 4,
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert len(bus.inbound) == 1
    assert len(bus.inbound[0].media) == 4
    frames = _decode_frames(conn)
    assert not any(f.get("event") == "image_rejected" for f in frames)


async def test_decoded_media_unlinked_when_handle_message_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downstream failure must not strand decoded images on disk.

    ``BaseChannel._handle_message`` can raise (bus failure) or silently
    drop (``is_allowed`` check returns False). Either way, files decoded
    before the hand-off must be cleaned up so repeated failures don't
    accumulate per-turn garbage.
    """
    channel, bus = _make_channel(tmp_path)

    async def _raising_handle_message(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("bus publish failed (simulated)")

    monkeypatch.setattr(channel, "_handle_message", _raising_handle_message)
    conn = FakeConnection(inbox=[
        json.dumps({
            "type": "message",
            "chat_id": "chat-1",
            "content": "see",
            "images": [TINY_PNG_DATA_URL],
        }),
    ])

    await channel._connection_loop(conn, sender_id="user-1")

    assert bus.inbound == []
    assert list((tmp_path / "media").iterdir()) == []
