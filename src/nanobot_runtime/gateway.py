"""Gateway launcher — monkey-patches AgentLoop to inject caller-supplied hooks.

Nanobot's CLI gateway does not expose a hook injection point; this module
wraps ``nanobot.cli.commands._run_gateway`` with a pre-import monkey-patch
on ``AgentLoop.__init__`` that appends hooks returned by a caller-supplied
factory. Workspaces import ``run`` and pass their own hooks_factory.

Pinned to nanobot 0.1.5.x — version drift fails loud at install time.
"""
from __future__ import annotations

import os
from typing import Any, Callable

import nanobot
from loguru import logger
from nanobot.agent.hook import AgentHook
from nanobot.agent.loop import AgentLoop

_SUPPORTED_PREFIXES = ("0.1.5",)

HooksFactory = Callable[[AgentLoop], list[AgentHook]]


def _install_monkey_patch(hooks_factory: HooksFactory) -> None:
    if not nanobot.__version__.startswith(_SUPPORTED_PREFIXES):
        raise RuntimeError(
            f"nanobot_runtime.gateway: unsupported nanobot version "
            f"{nanobot.__version__}; validated against {_SUPPORTED_PREFIXES}. "
            "Review AgentLoop.__init__ and _run_gateway internals before continuing."
        )

    _orig_init = AgentLoop.__init__

    def _patched_init(self: AgentLoop, *args: Any, **kwargs: Any) -> None:
        _orig_init(self, *args, **kwargs)
        added = hooks_factory(self)
        self._extra_hooks = list(self._extra_hooks) + added
        logger.info(
            "nanobot_runtime: injected {} hook(s): {}",
            len(added),
            [type(h).__name__ for h in added],
        )

    AgentLoop.__init__ = _patched_init  # type: ignore[assignment]


def run(
    *,
    hooks_factory: HooksFactory,
    config_path: str | None = None,
    workspace: str | None = None,
) -> None:
    """Install the monkey-patch and launch nanobot's gateway.

    ``config_path`` and ``workspace`` fall back to ``NANOBOT_CONFIG`` /
    ``NANOBOT_WORKSPACE`` env vars, then to ``./nanobot.json`` / ``.``.
    """
    _install_monkey_patch(hooks_factory)

    # Import after patch so any import-time side effects see the patched class.
    from nanobot.cli.commands import _load_runtime_config, _run_gateway

    cfg = _load_runtime_config(
        config_path or os.getenv("NANOBOT_CONFIG", "./nanobot.json"),
        workspace or os.getenv("NANOBOT_WORKSPACE", "."),
    )
    _run_gateway(cfg)
