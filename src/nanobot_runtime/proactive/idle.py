"""Idle-watcher system job for yuri / nanobot workspaces.

Phase 5 Proactive (A) — wakes the agent on a user's channel when a session
has been silent past ``idle_timeout_s``. Schedule, session iteration and
outbound delivery all ride nanobot-native primitives; the only logic we
own is the judgment gate (quiet hours, channel allowlist, idle threshold,
active-turn, cooldown, re-validation race) plus single-target selection.

Selection model: at most one nudge per scan tick, sent to the most-recently
updated allowlisted session. Conversation history does not cross channels
(``unifiedSession=false``), so a "most recent" target is well-defined per
allowlist scope. See issue #19 for the cross-channel design space.

Dispatch path: scanner publishes a synthesized ``InboundMessage`` with
``_wants_stream=True`` and ``proactive=True`` metadata. The nanobot
``AgentLoop._dispatch`` consumer picks it up, wires the streaming hooks
(deltas + stream_end), runs the agent loop, and publishes the final
OutboundMessage — identical to a real user message, so DesktopMateChannel
streaming and TTS chunk routing work without proactive-specific glue.

Installed via :func:`install_idle_system_job` from the gateway launcher.
"""
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

try:
    from nanobot.bus.events import InboundMessage
    from nanobot.cron.types import CronJob, CronPayload, CronSchedule
except ImportError:  # pragma: no cover - allows isolated static analysis
    CronJob = CronPayload = CronSchedule = None  # type: ignore[assignment]
    InboundMessage = None  # type: ignore[assignment]


IDLE_SYSTEM_JOB_ID = "idle-watcher"

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
    end: str = Field(description="HH:MM quiet-hours end. If start > end, window spans midnight.")


class IdleConfig(BaseModel):
    """Configuration for the idle-watcher system job."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = Field(default=True, description="Enable or disable the idle watcher entirely.")
    idle_timeout_s: int = Field(default=300, description="Seconds of silence before a nudge is sent.")
    cooldown_s: int = Field(default=900, description="Minimum seconds between nudges for the same session.")
    scan_interval_s: int = Field(default=30, description="How often the watcher scans sessions (seconds).")
    startup_grace_s: int = Field(
        default=120,
        ge=0,
        description="Seconds after process start during which all nudges are suppressed. "
        "Prevents the reboot-storm where dormant sessions trigger bulk nudges before "
        "the in-memory cooldown table has been populated.",
    )
    quiet_hours: "QuietHours | None" = Field(default=None, description="Time window during which nudges are suppressed.")
    timezone: str = Field(default="UTC", description="IANA timezone name for quiet-hours evaluation.")
    channels: tuple[str, ...] = Field(default=("desktop_mate",), description="Channel names that receive idle nudges.")
    idle_prompt: str = Field(default=_DEFAULT_IDLE_PROMPT, description="Prompt template; {minutes} is substituted.")
    context_providers: tuple[Callable[[], Awaitable[str]], ...] = Field(
        default_factory=tuple,
        description="Reserved for Phase 5.5 context injection.",
    )


class _SessionManagerLike(Protocol):
    def list_sessions(self) -> list[dict[str, Any]]: ...
    def get_or_create(self, key: str) -> Any: ...


class _AgentLike(Protocol):
    _session_locks: dict[str, Any]
    bus: Any  # nanobot.bus.queue.MessageBus — typed loosely so this module imports without nanobot.


class _CronLike(Protocol):
    on_job: Any

    def register_system_job(self, job: Any) -> Any: ...


class IdleScanner:
    """Scans sessions once per tick and issues a nudge if all gates pass."""

    def __init__(
        self,
        *,
        agent: _AgentLike,
        sessions: _SessionManagerLike,
        config: IdleConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._agent = agent
        self._sessions = sessions
        self._config = config
        self._tz = ZoneInfo(config.timezone)
        self._clock = clock or (lambda: datetime.now(tz=self._tz))
        self._cooldown_until: dict[str, float] = {}
        self._started_at: datetime = self._clock()

    async def scan_and_nudge(self) -> None:
        now = self._clock()
        if (now - self._started_at).total_seconds() < self._config.startup_grace_s:
            return
        if self._config.quiet_hours and _in_quiet_hours(now, self._config.quiet_hours):
            return

        target = self._select_target(now)
        if target is None:
            return

        key, channel, chat_id, fresh_updated = target

        # Mark cooldown BEFORE dispatch so a downstream failure cannot drive a retry storm.
        self._cooldown_until[key] = now.timestamp() + self._config.cooldown_s

        minutes = _minutes_between(fresh_updated, now)
        prompt = self._config.idle_prompt.format(minutes=minutes)
        try:
            await self._dispatch_nudge(key=key, channel=channel, chat_id=chat_id, prompt=prompt)
            logger.info("Idle nudge dispatched: session={} idle={}m", key, minutes)
        except Exception:
            logger.exception("Idle nudge failed for {}", key)

    def _select_target(
        self, now: datetime
    ) -> tuple[str, str, str, datetime] | None:
        """Pick the most-recently-updated allowlisted session that passes every gate.

        Returns ``(session_key, channel, chat_id, fresh_updated_at)`` or ``None``.

        Selection — single target per tick — embodies issue #19's per-channel
        isolation (no cross-channel "user is active somewhere" notion) and the
        Phase 5-A spec of "one nudge per scan tick at most".
        """
        best: tuple[str, str, str, datetime] | None = None
        for info in self._sessions.list_sessions():
            key = info.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if not chat_id:
                continue
            if channel not in self._config.channels:
                continue
            if self._is_active_turn(key):
                continue
            if not self._is_idle(info.get("updated_at"), now):
                continue
            if self._is_in_cooldown(key, now):
                continue

            fresh = self._sessions.get_or_create(key)
            fresh_updated = getattr(fresh, "updated_at", None)
            if not self._is_idle(fresh_updated, now):
                continue
            try:
                fresh_dt = _to_aware(fresh_updated, self._tz)
            except (TypeError, ValueError):
                continue

            if best is None or fresh_dt > best[3]:
                best = (key, channel, chat_id, fresh_dt)
        return best

    async def _dispatch_nudge(
        self, *, key: str, channel: str, chat_id: str, prompt: str
    ) -> None:
        """Publish a synthesized inbound message that rides the normal _dispatch path.

        ``_wants_stream`` enables the streaming callback wiring inside
        ``AgentLoop._dispatch`` (deltas + stream_end OutboundMessages on the bus),
        which is what registers ``_current_stream_id`` on DesktopMateChannel and
        makes TTS chunk routing work end-to-end.
        """
        if InboundMessage is None:  # pragma: no cover - import-time guard
            raise RuntimeError(
                "nanobot.bus.events unavailable — IdleScanner requires nanobot-ai runtime"
            )
        msg = InboundMessage(
            channel=channel,
            sender_id="idle-watcher",
            chat_id=chat_id,
            content=prompt,
            metadata={"proactive": True, "_wants_stream": True},
            session_key_override=key,
        )
        await self._agent.bus.publish_inbound(msg)

    def _is_active_turn(self, key: str) -> bool:
        locks = getattr(self._agent, "_session_locks", None)
        if not isinstance(locks, dict):
            return False
        lock = locks.get(key)
        return bool(lock is not None and getattr(lock, "locked", lambda: False)())

    def _is_idle(self, ts: Any, now: datetime) -> bool:
        if ts is None:
            return False
        try:
            last = _to_aware(ts, self._tz)
        except (TypeError, ValueError):
            return False
        return (now - last).total_seconds() >= self._config.idle_timeout_s

    def _is_in_cooldown(self, key: str, now: datetime) -> bool:
        until = self._cooldown_until.get(key)
        if until is None:
            return False
        if now.timestamp() >= until:
            del self._cooldown_until[key]
            return False
        return True


def install_idle_system_job(
    *,
    agent: _AgentLike,
    sessions: _SessionManagerLike,
    cron: _CronLike,
    config: IdleConfig,
    clock: Callable[[], datetime] | None = None,
) -> IdleScanner | None:
    """Register the system cron job and wrap ``cron.on_job`` to route it to the scanner.

    Returns the scanner (useful for tests or manual triggering) or ``None``
    when ``config.enabled`` is False.

    Side effects:
    - ``cron.register_system_job(...)`` — registers a protected system job
      (see ``nanobot/cron/service.py::register_system_job``).
    - ``cron.on_job`` is replaced with a composite that dispatches the idle
      job id to the scanner and delegates everything else to the previous
      callback (so default nanobot cron behaviour — reminders, dream, etc.
      — is preserved).
    """
    if not config.enabled:
        return None
    if CronJob is None or CronSchedule is None or CronPayload is None:
        raise RuntimeError(
            "nanobot.cron types unavailable — install_idle_system_job requires nanobot-ai runtime"
        )

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=clock)

    job = CronJob(
        id=IDLE_SYSTEM_JOB_ID,
        name=IDLE_SYSTEM_JOB_ID,
        enabled=True,
        schedule=CronSchedule(kind="every", every_ms=config.scan_interval_s * 1000),
        payload=CronPayload(kind="system_event"),
    )

    previous_on_job = cron.on_job

    async def composite(job: Any) -> Any:
        if getattr(job, "id", None) == IDLE_SYSTEM_JOB_ID:
            await scanner.scan_and_nudge()
            return None
        if previous_on_job is not None:
            return await previous_on_job(job)
        return None

    cron.on_job = composite
    cron.register_system_job(job)
    logger.info(
        "Idle watcher installed (timeout={}s cooldown={}s scan={}s channels={})",
        config.idle_timeout_s,
        config.cooldown_s,
        config.scan_interval_s,
        list(config.channels),
    )
    return scanner


# ── Helpers ──────────────────────────────────────────────────────────────


def _in_quiet_hours(now: datetime, qh: QuietHours) -> bool:
    start = _parse_hm(qh.start)
    end = _parse_hm(qh.end)
    current = (now.hour, now.minute)
    if start <= end:
        return start <= current < end
    # Window crosses midnight: e.g., 22:00 → 06:00.
    return current >= start or current < end


def _parse_hm(s: str) -> tuple[int, int]:
    hh, mm = s.split(":", 1)
    return int(hh), int(mm)


def _to_aware(ts: Any, tz: ZoneInfo) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=tz)
    if isinstance(ts, str):
        parsed = datetime.fromisoformat(ts)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    raise TypeError(f"unsupported timestamp: {ts!r}")


def _minutes_between(ts: Any, now: datetime) -> int:
    last = _to_aware(ts, now.tzinfo or ZoneInfo("UTC"))
    return max(0, int((now - last).total_seconds() // 60))
