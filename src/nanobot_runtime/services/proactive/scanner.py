"""Idle-watcher scanner — single judgment gate per scan tick.

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
"""
from datetime import datetime
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from loguru import logger

from nanobot_runtime.config.idle import IdleConfig, QuietHours

try:
    from nanobot.bus.events import InboundMessage
except ImportError:  # pragma: no cover - allows isolated static analysis
    InboundMessage = None  # type: ignore[assignment]


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
