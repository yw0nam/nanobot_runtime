"""TTS policy mixin for DesktopMateChannel.

Provides per-chat/per-connection TTS enable/disable logic, the ``send_tts_chunk``
TTSSink implementation, and emotion-emoji stripping for delta text.

Methods access state initialised in DesktopMateChannel.__init__:
``_tts_enabled_per_chat``, ``_tts_enabled_per_conn``, ``_current_stream_id``,
``_streams``, ``_chat_conn``, ``_emotion_strip_re``, and ``_send_frame``.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot_runtime.channels.desktop_mate_config import _parse_bool_flag
from nanobot_runtime.channels.desktop_mate_protocol import TTSChunkFrame
from nanobot_runtime.channels.desktop_mate_rest import query_first
from nanobot_runtime.hooks.tts import TTSChunk


class _DesktopMateTTSMixin:
    """TTS policy and sink methods — mixed into DesktopMateChannel."""

    def _strip_emotions(self, text: str) -> str:
        pattern = self._emotion_strip_re  # type: ignore[attr-defined]
        if pattern is None:
            return text
        return pattern.sub("", text)

    def _apply_connection_tts_override(
        self, connection: Any, query: dict[str, list[str]]
    ) -> None:
        """Read the URL ``tts`` param during handshake and record per-connection policy."""
        flag = _parse_bool_flag(query_first(query, "tts"))
        if flag is None:
            return
        self._tts_enabled_per_conn[id(connection)] = flag  # type: ignore[attr-defined]

    def _tts_enabled_for_chat(self, chat_id: str) -> bool:
        """Effective policy: connection override wins over per-chat flag; default True."""
        conn = self._chat_conn.get(chat_id)  # type: ignore[attr-defined]
        if conn is not None:
            conn_flag = self._tts_enabled_per_conn.get(id(conn))  # type: ignore[attr-defined]
            if conn_flag is not None:
                return conn_flag
        return self._tts_enabled_per_chat.get(chat_id, True)  # type: ignore[attr-defined]

    def is_tts_enabled_for_current_stream(self) -> bool:
        """Return False when no desktop_mate stream is currently registered.

        This prevents TTSHook from synthesising for turns running on other
        channels (e.g. the idle-watcher firing through ``channel=cli``).
        """
        stream_id = self._current_stream_id  # type: ignore[attr-defined]
        if stream_id is None:
            return False
        info = self._streams.get(stream_id)  # type: ignore[attr-defined]
        if info is None:
            return False
        chat_id, _ = info
        return self._tts_enabled_for_chat(chat_id)

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        """Emit a ``tts_chunk`` frame for the currently streaming chat."""
        stream_id = self._current_stream_id  # type: ignore[attr-defined]
        if stream_id is None or stream_id not in self._streams:  # type: ignore[attr-defined]
            logger.warning(
                "desktop_mate: send_tts_chunk with no active stream (seq={}); dropping",
                chunk.sequence,
            )
            return
        chat_id, proactive = self._streams[stream_id]  # type: ignore[attr-defined]
        conn = self._chat_conn.get(chat_id)  # type: ignore[attr-defined]
        if conn is None:
            logger.warning("desktop_mate: tts_chunk target gone (chat_id={})", chat_id)
            return

        # MVP trade-off: synthesis still ran upstream (see migration-todo §3-C.α).
        if not self._tts_enabled_for_chat(chat_id):
            return

        await self._send_frame(  # type: ignore[attr-defined]
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
