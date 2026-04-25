"""EmotionMapper — emotion string/emoji → list of timeline keyframes.

Ported from DMP's ``EmotionMotionMapper`` (src/services/tts_service/
emotion_motion_mapper.py). Implements the ``EmotionMapper`` Protocol
declared in ``nanobot_runtime.hooks.tts``.

Config format::

    {
        "😊": {"keyframes": [{"duration": 0.4, "targets": {"happy": 1.0}}]},
        "default": {"keyframes": [{"duration": 0.3, "targets": {"neutral": 1.0}}]},
    }

When an emotion is None, empty, or unregistered, returns the ``default``
entry's keyframes or a hardcoded neutral fallback.
"""

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from nanobot_runtime.hooks.tts import EmotionMapper as _EmotionMapperBase

_HARDCODED_DEFAULT: list[dict[str, Any]] = [
    {"duration": 0.3, "targets": {"neutral": 1.0}}
]


class EmotionMapper(_EmotionMapperBase):
    """Map emotion keyword/emoji → list of timeline keyframe dicts."""

    def __init__(self, config: dict[str, dict]) -> None:
        self._map = config
        default_entry = config.get("default", {}) if config else {}
        self._default: list[dict[str, Any]] = (
            default_entry.get("keyframes") or _HARDCODED_DEFAULT
        )
        self._known_emojis: frozenset[str] = frozenset(
            k for k in config if k != "default"
        )

    @property
    def known_emojis(self) -> frozenset[str]:
        return self._known_emojis

    def map(self, emotion: str | None) -> list[dict[str, Any]]:
        entry = self._map.get(emotion) if emotion else None
        if entry is None:
            return self._default
        return entry.get("keyframes") or self._default

    # ── Factory ───────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EmotionMapper":
        """Load the ``emotion_motion_map`` block from a YAML file."""
        p = Path(path)
        try:
            with p.open("r", encoding="utf-8") as stream:
                data = yaml.safe_load(stream) or {}
        except (OSError, yaml.YAMLError):
            logger.opt(exception=True).warning(
                "EmotionMapper.from_yaml failed to load {}; using empty config",
                p,
            )
            return cls({})

        if not isinstance(data, dict):
            return cls({})
        config = data.get("emotion_motion_map", {})
        if not isinstance(config, dict):
            return cls({})
        return cls(config)
