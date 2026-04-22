"""Integration: nanobot discovers and configures DesktopMateChannel end-to-end.

These tests verify the three points where our code meets nanobot's
installed package — if any of them break, the gateway won't start and
silent failure is likely. Run once per environment change.
"""
from __future__ import annotations

from importlib.metadata import entry_points


def test_desktop_mate_entry_point_registered():
    """``nanobot.channels`` group must advertise our channel."""
    found = {ep.name: ep.value for ep in entry_points(group="nanobot.channels")}
    assert "desktop_mate" in found
    assert found["desktop_mate"] == (
        "nanobot_runtime.channels.desktop_mate:DesktopMateChannel"
    )


def test_nanobot_discover_all_includes_desktop_mate():
    """nanobot's registry must surface our plugin next to built-ins."""
    from nanobot.channels.registry import discover_all

    discovered = discover_all()
    assert "desktop_mate" in discovered
    # Sanity: the resolved class is our implementation.
    from nanobot_runtime.channels.desktop_mate import DesktopMateChannel

    assert discovered["desktop_mate"] is DesktopMateChannel


def test_channels_config_preserves_desktop_mate_section_as_dict():
    """``channels.desktop_mate`` must survive pydantic validation intact so
    ChannelManager can pass it to our __init__ as a dict section.
    """
    from nanobot.config.schema import ChannelsConfig

    cfg = ChannelsConfig.model_validate({
        "desktop_mate": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8765,
            "allowFrom": ["*"],
            "pingIntervalS": 15.0,
            "maxMessageBytes": 4_000_000,
        }
    })

    section = getattr(cfg, "desktop_mate")
    assert isinstance(section, dict)
    assert section["enabled"] is True
    # camelCase keys preserved verbatim for our _coerce_config to pick up.
    assert section["allowFrom"] == ["*"]
    assert section["pingIntervalS"] == 15.0
    assert section["maxMessageBytes"] == 4_000_000
