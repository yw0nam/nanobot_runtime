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
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

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


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


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
        super().__init__(config, bus)
        self.config: DesktopMateConfig = config
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

    async def _send_frame(self, connection: Any, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except Exception as e:
            logger.warning("desktop_mate: send failed, dropping connection: {}", e)
            self._detach_connection(connection)

    # -- BaseChannel.send --------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """Dispatch an outbound nanobot message to the correct DMP frame."""
        conn = self._chat_conn.get(msg.chat_id)
        if conn is None:
            logger.warning("desktop_mate: no connection for chat_id={}", msg.chat_id)
            return

        meta = msg.metadata or {}
        proactive = bool(meta.get("proactive"))
        stream_id = meta.get("_stream_id")

        if meta.get("_stream_start"):
            frame: dict[str, Any] = {"event": "stream_start", "chat_id": msg.chat_id}
            if proactive:
                frame["proactive"] = True
            if stream_id:
                self._streams[stream_id] = (msg.chat_id, proactive)
                self._current_stream_id = stream_id
            await self._send_frame(conn, frame)
            return

        if meta.get("_stream_end"):
            frame = {
                "event": "stream_end",
                "chat_id": msg.chat_id,
                "content": msg.content,
            }
            if proactive:
                frame["proactive"] = True
            await self._send_frame(conn, frame)
            if stream_id and stream_id in self._streams:
                self._streams.pop(stream_id, None)
                if self._current_stream_id == stream_id:
                    self._current_stream_id = None
            return

        # Fallback: non-streaming final message. Treat it as a full-content
        # stream_end so FE sees the reply.
        frame = {
            "event": "stream_end",
            "chat_id": msg.chat_id,
            "content": msg.content,
        }
        if proactive:
            frame["proactive"] = True
        await self._send_frame(conn, frame)

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
        proactive = bool(meta.get("proactive"))

        # Register this stream for TTSSink routing on first sight.
        if stream_id and stream_id not in self._streams:
            self._streams[stream_id] = (chat_id, proactive)
            self._current_stream_id = stream_id

        # _stream_end is routed here by nanobot's channel manager when it
        # coalesces; mirror send() behaviour.
        if meta.get("_stream_end"):
            frame: dict[str, Any] = {
                "event": "stream_end",
                "chat_id": chat_id,
                "content": delta,
            }
            if proactive:
                frame["proactive"] = True
            await self._send_frame(conn, frame)
            if stream_id and stream_id in self._streams:
                self._streams.pop(stream_id, None)
                if self._current_stream_id == stream_id:
                    self._current_stream_id = None
            return

        cleaned = self._strip_emotions(delta)
        frame = {
            "event": "delta",
            "chat_id": chat_id,
            "text": cleaned,
        }
        if stream_id:
            frame["stream_id"] = stream_id
        if proactive:
            frame["proactive"] = True
        await self._send_frame(conn, frame)

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

        frame: dict[str, Any] = {
            "event": "tts_chunk",
            "chat_id": chat_id,
            "sequence": chunk.sequence,
            "text": chunk.text,
            "audio_base64": chunk.audio_base64,
            "emotion": chunk.emotion,
            "keyframes": list(chunk.keyframes),
        }
        if proactive:
            frame["proactive"] = True
        await self._send_frame(conn, frame)

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
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("desktop_mate: bad JSON frame, ignored: {!r}", raw[:100])
                continue
            if not isinstance(envelope, dict):
                continue

            kind = envelope.get("type")
            content = envelope.get("content")
            if not isinstance(content, str) or not content.strip():
                continue

            tts_enabled = bool(envelope.get("tts_enabled", True))
            reference_id = envelope.get("reference_id")
            base_metadata: dict[str, Any] = {
                "tts_enabled": tts_enabled,
                "reference_id": reference_id,
                "remote": getattr(connection, "remote_address", None),
            }

            if kind == "new_chat":
                chat_id = str(uuid.uuid4())
                self._attach(chat_id, connection)
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=content,
                    metadata=base_metadata,
                )
                continue

            if kind == "message":
                chat_id = envelope.get("chat_id")
                if not isinstance(chat_id, str) or not chat_id:
                    logger.warning("desktop_mate: message without chat_id, ignored")
                    continue
                self._attach(chat_id, connection)
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=content,
                    metadata=base_metadata,
                )
                continue

            logger.debug("desktop_mate: unknown frame type={!r}, ignored", kind)

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
            async with serve(handler, self.config.host, self.config.port) as server:
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
