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
import binascii
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger
from pydantic import BaseModel, ValidationError
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.utils.media_decode import FileSizeExceeded, save_base64_data_url

from nanobot_runtime.channels.desktop_mate_protocol import (
    DeltaFrame,
    ImageRejectedFrame,
    ImageRejectReason,
    MessageFrame,
    NewChatFrame,
    ReadyFrame,
    StreamEndFrame,
    StreamStartFrame,
    TTSChunkFrame,
    _MAX_IMAGES_PER_MESSAGE,
    parse_inbound,
)
from nanobot_runtime.channels.desktop_mate_rest import (
    bearer_token,
    decode_api_key,
    http_error,
    http_json_response,
    is_websocket_upgrade,
    parse_request_path,
    query_first,
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
    # Max inbound frame size in bytes.  Must be large enough to fit the
    # worst-case image payload: _MAX_IMAGES_PER_MESSAGE (4) × _MAX_IMAGE_BYTES
    # (10 MB) base64-encoded (×4/3) ≈ 53 MB.  60 MB gives headroom for JSON
    # framing and future cap adjustments.
    max_message_bytes: int = 60 * 1024 * 1024
    # Path to the YAML file whose ``emotion_motion_map`` keys enumerate the
    # emojis the channel should strip from outbound ``delta`` text. Same file
    # the TTSHook loads for keyframe mapping — kept in sync naturally.
    # When unset the channel does no stripping (tests explicitly inject the
    # set via the ``emotion_emojis`` kwarg; production must point this at
    # the workspace's ``tts_rules.yml``).
    emotion_map_path: str | None = None


# Accept either snake_case (internal) or camelCase (nanobot.json convention).
_CAMEL_TO_SNAKE: dict[str, str] = {
    "allowFrom": "allow_from",
    "pingIntervalS": "ping_interval_s",
    "pingTimeoutS": "ping_timeout_s",
    "maxMessageBytes": "max_message_bytes",
    "emotionMapPath": "emotion_map_path",
}


# Per-image ingress caps (issue #8). Matches upstream nanobot's built-in
# WS channel intent: 10 MB per image is the user-facing ceiling, SVG is
# excluded to avoid embedded-script XSS surface.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})
_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _load_emotion_emojis_from_yaml(path: str) -> set[str]:
    """Extract the keys of ``emotion_motion_map`` from a YAML file.

    Shared source of truth with :class:`EmotionMapper.from_yaml`. Returns
    an empty set on any load error — better to miss emoji stripping than
    to crash gateway startup over a missing config file.
    """
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    except (OSError, Exception) as e:  # noqa: BLE001 — tolerate any load error
        logger.warning(
            "desktop_mate: failed to load emotion map from {}: {}",
            path,
            e,
        )
        return set()
    if not isinstance(data, dict):
        return set()
    mapping = data.get("emotion_motion_map", {})
    if not isinstance(mapping, dict):
        return set()
    return {k for k in mapping if isinstance(k, str) and k != "default"}


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
        session_manager: Any | None = None,
    ) -> None:
        coerced = _coerce_config(config)
        super().__init__(coerced, bus)
        self.config: DesktopMateConfig = coerced
        # Injected by the gateway's ChannelManager monkey-patch; used by the
        # REST routes below. ``None`` is tolerated so unit tests can construct
        # the channel in isolation — the routes 503 in that case.
        self._session_manager = session_manager
        global _LATEST_CHANNEL
        _LATEST_CHANNEL = self
        # Emoji-stripping set: explicit kwarg (tests) wins, else load from
        # config-specified YAML (production path). Empty set = no stripping.
        if emotion_emojis is not None:
            self._emotion_emojis = set(emotion_emojis)
        elif coerced.emotion_map_path:
            self._emotion_emojis = _load_emotion_emojis_from_yaml(
                coerced.emotion_map_path
            )
        else:
            self._emotion_emojis = set()
        # chat_id -> connection (1 connection per chat in desktop-mate's 1:1 model)
        self._chat_conn: dict[str, Any] = {}
        # stream_id -> (chat_id, proactive flag) for TTSSink routing
        self._streams: dict[str, tuple[str, bool]] = {}
        # Most recently registered stream_id — used as "current" for send_tts_chunk
        self._current_stream_id: str | None = None
        # TTS enable/disable state (MVP — channel-side short-circuit only;
        # synthesis still runs. See migration-todo §3-C.α for the
        # synthesis-skip follow-up that removes the wasted work).
        # chat_id -> bool; absent == True (default enabled).
        self._tts_enabled_per_chat: dict[str, bool] = {}
        # connection identity -> bool; takes precedence over per-chat flags
        # so URL ``?tts=0`` overrides per-message toggles for the whole
        # socket. Connection objects are hashable; we key on id() to avoid
        # relying on their __hash__ implementation.
        self._tts_enabled_per_conn: dict[int, bool] = {}
        # Server state
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._server: Any | None = None
        # Per-image byte cap — exposed as an instance attr so tests can
        # dial it down without base64-encoding 10 MB of padding per case.
        self._max_image_bytes: int = _MAX_IMAGE_BYTES
        # Media directory for decoded inbound images. Resolved lazily via
        # :meth:`_resolve_media_dir` so tests can override by assigning
        # ``channel._media_dir`` before triggering the inbound loop.
        self._media_dir: Path | None = None

    # -- Subscription bookkeeping ------------------------------------------

    def _attach(self, chat_id: str, connection: Any) -> None:
        self._chat_conn[chat_id] = connection

    def _detach_connection(self, connection: Any) -> None:
        for cid, conn in list(self._chat_conn.items()):
            if conn is connection:
                self._chat_conn.pop(cid, None)
                self._tts_enabled_per_chat.pop(cid, None)
        self._tts_enabled_per_conn.pop(id(connection), None)
        # Drop any streams whose chat is gone.
        for sid, (cid, _) in list(self._streams.items()):
            if cid not in self._chat_conn:
                self._streams.pop(sid, None)
                if self._current_stream_id == sid:
                    self._current_stream_id = None

    # -- Image intake ------------------------------------------------------

    def _resolve_media_dir(self) -> Path:
        """Return the directory inbound images are persisted to.

        Uses nanobot's ``get_media_dir("desktop_mate")`` so the FS layout
        matches the built-in WS channel. Lazy-evaluated so construction
        order (channel-first vs config-first) doesn't matter.
        """
        if self._media_dir is not None:
            return self._media_dir
        from nanobot.config.paths import get_media_dir

        resolved = get_media_dir("desktop_mate")
        self._media_dir = resolved
        return resolved

    def _decode_inbound_images(
        self,
        images: list[str] | None,
        *,
        sender_id: str,
    ) -> tuple[list[str], ImageRejectReason | None]:
        """Decode a frame's ``images`` entries to disk.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the
        first failure (whole turn rejected — no partial ingress). Every
        rejection is logged with ``sender_id`` + mime + size so MIME
        violations (a potential XSS / XXE surface) and server-side IO
        failures are observable to ops. ``ImageRejectReason`` is the
        closed set documented on :class:`ImageRejectedFrame`.
        """
        if not images:
            return [], None

        if len(images) > _MAX_IMAGES_PER_MESSAGE:
            logger.warning(
                "desktop_mate: rejecting inbound images from {}: "
                "count={} exceeds cap={} (reason=too_many)",
                sender_id, len(images), _MAX_IMAGES_PER_MESSAGE,
            )
            return [], "too_many"

        media_dir = self._resolve_media_dir()
        saved_paths: list[str] = []

        def _abort(
            reason: ImageRejectReason,
            *,
            mime: str | None = None,
            size_hint: int | None = None,
        ) -> tuple[list[str], ImageRejectReason]:
            # io_error is a server-side failure (disk full, permission,
            # media dir gone); everything else is caller-fixable.
            level = "error" if reason == "io_error" else "warning"
            getattr(logger, level)(
                "desktop_mate: rejecting inbound image from {}: "
                "reason={} mime={} size_hint={}",
                sender_id, reason, mime, size_hint,
            )
            # Any image already written before a later entry fails must be
            # unlinked — partial state on disk confuses downstream cleanup.
            for p in saved_paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "desktop_mate: failed to unlink partial media {}: {}",
                        p, exc,
                    )
            return [], reason

        for entry in images:
            if not isinstance(entry, str) or not entry:
                return _abort("malformed")
            mime = _extract_data_url_mime(entry)
            if mime is None:
                return _abort("malformed")
            if mime not in _IMAGE_MIME_ALLOWED:
                return _abort("unsupported_mime", mime=mime)
            try:
                saved = save_base64_data_url(
                    entry, media_dir, max_bytes=self._max_image_bytes,
                )
            except FileSizeExceeded:
                return _abort("too_large", mime=mime, size_hint=len(entry))
            except (binascii.Error, ValueError) as exc:
                # Malformed base64 / bad data URL payload. Caller-fixable.
                logger.debug(
                    "desktop_mate: decode failed (caller-fixable): {}", exc
                )
                return _abort("malformed", mime=mime, size_hint=len(entry))
            except OSError as exc:
                # Disk write failure (full FS, permission, missing dir).
                # Server-side problem — surface as io_error so FE can
                # retry rather than blame the user's image.
                logger.opt(exception=True).error(
                    "desktop_mate: image persist failed: {}", exc
                )
                return _abort("io_error", mime=mime, size_hint=len(entry))
            if saved is None:
                return _abort("malformed", mime=mime, size_hint=len(entry))
            saved_paths.append(saved)
        return saved_paths, None

    # -- TTS policy --------------------------------------------------------

    @staticmethod
    def _parse_bool_flag(value: str | None) -> bool | None:
        """Parse ``?tts=<value>`` into a tri-state: True / False / None (no override)."""
        if value is None:
            return None
        v = value.strip().lower()
        if v in ("0", "false", "off", "no"):
            return False
        if v in ("1", "true", "on", "yes"):
            return True
        return None

    def _apply_connection_tts_override(
        self, connection: Any, query: dict[str, list[str]]
    ) -> None:
        """Read the URL ``tts`` param during handshake and record per-connection policy."""
        values = query.get("tts") or []
        flag = self._parse_bool_flag(values[0] if values else None)
        if flag is None:
            return
        self._tts_enabled_per_conn[id(connection)] = flag

    def _tts_enabled_for_chat(self, chat_id: str) -> bool:
        """Effective policy: connection override wins over per-chat flag; default True."""
        conn = self._chat_conn.get(chat_id)
        if conn is not None:
            conn_flag = self._tts_enabled_per_conn.get(id(conn))
            if conn_flag is not None:
                return conn_flag
        return self._tts_enabled_per_chat.get(chat_id, True)

    def is_tts_enabled_for_current_stream(self) -> bool:
        """Public accessor for :class:`LazyChannelTTSSink`.

        Returns ``False`` whenever no desktop_mate stream is currently
        registered. This is the gate that keeps TTSHook from synthesising
        for turns running on *other* channels (e.g. the Phase-5 idle
        watcher firing through ``channel=cli``): those turns never set
        ``_current_stream_id`` on this channel, so we correctly decline
        to emit audio for them.

        The previous default (``True`` when no stream) was a holdover from
        a single-channel assumption that Phase 5 broke. See
        migration-todo §3-D (Scenario C regression) and the tts.py
        "Per-session state" section.
        """
        stream_id = self._current_stream_id
        if stream_id is None:
            return False
        info = self._streams.get(stream_id)
        if info is None:
            return False
        chat_id, _ = info
        return self._tts_enabled_for_chat(chat_id)

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

        # Short-circuit when TTS is disabled for this chat/connection.
        # Synthesis still happened upstream (MVP trade-off — see
        # migration-todo §3-C.α). Dropping here still saves the FE from
        # decoding and playing audio on a muted client.
        if not self._tts_enabled_for_chat(chat_id):
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

    # -- REST surface ------------------------------------------------------

    _SESSION_KEY_PREFIX = "desktop_mate:"

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to a REST handler or fall through to WS.

        Returning ``None`` lets the ``websockets`` library proceed with the
        WebSocket handshake; returning a :class:`Response` short-circuits
        with an HTTP reply.
        """
        got, _query = parse_request_path(request.path)

        if got == "/api/sessions":
            return self._handle_sessions_list(request)

        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        # websockets' HTTP parser only supports GET, so delete is path-folded.
        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        # Let the WS handshake path through.
        expected_ws = self.config.path.rstrip("/") or "/"
        if got == expected_ws and is_websocket_upgrade(request):
            return None

        return http_error(404, "Not Found")

    def _check_rest_token(self, request: WsRequest) -> bool:
        """Validate the REST request against the channel's static token.

        Accepts ``Authorization: Bearer <token>`` header or ``?token=<token>``
        query string. When no token is configured (dev / loopback), every
        request is allowed — mirroring ``_authorize_token``.
        """
        expected = (self.config.token or "").strip()
        if not expected:
            return True
        _, query = parse_request_path(request.path)
        supplied = bearer_token(request.headers) or query_first(query, "token")
        return supplied is not None and supplied == expected

    def _is_desktop_mate_key(self, key: str) -> bool:
        return key.startswith(self._SESSION_KEY_PREFIX)

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self._check_rest_token(request):
            return http_error(401, "Unauthorized")
        if self._session_manager is None:
            return http_error(503, "session manager unavailable")
        sessions = self._session_manager.list_sessions()
        # Filter to our own channel's sessions and strip absolute paths —
        # the FE doesn't need filesystem layout.
        cleaned = [
            {k: v for k, v in s.items() if k != "path"}
            for s in sessions
            if isinstance(s.get("key"), str)
            and self._is_desktop_mate_key(s["key"])
        ]
        return http_json_response({"sessions": cleaned})

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self._check_rest_token(request):
            return http_error(401, "Unauthorized")
        if self._session_manager is None:
            return http_error(503, "session manager unavailable")
        decoded = decode_api_key(key)
        if decoded is None:
            return http_error(400, "invalid session key")
        if not self._is_desktop_mate_key(decoded):
            return http_error(404, "session not found")
        data = self._session_manager.read_session_file(decoded)
        if data is None:
            return http_error(404, "session not found")
        return http_json_response(data)

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self._check_rest_token(request):
            return http_error(401, "Unauthorized")
        if self._session_manager is None:
            return http_error(503, "session manager unavailable")
        decoded = decode_api_key(key)
        if decoded is None:
            return http_error(400, "invalid session key")
        if not self._is_desktop_mate_key(decoded):
            return http_error(404, "session not found")
        deleted = self._session_manager.delete_session(decoded)
        return http_json_response({"deleted": bool(deleted)})

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

            media_paths, reject_reason = self._decode_inbound_images(
                envelope.images, sender_id=sender_id,
            )
            if reject_reason is not None:
                # new_chat rejections carry no chat_id — the session was
                # never created. FE correlates via ``reference_id`` instead
                # when it needs to match the rejection to its pending send.
                await self._send_frame(
                    connection,
                    ImageRejectedFrame(
                        chat_id=chat_id if isinstance(envelope, MessageFrame)
                        else None,
                        reason=reject_reason,
                        reference_id=envelope.reference_id,
                    ),
                )
                continue

            self._attach(chat_id, connection)
            # Record per-chat TTS policy from the inbound flag. Connection
            # URL override (``?tts=0``) still wins in ``_tts_enabled_for_chat``.
            self._tts_enabled_per_chat[chat_id] = bool(envelope.tts_enabled)
            # Hand-off to the bus. If this raises (or the allow_from check
            # inside silently rejects), decoded image files would leak on
            # disk; unlink them on any non-happy exit. We don't propagate
            # the exception — the connection should keep serving further
            # frames from the same client.
            try:
                await self._handle_message(
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
                            "desktop_mate: leaked media {} "
                            "(unlink after handle_message failure): {}",
                            p, unlink_exc,
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

            self._apply_connection_tts_override(connection, query)
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

        async def process_request(connection: Any, request: WsRequest) -> Any:
            return await self._dispatch_http(connection, request)

        async def runner() -> None:
            async with serve(
                handler,
                self.config.host,
                self.config.port,
                process_request=process_request,
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

    def is_enabled(self) -> bool:
        """Return the current stream's TTS policy, or True if no channel
        has been constructed yet (safe default — hook does useful work).
        """
        try:
            channel = get_desktop_mate_channel()
        except RuntimeError:
            return True
        return channel.is_tts_enabled_for_current_stream()

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        try:
            channel = get_desktop_mate_channel()
        except RuntimeError:
            # Channel not constructed yet — drop silently.
            return
        await channel.send_tts_chunk(chunk)
