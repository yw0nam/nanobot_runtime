"""Server lifecycle and inbound loop mixin for DesktopMateChannel.

``_DesktopMateServerMixin`` provides ``start``, ``stop``, and
``_connection_loop``. Methods access state initialised in
DesktopMateChannel.__init__: ``config``, ``_running``, ``_stop_event``,
``_server``, ``_server_task``, ``_tts_enabled_per_chat``, and the methods
``_handshake``, ``_dispatch_http``, ``_apply_connection_tts_override``,
``_send_ready``, ``_decode_inbound_images``, ``_attach``, ``_send_frame``,
``_detach_connection``, ``_handle_message``, and ``is_allowed``.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from nanobot_runtime.channels.desktop_mate_protocol import (
    ImageRejectedFrame,
    MessageFrame,
    NewChatFrame,
    parse_inbound,
)
from nanobot_runtime.channels.desktop_mate_rest import parse_request_path, query_first


class _DesktopMateServerMixin:
    """WebSocket server lifecycle and inbound message loop."""

    # -- Inbound loop ----------------------------------------------------------

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
                    e, raw[:100],
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

            media_paths, reject_reason = self._decode_inbound_images(  # type: ignore[attr-defined]
                envelope.images, sender_id=sender_id,
            )
            if reject_reason is not None:
                # new_chat rejections have no chat_id yet; FE correlates via reference_id.
                await self._send_frame(  # type: ignore[attr-defined]
                    connection,
                    ImageRejectedFrame(
                        chat_id=chat_id if isinstance(envelope, MessageFrame) else None,
                        reason=reject_reason,
                        reference_id=envelope.reference_id,
                    ),
                )
                continue

            self._attach(chat_id, connection)  # type: ignore[attr-defined]
            self._tts_enabled_per_chat[chat_id] = bool(envelope.tts_enabled)  # type: ignore[attr-defined]
            # On any non-happy exit, unlink decoded image files to avoid disk
            # leakage. Don't propagate the exception — the connection keeps serving.
            try:
                await self._handle_message(  # type: ignore[attr-defined]
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=envelope.content,
                    media=media_paths or None,
                    metadata=base_metadata,
                )
            except Exception as exc:
                logger.opt(exception=True).error(
                    "desktop_mate: handle_message failed (chat_id={}): {}",
                    chat_id, exc,
                )
                for p in media_paths:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError as unlink_exc:
                        logger.warning(
                            "desktop_mate: leaked media {} (unlink after failure): {}",
                            p, unlink_exc,
                        )

    # -- Server lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Bind the WS server and serve until :meth:`stop` is called."""
        from websockets.asyncio.server import serve

        self._running = True  # type: ignore[attr-defined]
        self._stop_event = asyncio.Event()  # type: ignore[attr-defined]

        async def handler(connection: Any) -> None:
            request = getattr(connection, "request", None)
            raw_path = request.path if request else "/"
            _, query = parse_request_path(raw_path)

            accepted = await self._handshake(connection, query)  # type: ignore[attr-defined]
            if not accepted:
                return

            client_id = (query_first(query, "client_id") or "").strip()
            if not client_id:
                client_id = f"anon-{uuid.uuid4().hex[:12]}"
            if not self.is_allowed(client_id):  # type: ignore[attr-defined]
                try:
                    await connection.close(code=4003, reason="forbidden")
                except Exception:
                    pass
                return

            self._apply_connection_tts_override(connection, query)  # type: ignore[attr-defined]
            await self._send_ready(connection, client_id=client_id)  # type: ignore[attr-defined]

            try:
                await self._connection_loop(connection, sender_id=client_id)
            except Exception as e:
                logger.debug("desktop_mate: connection loop ended: {}", e)
            finally:
                self._detach_connection(connection)  # type: ignore[attr-defined]

        config = self.config  # type: ignore[attr-defined]
        logger.info(
            "desktop_mate: listening on ws://{}:{}{}",
            config.host, config.port, config.path,
        )

        async def runner() -> None:
            async with serve(
                handler,
                config.host,
                config.port,
                process_request=self._dispatch_http,  # type: ignore[attr-defined]
                ping_interval=config.ping_interval_s,
                ping_timeout=config.ping_timeout_s,
                max_size=config.max_message_bytes,
            ) as server:
                self._server = server  # type: ignore[attr-defined]
                stop_event = self._stop_event  # type: ignore[attr-defined]
                assert stop_event is not None
                await stop_event.wait()

        self._server_task = asyncio.create_task(runner())  # type: ignore[attr-defined]
        try:
            await self._server_task  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if not self._running:  # type: ignore[attr-defined]
            return
        self._running = False  # type: ignore[attr-defined]
        stop_event = self._stop_event  # type: ignore[attr-defined]
        if stop_event is not None:
            stop_event.set()
        server_task = self._server_task  # type: ignore[attr-defined]
        if server_task is not None:
            try:
                await asyncio.wait_for(server_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug("desktop_mate: server task cleanup: {}", e)
                server_task.cancel()
            self._server_task = None  # type: ignore[attr-defined]
        self._chat_conn.clear()  # type: ignore[attr-defined]
        self._streams.clear()  # type: ignore[attr-defined]
        self._current_stream_id = None  # type: ignore[attr-defined]
