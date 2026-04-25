"""Unit tests for IdleScanner / install_idle_system_job.

The scanner is the only piece of Phase 5 Idle we own — cron scheduling,
session iteration, and outbound delivery are all nanobot-native. These
tests exercise the judgment gate (quiet hours, channel allowlist, idle
threshold, active-turn, cooldown, re-validation race) and the composite
on_job wrapper used to install the system job without clobbering any
existing cron callback.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from nanobot_runtime.proactive.idle import (
    IDLE_SYSTEM_JOB_ID,
    IdleConfig,
    IdleScanner,
    QuietHours,
    install_idle_system_job,
)

_TZ = ZoneInfo("Asia/Tokyo")


def _cfg(**overrides) -> IdleConfig:
    base = dict(
        enabled=True,
        idle_timeout_s=300,
        cooldown_s=900,
        scan_interval_s=30,
        quiet_hours=None,
        timezone="Asia/Tokyo",
        channels=("desktop_mate",),
        idle_prompt="[Idle {minutes}m] nudge",
    )
    base.update(overrides)
    return IdleConfig(**base)


def _iso_ago(now: datetime, seconds: int) -> str:
    return (now - timedelta(seconds=seconds)).isoformat()


def _session_info(key: str, now: datetime, age_s: int) -> dict:
    return {"key": key, "updated_at": _iso_ago(now, age_s), "created_at": _iso_ago(now, age_s + 10)}


def _fake_session(updated_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(updated_at=updated_at)


def _build_agent(locks: dict | None = None) -> MagicMock:
    agent = MagicMock()
    agent._session_locks = locks if locks is not None else {}
    agent.process_direct = AsyncMock()
    return agent


def _build_sessions(list_result: list[dict], fresh_by_key: dict | None = None) -> MagicMock:
    sessions = MagicMock()
    sessions.list_sessions.return_value = list_result
    fresh_by_key = fresh_by_key or {}

    def _get_or_create(key: str):
        return fresh_by_key.get(key) or _fake_session(datetime.fromisoformat(list_result[0]["updated_at"]))

    sessions.get_or_create.side_effect = _get_or_create
    return sessions


# ---------- Judgment-gate tests ------------------------------------------------


async def test_in_quiet_hours_suppresses_nudge() -> None:
    """Mid quiet-hours tick must not call process_direct even if a session is idle."""
    now = datetime(2026, 4, 22, 3, 0, tzinfo=_TZ)  # 03:00 JST
    config = _cfg(quiet_hours=QuietHours(start="02:00", end="07:00"))
    agent = _build_agent()
    sessions = _build_sessions([_session_info("desktop_mate:abc", now, age_s=3600)])

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: now)
    await scanner.scan_and_nudge()

    agent.process_direct.assert_not_called()


async def test_quiet_hours_spanning_midnight() -> None:
    """22:00-06:00 should treat 03:00 as quiet and 08:00 as non-quiet."""
    config = _cfg(quiet_hours=QuietHours(start="22:00", end="06:00"))
    agent = _build_agent()
    sessions = _build_sessions([_session_info("desktop_mate:abc", datetime(2026, 4, 22, 3, 0, tzinfo=_TZ), age_s=3600)])

    quiet_now = datetime(2026, 4, 22, 3, 0, tzinfo=_TZ)
    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: quiet_now)
    await scanner.scan_and_nudge()
    agent.process_direct.assert_not_called()

    # Same session but now 08:00 — quiet hours over.
    wake_now = datetime(2026, 4, 22, 8, 0, tzinfo=_TZ)
    sessions = _build_sessions([_session_info("desktop_mate:abc", wake_now, age_s=3600)])
    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: wake_now)
    await scanner.scan_and_nudge()
    agent.process_direct.assert_awaited_once()


async def test_idle_below_threshold_skipped() -> None:
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    config = _cfg(idle_timeout_s=300)
    agent = _build_agent()
    # Last message 60s ago — below 300s threshold.
    sessions = _build_sessions([_session_info("desktop_mate:abc", now, age_s=60)])

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: now)
    await scanner.scan_and_nudge()

    agent.process_direct.assert_not_called()


async def test_active_turn_skipped() -> None:
    """If a per-session Lock is held, the agent is mid-turn — skip."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    locked = asyncio.Lock()
    await locked.acquire()
    try:
        agent = _build_agent(locks={"desktop_mate:abc": locked})
        sessions = _build_sessions([_session_info("desktop_mate:abc", now, age_s=3600)])

        scanner = IdleScanner(agent=agent, sessions=sessions, config=_cfg(), clock=lambda: now)
        await scanner.scan_and_nudge()

        agent.process_direct.assert_not_called()
    finally:
        locked.release()


async def test_cooldown_blocks_second_nudge() -> None:
    """After a successful nudge, a second tick within cooldown must not fire again."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    config = _cfg(cooldown_s=900)
    agent = _build_agent()
    fresh = _fake_session(now - timedelta(seconds=3600))
    sessions = _build_sessions(
        [_session_info("desktop_mate:abc", now, age_s=3600)],
        fresh_by_key={"desktop_mate:abc": fresh},
    )

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: now)
    await scanner.scan_and_nudge()
    assert agent.process_direct.await_count == 1

    # 5 minutes later — still within cooldown; idle threshold still satisfied.
    later = now + timedelta(seconds=300)
    sessions.list_sessions.return_value = [_session_info("desktop_mate:abc", later, age_s=3900)]
    sessions.get_or_create.side_effect = lambda _k: _fake_session(later - timedelta(seconds=3900))
    scanner._clock = lambda: later
    await scanner.scan_and_nudge()
    assert agent.process_direct.await_count == 1  # unchanged


async def test_cooldown_expired_allows_second_nudge() -> None:
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    config = _cfg(cooldown_s=900)
    agent = _build_agent()
    sessions = _build_sessions(
        [_session_info("desktop_mate:abc", now, age_s=3600)],
        fresh_by_key={"desktop_mate:abc": _fake_session(now - timedelta(seconds=3600))},
    )

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: now)
    await scanner.scan_and_nudge()
    assert agent.process_direct.await_count == 1

    # 16 minutes later — cooldown window past, session still idle.
    later = now + timedelta(seconds=16 * 60)
    sessions.list_sessions.return_value = [_session_info("desktop_mate:abc", later, age_s=3600 + 16 * 60)]
    sessions.get_or_create.side_effect = lambda _k: _fake_session(later - timedelta(seconds=3600 + 16 * 60))
    scanner._clock = lambda: later
    await scanner.scan_and_nudge()
    assert agent.process_direct.await_count == 2


async def test_channel_not_in_allowlist_skipped() -> None:
    """Sessions on channels outside the allowlist are ignored."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    config = _cfg(channels=("desktop_mate",))
    agent = _build_agent()
    sessions = _build_sessions([_session_info("slack:C012:1234", now, age_s=3600)])

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: now)
    await scanner.scan_and_nudge()

    agent.process_direct.assert_not_called()


async def test_session_key_without_colon_skipped() -> None:
    """Malformed keys (e.g. 'cli', 'heartbeat') are not routable — skip safely."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    agent = _build_agent()
    sessions = _build_sessions([
        _session_info("heartbeat", now, age_s=3600),
        _session_info("cli", now, age_s=3600),
    ])

    scanner = IdleScanner(agent=agent, sessions=sessions, config=_cfg(), clock=lambda: now)
    await scanner.scan_and_nudge()

    agent.process_direct.assert_not_called()


async def test_revalidation_race_skipped() -> None:
    """If list_sessions shows idle but a fresh reload reveals recent activity, skip."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    agent = _build_agent()
    # list_sessions: 1h ago; but get_or_create shows activity 10s ago (user just sent a msg).
    sessions = _build_sessions(
        [_session_info("desktop_mate:abc", now, age_s=3600)],
        fresh_by_key={"desktop_mate:abc": _fake_session(now - timedelta(seconds=10))},
    )

    scanner = IdleScanner(agent=agent, sessions=sessions, config=_cfg(), clock=lambda: now)
    await scanner.scan_and_nudge()

    agent.process_direct.assert_not_called()


async def test_all_gates_pass_triggers_process_direct_with_correct_args() -> None:
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    config = _cfg(idle_prompt="You've been silent {minutes}m — say hi.")
    agent = _build_agent()
    sessions = _build_sessions(
        [_session_info("desktop_mate:chat-abc", now, age_s=600)],
        fresh_by_key={"desktop_mate:chat-abc": _fake_session(now - timedelta(seconds=600))},
    )

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=lambda: now)
    await scanner.scan_and_nudge()

    agent.process_direct.assert_awaited_once()
    call = agent.process_direct.await_args
    assert call.kwargs["session_key"] == "desktop_mate:chat-abc"
    assert call.kwargs["channel"] == "desktop_mate"
    assert call.kwargs["chat_id"] == "chat-abc"
    assert "10m" in call.args[0]  # 600s = 10 minutes


async def test_process_direct_exception_swallowed_and_cooldown_preserved() -> None:
    """A failing process_direct must not raise out of the scanner, but cooldown still sticks."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    agent = _build_agent()
    agent.process_direct.side_effect = RuntimeError("provider down")
    sessions = _build_sessions(
        [_session_info("desktop_mate:abc", now, age_s=3600)],
        fresh_by_key={"desktop_mate:abc": _fake_session(now - timedelta(seconds=3600))},
    )

    scanner = IdleScanner(agent=agent, sessions=sessions, config=_cfg(cooldown_s=900), clock=lambda: now)
    await scanner.scan_and_nudge()  # must not raise

    # Second tick in same minute — cooldown still marked so no retry storm.
    await scanner.scan_and_nudge()
    assert agent.process_direct.await_count == 1


# ---------- install_idle_system_job wiring tests ------------------------------


async def test_install_registers_system_job_and_wraps_on_job() -> None:
    agent = _build_agent()
    sessions = _build_sessions([])
    cron = MagicMock()
    cron.on_job = None

    install_idle_system_job(agent=agent, sessions=sessions, cron=cron, config=_cfg())

    cron.register_system_job.assert_called_once()
    registered = cron.register_system_job.call_args.args[0]
    assert registered.id == IDLE_SYSTEM_JOB_ID
    assert registered.schedule.kind == "every"
    assert registered.schedule.every_ms == 30_000
    assert registered.payload.kind == "system_event"
    assert cron.on_job is not None


async def test_install_composite_delegates_non_idle_jobs() -> None:
    """Existing cron.on_job must still run for jobs other than 'idle-watcher'."""
    agent = _build_agent()
    sessions = _build_sessions([])
    cron = MagicMock()
    original = AsyncMock(return_value="from-original")
    cron.on_job = original

    install_idle_system_job(agent=agent, sessions=sessions, cron=cron, config=_cfg())
    composite = cron.on_job

    other_job = SimpleNamespace(id="some-user-job", name="reminder")
    result = await composite(other_job)

    original.assert_awaited_once_with(other_job)
    assert result == "from-original"


async def test_install_disabled_config_is_noop() -> None:
    agent = _build_agent()
    sessions = _build_sessions([])
    cron = MagicMock()
    sentinel = AsyncMock()
    cron.on_job = sentinel

    install_idle_system_job(agent=agent, sessions=sessions, cron=cron, config=_cfg(enabled=False))

    cron.register_system_job.assert_not_called()
    assert cron.on_job is sentinel  # untouched


def test_is_in_cooldown_evicts_expired_entry() -> None:
    """_is_in_cooldown must remove expired entries from _cooldown_until.

    Regression: the method changed from a pure read to a read-with-eviction.
    Removing the ``del`` would silently pass higher-level cooldown tests while
    allowing stale keys to accumulate in long-running processes.
    """
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    scanner = IdleScanner(
        agent=_build_agent(),
        sessions=_build_sessions([]),
        config=_cfg(cooldown_s=900),
        clock=lambda: now,
    )
    key = "desktop_mate:abc"
    # Plant an already-expired cooldown entry (expired 1 second ago).
    scanner._cooldown_until[key] = now.timestamp() - 1

    # First call: expired → must return False and remove the key.
    assert scanner._is_in_cooldown(key, now) is False
    assert key not in scanner._cooldown_until

    # Second call: key is gone → must still return False (not KeyError).
    assert scanner._is_in_cooldown(key, now) is False


async def test_install_idle_job_invokes_scanner() -> None:
    """When composite receives the idle job id, it must trigger scan_and_nudge."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=_TZ)
    agent = _build_agent()
    sessions = _build_sessions(
        [_session_info("desktop_mate:abc", now, age_s=3600)],
        fresh_by_key={"desktop_mate:abc": _fake_session(now - timedelta(seconds=3600))},
    )
    cron = MagicMock()
    cron.on_job = None

    install_idle_system_job(
        agent=agent, sessions=sessions, cron=cron, config=_cfg(), clock=lambda: now
    )
    composite = cron.on_job
    idle_job = SimpleNamespace(id=IDLE_SYSTEM_JOB_ID, name="idle-watcher")
    await composite(idle_job)

    agent.process_direct.assert_awaited_once()
