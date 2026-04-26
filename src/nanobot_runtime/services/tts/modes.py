"""Channel → TTS mode mapping loaded from a workspace YAML.

The launcher loads this once at boot and injects the resulting
``ChannelModeMap`` into ``LazyChannelTTSSink``. The hook never sees the
map directly — it only calls ``sink.is_enabled(session_key)``, and the
sink consults the map to decide.

YAML shape::

    default: none
    channels:
      desktop_mate: streaming
      telegram: attachment   # mode declared; ATTACHMENT pipeline TBD
      slack: none

Channels not listed default to the value of ``default:`` (which itself
defaults to ``none`` if absent). Unknown mode strings raise ``ValueError``
at boot so an operator typo fails loud.
"""
from enum import Enum
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class TTSMode(str, Enum):
    """TTS dispatch mode per channel.

    Inherits from ``str`` for trivial YAML round-tripping and Pydantic
    field coercion.
    """

    STREAMING = "streaming"
    ATTACHMENT = "attachment"
    NONE = "none"


class ChannelModeMap(BaseModel):
    """Resolves channel name → TTS mode. Frozen post-construction."""

    model_config = ConfigDict(frozen=True)

    default: TTSMode = Field(
        default=TTSMode.NONE,
        description="Mode for channels not listed explicitly in `channels`.",
    )
    channels: dict[str, TTSMode] = Field(
        default_factory=dict,
        description="Explicit channel-name → TTSMode mapping.",
    )

    def lookup(self, channel_name: str | None) -> TTSMode:
        """Return the mode for ``channel_name``, or ``default`` if unknown.

        ``None`` (e.g. session_key was None upstream — Slack DM, Telegram
        non-topic) maps to ``default``.
        """
        if channel_name is None:
            return self.default
        return self.channels.get(channel_name, self.default)


def load_channel_modes(path: str | Path) -> ChannelModeMap:
    """Parse the YAML at ``path`` into a ``ChannelModeMap``.

    - File missing → ``FileNotFoundError`` (caller decides whether to
      fail or skip; the launcher fails loud at boot).
    - Empty file → ``ChannelModeMap()`` (all-NONE; via Pydantic field
      defaults, no special-case in this loader).
    - Missing ``default:`` or ``channels:`` keys → field defaults apply.
    - Invalid mode strings → ``ValueError`` naming the field, value, and
      file path so an operator typo is immediately actionable.
    - Top-level not a mapping (e.g. ``[1, 2, 3]``) → ``ValueError``.
    - ``channels:`` not a mapping (e.g. a list) → ``ValueError``. Caught
      explicitly so the operator gets a clear message instead of an
      AttributeError on ``.items()``.

    The loader's only job is YAML I/O, type-shape validation, and
    converting raw mode strings to ``TTSMode`` enum values. It never
    substitutes defaults itself — Pydantic field defaults handle absence.
    """
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"Failed to parse TTS modes YAML at {p}: {e}") from e
    if raw is None:
        return ChannelModeMap()
    if not isinstance(raw, dict):
        raise ValueError(
            f"Top-level YAML must be a mapping in {p}, got {type(raw).__name__}"
        )

    kwargs: dict[str, object] = {}

    if "default" in raw:
        try:
            kwargs["default"] = TTSMode(raw["default"])
        except ValueError as e:
            raise ValueError(
                f"Invalid TTS mode {raw['default']!r} for 'default' in {p}"
            ) from e

    if "channels" in raw and raw["channels"] is not None:
        chans_raw = raw["channels"]
        if not isinstance(chans_raw, dict):
            raise ValueError(
                f"'channels' must be a mapping in {p}, got {type(chans_raw).__name__}"
            )
        channels: dict[str, TTSMode] = {}
        for name, mode_str in chans_raw.items():
            try:
                channels[name] = TTSMode(mode_str)
            except ValueError as e:
                raise ValueError(
                    f"Invalid TTS mode {mode_str!r} for channel {name!r} in {p}"
                ) from e
        kwargs["channels"] = channels

    result = ChannelModeMap(**kwargs)
    attachment_channels = sorted(
        name for name, mode in result.channels.items() if mode is TTSMode.ATTACHMENT
    )
    if attachment_channels or result.default is TTSMode.ATTACHMENT:
        logger.warning(
            "TTSMode.ATTACHMENT is declared in {} for {} but no attachment "
            "delivery pipeline is implemented yet — those channels will "
            "receive no audio (silently equivalent to NONE).",
            p,
            (
                f"channels {attachment_channels}"
                if attachment_channels
                else "default"
            ),
        )
    return result
