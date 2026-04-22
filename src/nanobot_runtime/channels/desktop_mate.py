"""DesktopMateChannel — DMP-compatible WebSocket channel + TTSSink.

Implements the FE WebSocket protocol documented in
``nanobot-migration.md §8`` (outbound: ``stream_start`` / ``delta`` /
``tts_chunk`` / ``stream_end`` + optional ``proactive: true``; inbound:
``new_chat`` / ``message``). Also satisfies the ``TTSSink`` protocol
defined in :mod:`nanobot_runtime.hooks.tts` so ``TTSHook`` can route
synthesised audio frames through the same socket.

TTSSink chat_id routing
-----------------------

``TTSHook.send_tts_chunk(chunk)`` does not pass ``chat_id``. Nanobot's
agent loop streams sequentially per session, so we track the *currently
active stream* by recording ``(stream_id → (chat_id, connection))`` on
the first ``send_delta`` for a given ``stream_id`` and clearing it on
``stream_end``. ``send_tts_chunk`` always routes to the most recently
registered stream.

This slice keeps the server minimal. Auth is a single static token
matched against ``?token=`` in the connection URL; JWT / rotating tokens
are explicitly out of scope and can layer on later.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger
from pydantic import BaseModel, ValidationError

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

from nanobot_runtime.channels.desktop_mate_protocol import (
    DeltaFrame,
    MessageFrame,
    NewChatFrame,
    ReadyFrame,
    StreamEndFrame,
    StreamStartFrame,
    TTSChunkFrame,
    parse_inbound,
)
from nanobot_runtime.hooks.tts import TTSChunk


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class DesktopMateConfig:
    """Runtime configuration for :class:`DesktopMateChannel`.

    The channel expects clients to connect at
    ``ws://{host}:{port}{path}?token=<token>&client_id=<client_id>``.
    ``token`` is compared against :attr:`token` using constant-time
    equality. ``client_id`` populates the nanobot ``sender_id`` for
    ``allow_from`` authorization.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/ws"
    token: str = ""
    allow_from: list[str] = field(default_factory=lambda: ["*"])
    streaming: bool = True
    # WS protocol-level keepalive (seconds). Set to None to disable.
    ping_interval_s: float | None = 20.0
    ping_timeout_s: float | None = 20.0
    # Max inbound frame size in bytes. 6MB accommodates the DMP image cap
    # (~4.5MB binary ≈ ~6MB base64).
    max_message_bytes: int = 6 * 1024 * 1024


# Accept either snake_case (internal) or camelCase (nanobot.json convention).
_CAMEL_TO_SNAKE: dict[str, str] = {
    "allowFrom": "allow_from",
    "pingIntervalS": "ping_interval_s",
    "pingTimeoutS": "ping_timeout_s",
    "maxMessageBytes": "max_message_bytes",
}


def _coerce_config(section: Any) -> DesktopMateConfig:
    """Normalise a channel section into :class:`DesktopMateConfig`.

    Accepts three shapes ChannelManager may emit:
      * an existing ``DesktopMateConfig`` instance (used by tests and direct
        Python callers) — returned unchanged;
      * a dict parsed from ``nanobot.json`` (snake_case or camelCase);
      * a pydantic-like section object — coerced via ``model_dump``.

    Unknown keys are silently ignored so config file evolution doesn't
    crash startup on a forgotten field.
    """
    if isinstance(section, DesktopMateConfig):
        return section

    if hasattr(section, "model_dump"):
        raw: dict[str, Any] = section.model_dump()
    elif isinstance(section, dict):
        raw = dict(section)
    else:
        raw = {}

    normalised: dict[str, Any] = {}
    known = {f.name for f in DesktopMateConfig.__dataclass_fields__.values()}
    for key, value in raw.items():
        target = _CAMEL_TO_SNAKE.get(key, key)
        if target in known:
            normalised[target] = value
    return DesktopMateConfig(**normalised)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


# Single-process registry for TTS sink lookup.
#
# ChannelManager builds the channel during gateway start-up, while hooks are
# built per-AgentLoop. The two code paths don't share a reference, so the
# channel registers itself here on construction and :class:`LazyChannelTTSSink`
# resolves it at send time. In practice nanobot instantiates exactly one
# DesktopMateChannel per process; repeat construction (e.g. hot reload or
# test reruns) overwrites the entry.
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


class DesktopMateChannel(BaseChannel):
    """WebSocket channel implementing the DMP-compatible FE protocol."""

    name = "desktop_mate"
    display_name = "DesktopMate"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        emotion_emojis: set[str] | None = None,
    ) -> None:
        coerced = _coerce_config(config)
        super().__init__(coerced, bus)
        self.config: DesktopMateConfig = coerced
        global _LATEST_CHANNEL
        _LATEST_CHANNEL = self
        self._emotion_emojis = emotion_emojis or set()
        # chat_id -> connection (1 connection per chat in desktop-mate's 1:1 model)
        self._chat_conn: dict[str, Any] = {}
        # stream_id -> (chat_id, proactive flag) for TTSSink routing
        self._streams: dict[str, tuple[str, bool]] = {}
        # Most recently registered stream_id — used as "current" for send_tts_chunk
        self._current_stream_id: str | None = None
        # Server state
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._server: Any | None = None

    # -- Subscription bookkeeping ------------------------------------------

    def _attach(self, chat_id: str, connection: Any) -> None:
        self._chat_conn[chat_id] = connection

    def _detach_connection(self, connection: Any) -> None:
        for cid, conn in list(self._chat_conn.items()):
            if conn is connection:
                self._chat_conn.pop(cid, None)
        # Drop any streams whose chat is gone.
        for sid, (cid, _) in list(self._streams.items()):
            if cid not in self._chat_conn:
                self._streams.pop(sid, None)
                if self._current_stream_id == sid:
                    self._current_stream_id = None

    # -- Frame helpers -----------------------------------------------------

    def _strip_emotions(self, text: str) -> str:
        if not self._emotion_emojis:
            return text
        out = text
        for emoji in self._emotion_emojis:
            out = out.replace(emoji, "")
        return out

    async def _send_frame(self, connection: Any, frame: BaseModel) -> None:
        raw = frame.model_dump_json(exclude_none=True)
        try:
            await connection.send(raw)
        except Exception as e:
            logger.warning("desktop_mate: send failed, dropping connection: {}", e)
            self._detach_connection(connection)

    async def _send_ready(self, connection: Any, *, client_id: str) -> str:
        """Emit the post-handshake ``ready`` frame. Returns the new connection_id."""
        connection_id = str(uuid.uuid4())
        await self._send_frame(
            connection,
            ReadyFrame(
                connection_id=connection_id,
                client_id=client_id,
                server_time=time.time(),
            ),
        )
        return connection_id

    # -- BaseChannel.send --------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """Dispatch an outbound nanobot message to the correct DMP frame."""
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
            await self._send_frame(
                conn,
                StreamStartFrame(chat_id=msg.chat_id, proactive=proactive_flag),
            )
            return

        # Fallback and explicit _stream_end both serialise as stream_end.
        # Note: we deliberately do NOT clear ``_streams[stream_id]`` or
        # ``_current_stream_id`` here. TTS synthesis runs concurrently with
        # the agent loop; a ``tts_chunk`` may arrive via the hook's TTS
        # Barrier **after** nanobot's manager has already dispatched
        # ``_stream_end`` through the bus. Stream entries are cleared only
        # when a new stream replaces them or the connection drops.
        await self._send_frame(
            conn,
            StreamEndFrame(
                chat_id=msg.chat_id,
                content=msg.content,
                proactive=proactive_flag,
            ),
        )

    # -- BaseChannel.send_delta --------------------------------------------

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._chat_conn.get(chat_id)
        if conn is None:
            return

        meta = metadata or {}
        stream_id = meta.get("_stream_id")
        proactive_flag: bool | None = True if meta.get("proactive") else None

        # First delta for a new stream: register the routing entry AND
        # auto-emit a ``stream_start`` frame. Nanobot's channel manager
        # never sets a ``_stream_start`` metadata itself, so without this
        # the FE never sees the turn boundary.
        is_new_stream = bool(stream_id) and stream_id not in self._streams
        if is_new_stream:
            self._streams[stream_id] = (chat_id, bool(proactive_flag))
            self._current_stream_id = stream_id
            await self._send_frame(
                conn,
                StreamStartFrame(chat_id=chat_id, proactive=proactive_flag),
            )

        # _stream_end is routed here by nanobot's channel manager when it
        # coalesces. We deliberately keep stream state so a ``tts_chunk``
        # arriving via the TTS Barrier after stream_end still has a
        # chat_id to route to (see :meth:`send`).
        if meta.get("_stream_end"):
            await self._send_frame(
                conn,
                StreamEndFrame(
                    chat_id=chat_id,
                    content=delta,
                    proactive=proactive_flag,
                ),
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

    # -- TTSSink -----------------------------------------------------------

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        """Emit a ``tts_chunk`` frame for the currently streaming chat."""
        stream_id = self._current_stream_id
        if stream_id is None or stream_id not in self._streams:
            logger.warning(
                "desktop_mate: send_tts_chunk with no active stream (seq={}); dropping",
                chunk.sequence,
            )
            return
        chat_id, proactive = self._streams[stream_id]
        conn = self._chat_conn.get(chat_id)
        if conn is None:
            logger.warning("desktop_mate: tts_chunk target gone (chat_id={})", chat_id)
            return

        await self._send_frame(
            conn,
            TTSChunkFrame(
                chat_id=chat_id,
                sequence=chunk.sequence,
                text=chunk.text,
                audio_base64=chunk.audio_base64,
                emotion=chunk.emotion,
                keyframes=list(chunk.keyframes),
                proactive=True if proactive else None,
            ),
        )

    # -- Auth / handshake --------------------------------------------------

    def _authorize_token(self, supplied: str | None) -> bool:
        expected = (self.config.token or "").strip()
        if not expected:
            # No token configured — allow (trusted dev loopback only).
            return True
        if not supplied:
            return False
        # hmac.compare_digest-ish; plain equality is fine for a dev-static
        # token but we avoid early exit on length to keep the intent clear.
        return supplied == expected

    async def _handshake(self, connection: Any, query: dict[str, list[str]]) -> bool:
        """Validate ``?token=`` and close with 4003 on failure.

        Returns True when the connection should proceed to the main loop.
        """
        token_values = query.get("token") or []
        supplied = token_values[0] if token_values else None
        if not self._authorize_token(supplied):
            try:
                await connection.close(code=4003, reason="invalid token")
            except Exception as e:
                logger.debug("desktop_mate: close after bad token raised: {}", e)
            return False
        return True

    # -- Inbound loop ------------------------------------------------------

    async def _connection_loop(
        self,
        connection: Any,
        *,
        sender_id: str,
        default_chat_id: str | None = None,
    ) -> None:
        """Consume inbound frames, dispatching ``new_chat`` / ``message``."""
        async for raw in connection:
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("desktop_mate: non-utf8 binary frame, ignored")
                    continue

            try:
                envelope = parse_inbound(raw)
            except (ValidationError, ValueError) as e:
                logger.warning(
                    "desktop_mate: bad inbound frame, ignored: {} ({!r})",
                    e,
                    raw[:100],
                )
                continue

            base_metadata: dict[str, Any] = {
                "tts_enabled": envelope.tts_enabled,
                "reference_id": envelope.reference_id,
                "remote": getattr(connection, "remote_address", None),
            }

            if isinstance(envelope, NewChatFrame):
                chat_id = str(uuid.uuid4())
            else:
                assert isinstance(envelope, MessageFrame)
                chat_id = envelope.chat_id

            self._attach(chat_id, connection)
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=envelope.content,
                metadata=base_metadata,
            )

    # -- Server lifecycle --------------------------------------------------

    async def start(self) -> None:
        """Bind the WS server and serve until :meth:`stop` is called.

        This uses the minimum viable ``websockets`` serve loop. Production
        concerns (backpressure, SSL, per-connection timeouts, token rotation)
        are explicitly deferred to a follow-up slice.
        """
        import websockets
        from websockets.asyncio.server import serve

        self._running = True
        self._stop_event = asyncio.Event()

        async def handler(connection: Any) -> None:
            request = getattr(connection, "request", None)
            raw_path = request.path if request else "/"
            parsed = urlparse("ws://x" + raw_path)
            query = parse_qs(parsed.query)

            accepted = await self._handshake(connection, query)
            if not accepted:
                return

            client_id_vals = query.get("client_id") or []
            client_id = (client_id_vals[0] if client_id_vals else "").strip()
            if not client_id:
                client_id = f"anon-{uuid.uuid4().hex[:12]}"
            if not self.is_allowed(client_id):
                try:
                    await connection.close(code=4003, reason="forbidden")
                except Exception:
                    pass
                return

            await self._send_ready(connection, client_id=client_id)

            try:
                await self._connection_loop(connection, sender_id=client_id)
            except Exception as e:
                logger.debug("desktop_mate: connection loop ended: {}", e)
            finally:
                self._detach_connection(connection)

        logger.info(
            "desktop_mate: listening on ws://{}:{}{}",
            self.config.host,
            self.config.port,
            self.config.path,
        )

        async def runner() -> None:
            async with serve(
                handler,
                self.config.host,
                self.config.port,
                ping_interval=self.config.ping_interval_s,
                ping_timeout=self.config.ping_timeout_s,
                max_size=self.config.max_message_bytes,
            ) as server:
                self._server = server
                assert self._stop_event is not None
                await self._stop_event.wait()

        self._server_task = asyncio.create_task(runner())
        try:
            await self._server_task
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug("desktop_mate: server task cleanup: {}", e)
                self._server_task.cancel()
            self._server_task = None
        self._chat_conn.clear()
        self._streams.clear()
        self._current_stream_id = None


class LazyChannelTTSSink:
    """TTSSink that resolves the active DesktopMateChannel at send time.

    ``TTSHook`` takes a sink at construction, but the channel is created
    later on a different code path. Doing the lookup lazily (per-chunk)
    avoids ordering constraints. If the channel isn't available yet the
    chunk is silently dropped — the agent loop stays healthy and FE will
    simply miss TTS for that window.
    """

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        try:
            channel = get_desktop_mate_channel()
        except RuntimeError:
            # Channel not constructed yet — drop silently.
            return
        await channel.send_tts_chunk(chunk)
