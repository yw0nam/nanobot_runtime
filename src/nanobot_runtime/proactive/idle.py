"""Idle-watcher system job for yuri / nanobot workspaces.

Phase 5 Proactive (A) — wakes the agent on a user's channel when a session
has been silent past ``idle_timeout_s``. Schedule, session iteration and
outbound delivery all ride nanobot-native primitives; the only logic we
own is the judgment gate (quiet hours, channel allowlist, idle threshold,
active-turn, cooldown, re-validation race).

Installed via :func:`install_idle_system_job` from the gateway launcher.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from loguru import logger

try:
    from nanobot.cron.types import CronJob, CronPayload, CronSchedule
except ImportError:  # pragma: no cover - allows isolated static analysis
    CronJob = CronPayload = CronSchedule = None  # type: ignore[assignment]


IDLE_SYSTEM_JOB_ID = "idle-watcher"

_DEFAULT_IDLE_PROMPT = (
    "[Idle Nudge] The user has been silent for {minutes} minutes. "
    "Speak first as their desktop companion — greet them or pick up an earlier thread. "
    "Keep it short (1-2 sentences). "
    "Use the Current Time in your system prompt to choose an appropriate tone; if it looks "
    "like deep-focus or late-night hours, stay warm and brief rather than starting a new topic."
)


@dataclass(frozen=True)
class QuietHours:
    """Local-time window during which idle nudges are suppressed entirely.

    ``start`` and ``end`` are ``HH:MM`` strings in the config timezone. If
    ``start > end`` the window spans midnight (e.g. ``22:00 → 06:00``).
    """

    start: str
    end: str


@dataclass
class IdleConfig:
    enabled: bool = True
    idle_timeout_s: int = 300
    cooldown_s: int = 900
    scan_interval_s: int = 30
    # Ceiling on how stale a session can be and still count as "idle" (not
    # "dormant"). Sessions last touched more than this many seconds ago are
    # skipped entirely. ``0`` disables the ceiling. Default 24h.
    max_idle_s: int = 86_400
    # Grace window after scanner construction during which sessions whose
    # ``updated_at`` predates ``started_at`` are suppressed. Prevents the
    # reboot-storm: a fresh process has no memory of prior cooldowns, so
    # treating pre-existing idle sessions as fresh nudge targets would fire
    # every stale session at once. See issue #14.
    startup_grace_s: int = 300
    quiet_hours: QuietHours | None = None
    timezone: str = "UTC"
    channels: tuple[str, ...] = ("desktop_mate",)
    idle_prompt: str = _DEFAULT_IDLE_PROMPT
    # Reserved for Phase 5.5 (screen / process context injection).
    context_providers: tuple[Callable[[], Awaitable[str]], ...] = field(default_factory=tuple)


class _SessionManagerLike(Protocol):
    def list_sessions(self) -> list[dict[str, Any]]: ...
    def get_or_create(self, key: str) -> Any: ...


class _AgentLike(Protocol):
    _session_locks: dict[str, Any]

    async def process_direct(
        self,
        content: str,
        session_key: str = ...,
        channel: str = ...,
        chat_id: str = ...,
    ) -> Any: ...


class _CronLike(Protocol):
    on_job: Any

    def register_system_job(self, job: Any) -> Any: ...

    def enable_job(self, job_id: str, enabled: bool = True) -> Any: ...


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
        # Captured at construction, *not* first tick, so the grace applies
        # from launcher start rather than first scheduled scan.
        self._started_at: datetime = self._clock()

    async def scan_and_nudge(self) -> None:
        now = self._clock()
        if self._config.quiet_hours and _in_quiet_hours(now, self._config.quiet_hours):
            return

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
            updated_at = info.get("updated_at")
            if not self._is_idle(updated_at, now):
                continue
            if self._is_dormant(updated_at, now):
                continue
            if self._is_pre_start(updated_at, now):
                continue
            if self._is_in_cooldown(key, now):
                continue

            fresh = self._sessions.get_or_create(key)
            fresh_updated = getattr(fresh, "updated_at", None)
            if not self._is_idle(fresh_updated, now):
                continue
            if self._is_dormant(fresh_updated, now):
                continue

            # Mark cooldown BEFORE dispatch so exceptions don't cause retry storms.
            self._cooldown_until[key] = now.timestamp() + self._config.cooldown_s

            minutes = _minutes_between(fresh_updated, now)
            prompt = self._config.idle_prompt.format(minutes=minutes)
            try:
                await self._agent.process_direct(
                    prompt,
                    session_key=key,
                    channel=channel,
                    chat_id=chat_id,
                )
                logger.info("Idle nudge delivered: session={} idle={}m", key, minutes)
            except Exception:
                logger.exception("Idle nudge failed for {}", key)

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
        return until is not None and now.timestamp() < until

    def _is_dormant(self, ts: Any, now: datetime) -> bool:
        """Sessions idle beyond ``max_idle_s`` are dormant — never nudged."""
        max_idle = self._config.max_idle_s
        if max_idle <= 0 or ts is None:
            return False
        try:
            last = _to_aware(ts, self._tz)
        except (TypeError, ValueError):
            return False
        return (now - last).total_seconds() >= max_idle

    def _is_pre_start(self, ts: Any, now: datetime) -> bool:
        """Skip sessions idle from before this process started (see issue #14).

        Cooldown state is not persisted, so without this guard every session
        with ``updated_at < started_at`` would be indistinguishable from a
        legitimately-idle session on the first tick and get bulk-nudged.
        """
        grace = self._config.startup_grace_s
        if grace <= 0 or ts is None:
            return False
        if (now - self._started_at).total_seconds() >= grace:
            return False
        try:
            last = _to_aware(ts, self._tz)
        except (TypeError, ValueError):
            return False
        return last < self._started_at


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
        # Persisted job may exist from a previous run that had idle enabled.
        # Disable it on disk so nanobot's scheduler doesn't fire it and fall
        # through to the default agent-loop handler (see issue #14 follow-up).
        enable = getattr(cron, "enable_job", None)
        if callable(enable):
            try:
                result = enable(IDLE_SYSTEM_JOB_ID, False)
                if result is not None:
                    logger.info(
                        "Idle watcher disabled: persisted job '{}' set enabled=False",
                        IDLE_SYSTEM_JOB_ID,
                    )
            except Exception:
                logger.exception(
                    "Failed to disable persisted idle job '{}'", IDLE_SYSTEM_JOB_ID
                )
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


# ---------- helpers ----------------------------------------------------------


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
