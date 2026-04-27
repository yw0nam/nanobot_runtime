"""Smoke test: gateway's ChannelManager patch wires session_manager into DesktopMateChannel.

Nanobot's upstream ``ChannelManager._init_channels`` only forwards
``session_manager`` to the built-in ``websocket`` channel. Our gateway
patches the method to forward it to any channel whose ``__init__``
declares the kwarg — including ``desktop_mate``. This test verifies the
patch actually takes effect by constructing a ``ChannelManager`` with a
fake session manager and confirming the channel received it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


from nanobot_runtime.services.channels.desktop_mate import DesktopMateChannel
from nanobot_runtime.gateway import _install_channel_manager_patch


class _FakeBus:
    async def publish_inbound(self, msg: Any) -> None:  # pragma: no cover
        pass

    async def publish_outbound(self, msg: Any) -> None:  # pragma: no cover
        pass


def test_gateway_patch_injects_session_manager_into_desktop_mate():
    """After the patch runs, a ChannelManager built with a session_manager
    must forward it to DesktopMateChannel at construction time.
    """
    # Install the patch. Idempotent under repeat calls is not asserted —
    # in a full run the gateway installs it exactly once at startup.
    _install_channel_manager_patch()

    from nanobot.channels.manager import ChannelManager
    from nanobot.config.schema import Config

    # Build the minimum config that enables only desktop_mate. Other sections
    # default-disabled; we don't want websocket or slack contending for ports.
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {"model": "test-model"},
            },
            "channels": {
                "desktop_mate": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 0,
                    "path": "/ws",
                    "token": "secret",
                }
            },
        }
    )

    fake_sm = MagicMock()
    fake_sm.list_sessions.return_value = []

    mgr = ChannelManager(config, _FakeBus(), session_manager=fake_sm)

    channel = mgr.channels.get("desktop_mate")
    assert isinstance(
        channel, DesktopMateChannel
    ), "desktop_mate channel did not initialise; patch may have regressed"
    assert (
        channel._session_manager is fake_sm
    ), "gateway patch failed to inject session_manager into DesktopMateChannel"
