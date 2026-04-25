"""Config dataclass and helpers for DesktopMateChannel."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class DesktopMateConfig:
    """Runtime configuration for DesktopMateChannel.

    Clients connect at ``ws://{host}:{port}{path}?token=<token>&client_id=<id>``.
    ``token`` is matched with plain string equality (not constant-time);
    ``client_id`` becomes the nanobot ``sender_id`` for ``allow_from`` authorization.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/ws"
    token: str = ""
    allow_from: list[str] = field(default_factory=lambda: ["*"])
    streaming: bool = True
    ping_interval_s: float | None = 20.0
    ping_timeout_s: float | None = 20.0
    # Must fit _MAX_IMAGES_PER_MESSAGE × _MAX_IMAGE_BYTES base64-encoded (×4/3)
    # ≈ 53 MB. 60 MB gives headroom for JSON framing.
    max_message_bytes: int = 60 * 1024 * 1024
    # Path to the YAML file whose ``emotion_motion_map`` keys enumerate the
    # emojis to strip from outbound ``delta`` text.
    emotion_map_path: str | None = None


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

    known = {f.name for f in DesktopMateConfig.__dataclass_fields__.values()}
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
