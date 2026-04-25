"""Config model and helpers for DesktopMateChannel."""
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field


class DesktopMateConfig(BaseModel):
    """Runtime configuration for DesktopMateChannel.

    Clients connect at ``ws://{host}:{port}{path}?token=<token>&client_id=<id>``.
    ``token`` is matched with plain string equality (not constant-time);
    ``client_id`` becomes the nanobot ``sender_id`` for ``allow_from`` authorization.
    """

    enabled: bool = Field(default=True, description="Whether the channel is active.")
    host: str = Field(default="127.0.0.1", description="Bind address for the WebSocket server.")
    port: int = Field(default=8765, description="TCP port the WebSocket server listens on.")
    path: str = Field(default="/ws", description="URL path for the WebSocket endpoint.")
    token: str = Field(default="", description="Static bearer token; empty means no auth required.")
    allow_from: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Allowlist of client_id values; '*' accepts any client.",
    )
    streaming: bool = Field(default=True, description="Enable streaming delta frames.")
    ping_interval_s: float | None = Field(
        default=20.0, description="WebSocket ping interval in seconds; None disables."
    )
    ping_timeout_s: float | None = Field(
        default=20.0, description="WebSocket ping timeout in seconds; None disables."
    )
    # Must fit _MAX_IMAGES_PER_MESSAGE × _MAX_IMAGE_BYTES base64-encoded (×4/3)
    # ≈ 53 MB. 60 MB gives headroom for JSON framing.
    max_message_bytes: int = Field(
        default=60 * 1024 * 1024,
        description="Maximum inbound WebSocket message size in bytes.",
    )
    # Path to the YAML file whose ``emotion_motion_map`` keys enumerate the
    # emojis to strip from outbound ``delta`` text.
    emotion_map_path: str | None = Field(
        default=None,
        description="Path to the YAML emotion-motion map; None disables emoji stripping.",
    )


# Accept either snake_case (internal) or camelCase (nanobot.json convention).
_CAMEL_TO_SNAKE: dict[str, str] = {
    "allowFrom": "allow_from",
    "pingIntervalS": "ping_interval_s",
    "pingTimeoutS": "ping_timeout_s",
    "maxMessageBytes": "max_message_bytes",
    "emotionMapPath": "emotion_map_path",
}


def _coerce_config(section: Any) -> DesktopMateConfig:
    """Normalise a channel section into DesktopMateConfig.

    Accepts an existing instance (returned unchanged), a dict from
    ``nanobot.json`` (snake_case or camelCase), or a pydantic-like object.
    Unknown keys are silently ignored.
    """
    if isinstance(section, DesktopMateConfig):
        return section

    if hasattr(section, "model_dump"):
        raw: dict[str, Any] = section.model_dump()
    elif isinstance(section, dict):
        raw = dict(section)
    else:
        logger.warning(
            "desktop_mate: unrecognised config type %s — using defaults",
            type(section).__name__,
        )
        raw = {}

    known = set(DesktopMateConfig.model_fields)
    normalised: dict[str, Any] = {}
    for key, value in raw.items():
        target = _CAMEL_TO_SNAKE.get(key, key)
        if target in known:
            normalised[target] = value
    return DesktopMateConfig(**normalised)


def _parse_bool_flag(value: str | None) -> bool | None:
    """Parse a URL query value into True / False / None (absent = no override)."""
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("0", "false", "off", "no"):
        return False
    if v in ("1", "true", "on", "yes"):
        return True
    return None
