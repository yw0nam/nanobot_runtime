"""DesktopMateChannel — DMP-compatible WebSocket channel + TTSSink.

TTSSink routing: ``send_tts_chunk`` does not pass ``chat_id``. The channel
tracks the currently active stream via ``(stream_id → (chat_id, conn))``
registered on the first ``send_delta`` for a new stream, or on a
``_stream_start``-tagged ``send()`` call. Routes TTS chunks to the latest.
Stream entries are kept past ``stream_end`` so late TTS chunks (arriving
via the TTS Barrier) still have a chat_id to route to.

Auth: single static token against ``?token=`` in the WS URL.
"""
import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel
from websockets.http11 import Request as WsRequest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

from nanobot_runtime.config.desktop_mate import DesktopMateConfig, _coerce_config
from nanobot_runtime.services.channels.desktop_mate_image import _MAX_IMAGE_BYTES, _decode_images
from nanobot_runtime.models.desktop_mate import (
    DeltaFrame,
    ImageRejectReason,
    ReadyFrame,
    StreamEndFrame,
    StreamStartFrame,
)
from nanobot_runtime.clients.desktop_mate_rest import dispatch_http, parse_request_path, query_first
from nanobot_runtime.services.channels.desktop_mate_server import _DesktopMateServerMixin
from nanobot_runtime.services.channels.desktop_mate_tts import _DesktopMateTTSMixin
from nanobot_runtime.services.hooks.tts import TTSChunk
from nanobot_runtime.services.tts.emotion_mapper import EmotionMapper


# ── Registry ─────────────────────────────────────────────────────────────

# Single-process registry so LazyChannelTTSSink can resolve the channel at
# send time without a shared construction reference between the channel
# manager and the hook factory.
_LATEST_CHANNEL: "DesktopMateChannel | None" = None


def get_desktop_mate_channel() -> "DesktopMateChannel":
    """Return the active channel or raise if none has been constructed yet."""
    if _LATEST_CHANNEL is None:
        raise RuntimeError(
            "DesktopMateChannel has not been constructed — check that "
            "channels.desktop_mate is enabled in nanobot.json."
        )
    return _LATEST_CHANNEL


def _reset_registry_for_tests() -> None:
    """Test-only: wipe the registry between cases to avoid leakage."""
    global _LATEST_CHANNEL
    _LATEST_CHANNEL = None


# ── Channel ──────────────────────────────────────────────────────────────


class DesktopMateChannel(_DesktopMateTTSMixin, _DesktopMateServerMixin, BaseChannel):
    """WebSocket channel implementing the DMP-compatible FE protocol."""

    name = "desktop_mate"
    display_name = "DesktopMate"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        emotion_emojis: set[str] | None = None,
        session_manager: Any | None = None,
    ) -> None:
        coerced = _coerce_config(config)
        super().__init__(coerced, bus)
        self.config: DesktopMateConfig = coerced
        # Injected by the gateway's ChannelManager monkey-patch; used by the
        # REST routes. ``None`` is tolerated so unit tests can construct the
        # channel in isolation — the routes 503 in that case.
        self._session_manager = session_manager
        global _LATEST_CHANNEL
        _LATEST_CHANNEL = self
        # Emoji-stripping: explicit kwarg (tests) wins over config YAML.
        if emotion_emojis is not None:
            self._emotion_emojis: frozenset[str] = frozenset(emotion_emojis)
        elif coerced.emotion_map_path:
            self._emotion_emojis = EmotionMapper.from_yaml(coerced.emotion_map_path).known_emojis
        else:
            self._emotion_emojis = frozenset()
        self._emotion_strip_re: re.Pattern[str] | None = (
            re.compile("|".join(re.escape(e) for e in self._emotion_emojis))
            if self._emotion_emojis else None
        )
        self._chat_conn: dict[str, Any] = {}
        self._streams: dict[str, tuple[str, bool]] = {}
        self._current_stream_id: str | None = None
        # TTS enable/disable state (MVP — channel-side short-circuit only;
        # synthesis still runs. See migration-todo §3-C.α).
        # chat_id -> bool; absent == True (default enabled).
        self._tts_enabled_per_chat: dict[str, bool] = {}
        # chat_id -> reference_id (voice id) most recently sent by the FE.
        # Survives across turns within a session so proactive nudges (which
        # carry no envelope) can reuse the user's chosen voice. ``None``
        # means "use the synthesizer's default".
        self._reference_id_per_chat: dict[str, str | None] = {}
        # connection id() -> bool; takes precedence over per-chat flags
        # so URL ``?tts=0`` overrides per-message toggles for the whole socket.
        self._tts_enabled_per_conn: dict[int, bool] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._server: Any | None = None
        # Per-image byte cap — instance attr so tests can dial it down.
        self._max_image_bytes: int = _MAX_IMAGE_BYTES
        # Media directory resolved lazily; tests can override by assigning _media_dir.
        self._media_dir: Path | None = None

    # ── Connection Bookkeeping ───────────────────────────────────────────

    def _attach(self, chat_id: str, connection: Any) -> None:
        self._chat_conn[chat_id] = connection

    def _detach_connection(self, connection: Any) -> None:
        for cid, conn in list(self._chat_conn.items()):
            if conn is connection:
                self._chat_conn.pop(cid, None)
                self._tts_enabled_per_chat.pop(cid, None)
                self._reference_id_per_chat.pop(cid, None)
        self._tts_enabled_per_conn.pop(id(connection), None)
        for sid, (cid, _) in list(self._streams.items()):
            if cid not in self._chat_conn:
                self._streams.pop(sid, None)
                if self._current_stream_id == sid:
                    self._current_stream_id = None

    # ── Image Intake ─────────────────────────────────────────────────────

    def _resolve_media_dir(self) -> Path:
        if self._media_dir is not None:
            return self._media_dir
        from nanobot.config.paths import get_media_dir
        self._media_dir = get_media_dir("desktop_mate")
        return self._media_dir

    def _decode_inbound_images(
        self,
        images: list[str] | None,
        *,
        sender_id: str,
    ) -> tuple[list[str], ImageRejectReason | None]:
        return _decode_images(
            images,
            sender_id=sender_id,
            media_dir=self._resolve_media_dir(),
            max_image_bytes=self._max_image_bytes,
        )

    # ── Frame Helpers ─────────────────────────────────────────────────────

    async def _send_frame(self, connection: Any, frame: BaseModel) -> None:
        raw = frame.model_dump_json(exclude_none=True)
        try:
            await connection.send(raw)
        except Exception:
            logger.opt(exception=True).warning("desktop_mate: send failed, dropping connection")
            self._detach_connection(connection)

    async def _send_ready(self, connection: Any, *, client_id: str) -> str:
        """Emit the post-handshake ``ready`` frame. Returns the new connection_id."""
        connection_id = str(uuid.uuid4())
        await self._send_frame(
            connection,
            ReadyFrame(connection_id=connection_id, client_id=client_id, server_time=time.time()),
        )
        return connection_id

    # ── Outbound ──────────────────────────────────────────────────────────

    async def send(self, msg: OutboundMessage) -> None:
        conn = self._chat_conn.get(msg.chat_id)
        if conn is None:
            logger.warning("desktop_mate: no connection for chat_id={}", msg.chat_id)
            return

        meta = msg.metadata or {}
        proactive_flag: bool | None = True if meta.get("proactive") else None
        stream_id = meta.get("_stream_id")

        if meta.get("_stream_start"):
            if stream_id:
                self._streams[stream_id] = (msg.chat_id, bool(proactive_flag))
                self._current_stream_id = stream_id
            await self._send_frame(conn, StreamStartFrame(chat_id=msg.chat_id, proactive=proactive_flag))
            return

        # Nanobot calls send() once per agent iteration, not just at the
        # conversational turn boundary: tool-call hops produce an
        # OutboundMessage with empty content and no _stream_end marker (just
        # _tool_hint / _tool_events). Emitting stream_end on those would let
        # FE consumers — and the live e2e harness — interpret each tool hop
        # as the turn being complete and stop reading early. Only emit when
        # nanobot explicitly marks the message as the stream end.
        if not meta.get("_stream_end"):
            return

        # We deliberately do NOT clear stream state here — TTS synthesis runs
        # concurrently and a tts_chunk may arrive after stream_end via the TTS
        # Barrier. Stream entries are cleared only when a new stream registers
        # or the connection drops.
        await self._send_frame(
            conn,
            StreamEndFrame(chat_id=msg.chat_id, content=msg.content, proactive=proactive_flag),
        )

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._chat_conn.get(chat_id)
        if conn is None:
            logger.warning("desktop_mate: send_delta: no connection for chat_id={}", chat_id)
            return

        meta = metadata or {}
        stream_id = meta.get("_stream_id")
        proactive_flag: bool | None = True if meta.get("proactive") else None

        # An empty delta marked _stream_end on a NEW stream_id means this is
        # an iteration boundary that produced no user-visible text — typically
        # a tool-call hop. Emitting stream_start + stream_end for it would let
        # FE consumers (and the live e2e harness) interpret each tool hop as
        # the turn being complete. Skip — only register / emit when actual
        # content arrives.
        if (
            not delta
            and meta.get("_stream_end")
            and bool(stream_id)
            and stream_id not in self._streams
        ):
            return

        # First delta for a new stream: register routing entry AND auto-emit
        # stream_start. Nanobot's channel manager never sets _stream_start
        # metadata itself, so without this the FE never sees the turn boundary.
        if bool(stream_id) and stream_id not in self._streams:
            self._streams[stream_id] = (chat_id, bool(proactive_flag))
            self._current_stream_id = stream_id
            await self._send_frame(conn, StreamStartFrame(chat_id=chat_id, proactive=proactive_flag))

        if meta.get("_stream_end"):
            await self._send_frame(
                conn,
                StreamEndFrame(chat_id=chat_id, content=delta, proactive=proactive_flag),
            )
            return

        await self._send_frame(
            conn,
            DeltaFrame(
                chat_id=chat_id,
                text=self._strip_emotions(delta),
                stream_id=stream_id,
                proactive=proactive_flag,
            ),
        )

    # ── REST Surface ──────────────────────────────────────────────────────

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        return dispatch_http(self.config.token, self._session_manager, self.config.path, request)

    # ── Auth / Handshake ──────────────────────────────────────────────────

    def _authorize_token(self, supplied: str | None) -> bool:
        expected = (self.config.token or "").strip()
        if not expected:
            return True
        return bool(supplied) and supplied == expected

    async def _handshake(self, connection: Any, query: dict[str, list[str]]) -> bool:
        """Validate ``?token=`` and close with 4003 on failure."""
        if not self._authorize_token(query_first(query, "token")):
            try:
                await connection.close(code=4003, reason="invalid token")
            except Exception:
                logger.opt(exception=True).warning("desktop_mate: close after bad token raised")
            return False
        return True


# ── Lazy TTS Sink ──────────────────────────────────────────────────────────


class LazyChannelTTSSink:
    """TTSSink that resolves the active DesktopMateChannel at send time.

    Avoids ordering constraints between hook factory and channel construction.
    If the channel isn't available yet, chunks are silently dropped — the
    agent loop stays healthy and the FE misses TTS for that window.
    """

    def is_enabled(self) -> bool:
        try:
            return get_desktop_mate_channel().is_tts_enabled_for_current_stream()
        except RuntimeError:
            return True  # No channel yet — allow hook to do useful work.

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        try:
            channel = get_desktop_mate_channel()
        except RuntimeError:
            return
        await channel.send_tts_chunk(chunk)

    def get_reference_id_for_session(self, session_key: str | None) -> str | None:
        """Resolve the voice for ``session_key`` via the active channel.

        ``session_key`` follows the nanobot ``"<channel>:<chat_id>"`` form. Only
        ``desktop_mate:*`` keys can be resolved here; anything else (or a
        missing channel) returns ``None`` so the synthesizer falls back to its
        constructor default.
        """
        if not session_key:
            return None
        prefix, _, chat_id = session_key.partition(":")
        if prefix != "desktop_mate" or not chat_id:
            return None
        try:
            channel = get_desktop_mate_channel()
        except RuntimeError:
            return None
        return channel.reference_id_for_chat_id(chat_id)
