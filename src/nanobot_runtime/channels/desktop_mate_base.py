"""Abstract base declaring the shared state contract for desktop_mate mixins."""
import asyncio
import re
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from nanobot_runtime.channels.desktop_mate_config import DesktopMateConfig
from nanobot_runtime.channels.desktop_mate_protocol import ImageRejectReason


class _DesktopMateBase(ABC):
    """Declares attributes and abstract methods shared across desktop_mate mixins.

    Both ``_DesktopMateServerMixin`` and ``_DesktopMateTTSMixin`` inherit from
    this class so the type checker can resolve attribute and method access.
    ``DesktopMateChannel`` and ``BaseChannel`` supply the concrete implementations.
    """

    # ── Attributes initialised in DesktopMateChannel.__init__ ─────────────────

    config: DesktopMateConfig
    _running: bool
    _stop_event: asyncio.Event | None
    _server: Any | None
    _server_task: asyncio.Task[None] | None
    _chat_conn: dict[str, Any]
    _streams: dict[str, tuple[str, bool]]
    _current_stream_id: str | None
    _tts_enabled_per_chat: dict[str, bool]
    _tts_enabled_per_conn: dict[int, bool]
    _emotion_strip_re: re.Pattern[str] | None

    # ── Abstract methods implemented by DesktopMateChannel / BaseChannel ───────

    @abstractmethod
    async def _send_frame(self, connection: Any, frame: BaseModel) -> None: ...

    @abstractmethod
    def _attach(self, chat_id: str, connection: Any) -> None: ...

    @abstractmethod
    def _detach_connection(self, connection: Any) -> None: ...

    @abstractmethod
    def _decode_inbound_images(
        self,
        images: list[str] | None,
        *,
        sender_id: str,
    ) -> tuple[list[str], "ImageRejectReason | None"]: ...

    @abstractmethod
    async def _send_ready(self, connection: Any, *, client_id: str) -> str: ...

    @abstractmethod
    async def _handshake(
        self, connection: Any, query: dict[str, list[str]]
    ) -> bool: ...

    @abstractmethod
    async def _dispatch_http(self, connection: Any, request: Any) -> Any: ...

    @abstractmethod
    def _apply_connection_tts_override(
        self, connection: Any, query: dict[str, list[str]]
    ) -> None: ...
