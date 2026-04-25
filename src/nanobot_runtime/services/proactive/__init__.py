"""Proactive (agent-initiated) machinery for nanobot workspaces.

Currently exposes the Phase 5-A Idle Watcher. Schedule/Cron is handled by
nanobot-native ``CronService`` with no extra glue on our side.
"""
from nanobot_runtime.services.proactive.idle import (
    IDLE_ASYNCIO_TASK_ATTR,
    IDLE_SYSTEM_JOB_ID,
    IdleConfig,
    IdleScanner,
    QuietHours,
    install_idle_asyncio_task,
    install_idle_system_job,
)
