"""Integration: real CronService drives the idle scanner.

Scheduler loop, job persistence, and timer arming are all nanobot-native.
This test plugs :func:`install_idle_system_job` into a real
``CronService`` to prove the glue registers, fires, and routes correctly
without a gateway subprocess.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule

from nanobot_runtime.config.idle import IdleConfig
from nanobot_runtime.services.proactive.installer import (
    IDLE_SYSTEM_JOB_ID,
    install_idle_system_job,
)


async def test_real_cron_fires_scanner_and_nudges_idle_session(tmp_path) -> None:
    now = datetime(2026, 4, 22, 14, 0, tzinfo=ZoneInfo("UTC"))

    agent = MagicMock()
    agent._session_locks = {}
    agent.bus = MagicMock()
    agent.bus.publish_inbound = AsyncMock()

    sessions = MagicMock()
    sessions.list_sessions.return_value = [
        {"key": "desktop_mate:abc", "updated_at": (now - timedelta(seconds=3600)).isoformat()},
    ]
    sessions.get_or_create.return_value = SimpleNamespace(
        updated_at=now - timedelta(seconds=3600),
    )

    cron = CronService(tmp_path / "cron" / "jobs.json")
    await cron.start()
    try:
        install_idle_system_job(
            agent=agent,
            sessions=sessions,
            cron=cron,
            config=IdleConfig(
                enabled=True,
                idle_timeout_s=300,
                cooldown_s=900,
                scan_interval_s=1,
                startup_grace_s=0,
                quiet_hours=None,
                timezone="UTC",
                channels=("desktop_mate",),
            ),
            clock=lambda: now,
        )
        # Allow at least two tick cycles — 1s each. Within cooldown so the
        # second tick sees the cooldown and skips; first should nudge once.
        await asyncio.sleep(2.5)
    finally:
        cron.stop()

    agent.bus.publish_inbound.assert_awaited_once()
    msg = agent.bus.publish_inbound.await_args.args[0]
    assert msg.session_key_override == "desktop_mate:abc"
    assert msg.channel == "desktop_mate"
    assert msg.chat_id == "abc"
    assert msg.metadata.get("proactive") is True
    assert msg.metadata.get("_wants_stream") is True


async def test_real_cron_preserves_existing_on_job(tmp_path) -> None:
    """A pre-existing cron.on_job must still handle non-idle jobs after install."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=ZoneInfo("UTC"))
    user_callback = AsyncMock(return_value=None)

    cron = CronService(tmp_path / "cron" / "jobs.json")
    cron.on_job = user_callback
    await cron.start()
    try:
        idle_agent = MagicMock(_session_locks={})
        idle_agent.bus = MagicMock()
        idle_agent.bus.publish_inbound = AsyncMock()
        install_idle_system_job(
            agent=idle_agent,
            sessions=MagicMock(list_sessions=MagicMock(return_value=[]), get_or_create=MagicMock()),
            cron=cron,
            config=IdleConfig(
                enabled=True,
                scan_interval_s=60,  # large so it doesn't race the user job
                startup_grace_s=0,
                quiet_hours=None,
                timezone="UTC",
                channels=("desktop_mate",),
            ),
            clock=lambda: now,
        )
        # Add an ordinary "every 1s" user job and wait for it to fire.
        cron.add_job(
            name="user-reminder",
            schedule=CronSchedule(kind="every", every_ms=1000),
            message="say hi",
        )
        await asyncio.sleep(1.8)
    finally:
        cron.stop()

    # User callback must have been invoked at least once, with its own CronJob.
    assert user_callback.await_count >= 1
    fired_jobs = [c.args[0] for c in user_callback.await_args_list]
    assert all(isinstance(j, CronJob) for j in fired_jobs)
    assert all(j.name == "user-reminder" for j in fired_jobs)
