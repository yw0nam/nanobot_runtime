"""Gateway launcher — monkey-patches AgentLoop to inject caller-supplied hooks.

Nanobot's CLI gateway does not expose a hook injection point; this module
pre-imports ``nanobot.agent.loop.AgentLoop`` and monkey-patches its
``__init__`` to append hooks returned by a caller-supplied factory, then
dispatches to nanobot's Typer CLI with the ``gateway`` subcommand so the
patch applies in-process to the AgentLoop constructed by the CLI.

Pinned to nanobot 0.1.5.x — version drift fails loud at startup.
"""
from __future__ import annotations

import inspect
import os
from typing import Any, Callable

import nanobot
from loguru import logger
from nanobot.agent.hook import AgentHook
from nanobot.agent.loop import AgentLoop
from nanobot.channels.manager import ChannelManager

_SUPPORTED_PREFIXES = ("0.1.5",)

HooksFactory = Callable[[AgentLoop], list[AgentHook]]


def _install_monkey_patch(hooks_factory: HooksFactory) -> None:
    if not nanobot.__version__.startswith(_SUPPORTED_PREFIXES):
        raise RuntimeError(
            f"nanobot_runtime.gateway: unsupported nanobot version "
            f"{nanobot.__version__}; validated against {_SUPPORTED_PREFIXES}. "
            "Review AgentLoop.__init__ internals before continuing."
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


def _install_channel_manager_patch() -> None:
    """Generalize ChannelManager's ``session_manager`` injection.

    Upstream only forwards ``session_manager`` to the built-in ``websocket``
    channel (``nanobot/channels/manager.py`` gates on ``cls.name == "websocket"``).
    We need the same dependency on ``desktop_mate`` for its REST surface.

    Rather than duplicate 25 lines of ``_init_channels``, we wrap it: before
    each class instantiation, inspect the constructor signature and, when it
    declares a ``session_manager`` parameter, inject our stored reference.
    This makes the fix generic for any future channel and trivially
    compatible with the upstream guard (we set the kwarg before upstream's
    conditional runs).
    """
    _orig_init_channels = ChannelManager._init_channels

    def _patched_init_channels(self: ChannelManager) -> None:
        from nanobot.channels.registry import discover_all

        sm = getattr(self, "_session_manager", None)
        if sm is None:
            _orig_init_channels(self)
            return

        # Temporarily wrap each discovered channel's ``__init__`` to default-
        # inject ``session_manager`` when the signature accepts it. Upstream's
        # guard only fires for ``cls.name == "websocket"`` — this expands it
        # to any channel that declares the kwarg (e.g. ``desktop_mate``).
        # Restored after the original ``_init_channels`` returns.
        patched: list[tuple[type, Any]] = []
        try:
            for cls in discover_all().values():
                try:
                    sig = inspect.signature(cls.__init__)
                except (TypeError, ValueError):
                    continue
                if "session_manager" not in sig.parameters:
                    continue
                orig_cls_init = cls.__init__

                def _make_wrapper(orig: Any) -> Any:
                    def _wrapped(inst: Any, *a: Any, **kw: Any) -> None:
                        kw.setdefault("session_manager", sm)
                        orig(inst, *a, **kw)

                    return _wrapped

                cls.__init__ = _make_wrapper(orig_cls_init)  # type: ignore[method-assign]
                patched.append((cls, orig_cls_init))
            _orig_init_channels(self)
        finally:
            for cls, orig in patched:
                cls.__init__ = orig  # type: ignore[method-assign]

    ChannelManager._init_channels = _patched_init_channels  # type: ignore[assignment]
    logger.debug(
        "nanobot_runtime: patched ChannelManager._init_channels for "
        "generic session_manager injection"
    )


def run(
    *,
    hooks_factory: HooksFactory,
    config_path: str | None = None,
    workspace: str | None = None,
) -> None:
    """Install the monkey-patch and dispatch to ``nanobot gateway``.

    ``config_path`` and ``workspace`` fall back to ``NANOBOT_CONFIG`` /
    ``NANOBOT_WORKSPACE`` env vars, then to ``./nanobot.json`` / ``.``.
    """
    _install_monkey_patch(hooks_factory)
    _install_channel_manager_patch()

    # Import after patch so any import-time side effects see the patched class.
    from nanobot.cli.commands import app

    resolved_config = config_path or os.getenv("NANOBOT_CONFIG", "./nanobot.json")
    resolved_workspace = workspace or os.getenv("NANOBOT_WORKSPACE", ".")
    app(
        args=["gateway", "--config", resolved_config, "--workspace", resolved_workspace],
        prog_name="nanobot",
        standalone_mode=False,
    )
