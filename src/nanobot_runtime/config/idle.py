"""Configuration models for the idle-watcher system job."""

from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field


_DEFAULT_IDLE_PROMPT = (
    "[Idle Nudge] The user has been silent for {minutes} minutes. "
    "Speak first as their desktop companion — greet them or pick up an earlier thread. "
    "Keep it short (1-2 sentences). "
    "Use the Current Time in your system prompt to choose an appropriate tone; if it looks "
    "like deep-focus or late-night hours, stay warm and brief rather than starting a new topic."
)


class QuietHours(BaseModel):
    """Local-time window during which idle nudges are suppressed entirely.

    ``start`` and ``end`` are ``HH:MM`` strings in the config timezone. If
    ``start > end`` the window spans midnight (e.g. ``22:00 → 06:00``).
    """

    model_config = ConfigDict(frozen=True)

    start: str = Field(description="HH:MM quiet-hours start in config timezone.")
    end: str = Field(
        description="HH:MM quiet-hours end. If start > end, window spans midnight."
    )


class IdleConfig(BaseModel):
    """Configuration for the idle-watcher system job."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = Field(
        default=True, description="Enable or disable the idle watcher entirely."
    )
    idle_timeout_s: int = Field(
        default=300, description="Seconds of silence before a nudge is sent."
    )
    cooldown_s: int = Field(
        default=900, description="Minimum seconds between nudges for the same session."
    )
    scan_interval_s: int = Field(
        default=30, description="How often the watcher scans sessions (seconds)."
    )
    startup_grace_s: int = Field(
        default=120,
        ge=0,
        description="Seconds after process start during which all nudges are suppressed. "
        "Prevents the reboot-storm where dormant sessions trigger bulk nudges before "
        "the in-memory cooldown table has been populated.",
    )
    quiet_hours: "QuietHours | None" = Field(
        default=None, description="Time window during which nudges are suppressed."
    )
    timezone: str = Field(
        default="UTC", description="IANA timezone name for quiet-hours evaluation."
    )
    channels: tuple[str, ...] = Field(
        default=("desktop_mate",), description="Channel names that receive idle nudges."
    )
    idle_prompt: str = Field(
        default=_DEFAULT_IDLE_PROMPT,
        description="Prompt template; {minutes} is substituted.",
    )
    context_providers: tuple[Callable[[], Awaitable[str]], ...] = Field(
        default_factory=tuple,
        description="Reserved for Phase 5.5 context injection.",
    )
