"""Installer entry points for the idle-watcher.

Two install paths are exposed:

* :func:`install_idle_system_job` — registers a nanobot system cron job and
  wraps ``cron.on_job`` to dispatch idle ticks to the scanner.
* :func:`install_idle_asyncio_task` — installs the watcher as a free-standing
  ``asyncio`` task on the agent, sidestepping cron entirely.

Both are imported by the gateway launcher; only one should run per process.
"""
import asyncio
from datetime import datetime
from typing import Any, Callable

from loguru import logger

from nanobot_runtime.config.idle import IdleConfig
from nanobot_runtime.services.proactive.scanner import (
    IdleScanner,
    _AgentLike,
    _CronLike,
    _SessionManagerLike,
)

try:
    from nanobot.cron.types import CronJob, CronPayload, CronSchedule
except ImportError:  # pragma: no cover - allows isolated static analysis
    CronJob = CronPayload = CronSchedule = None  # type: ignore[assignment]


IDLE_SYSTEM_JOB_ID = "idle-watcher"
IDLE_ASYNCIO_TASK_ATTR = "_yuri_idle_scanner_starter"


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


def install_idle_asyncio_task(
    *,
    agent: Any,
    sessions: _SessionManagerLike,
    config: IdleConfig,
    clock: Callable[[], datetime] | None = None,
) -> IdleScanner | None:
    """Install the idle watcher as a free-standing asyncio task on ``agent``.

    Why: nanobot's ``cli/commands.py`` reassigns ``cron.on_job`` *after*
    ``AgentLoop.__init__`` returns, which silently overwrites the composite
    that :func:`install_idle_system_job` installs during ``hooks_factory``.
    The cron path therefore never reaches ``scan_and_nudge`` in a real
    gateway boot. This installer sidesteps cron entirely: the gateway
    monkey-patch wraps ``AgentLoop.run`` and starts the stashed coroutine
    once the event loop is live, so the watcher runs on its own ``asyncio``
    timer with no third-party touch points.

    Returns the scanner (so tests/manual triggers still work) or ``None``
    when ``config.enabled`` is False.

    Side effect: sets ``agent`` attribute :data:`IDLE_ASYNCIO_TASK_ATTR` to
    a zero-arg coroutine factory. The gateway patch spawns it exactly once;
    if no patch is installed, the watcher is silently inert (matches the
    "disabled" contract).
    """
    if not config.enabled:
        return None

    scanner = IdleScanner(agent=agent, sessions=sessions, config=config, clock=clock)

    async def _scanner_loop() -> None:
        logger.info(
            "Idle watcher started (timeout={}s cooldown={}s scan={}s channels={})",
            config.idle_timeout_s,
            config.cooldown_s,
            config.scan_interval_s,
            list(config.channels),
        )
        try:
            while True:
                await asyncio.sleep(config.scan_interval_s)
                try:
                    await scanner.scan_and_nudge()
                except Exception:
                    # ``scan_and_nudge`` already swallows per-target failures;
                    # this guard catches programming errors (bad config,
                    # missing deps) so one bad tick can't kill the watcher.
                    logger.exception("Idle watcher tick failed")
        except asyncio.CancelledError:
            logger.info("Idle watcher cancelled")
            raise

    setattr(agent, IDLE_ASYNCIO_TASK_ATTR, _scanner_loop)
    return scanner
