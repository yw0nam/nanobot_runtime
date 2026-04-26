"""Tests for the channel-mode loader and lookup.

Covers the enum's string round-trip (since the YAML stores raw strings),
the loader's failure modes (which surface as boot-time ValueErrors), and
the map's lookup semantics (None / unknown channel both fall through to
the configured default).
"""
from pathlib import Path

import pytest
import yaml

from nanobot_runtime.services.tts.modes import (
    ChannelModeMap,
    TTSMode,
    load_channel_modes,
)


# ── TTSMode enum ──────────────────────────────────────────────────────


class TestTTSMode:
    def test_streaming_value_is_lowercase_string(self):
        assert TTSMode.STREAMING.value == "streaming"

    def test_attachment_value_is_lowercase_string(self):
        assert TTSMode.ATTACHMENT.value == "attachment"

    def test_none_value_is_lowercase_string(self):
        assert TTSMode.NONE.value == "none"

    def test_constructed_from_string_round_trips(self):
        assert TTSMode("streaming") is TTSMode.STREAMING
        assert TTSMode("attachment") is TTSMode.ATTACHMENT
        assert TTSMode("none") is TTSMode.NONE

    def test_unknown_string_raises_value_error(self):
        with pytest.raises(ValueError):
            TTSMode("bogus")


# ── ChannelModeMap.lookup ─────────────────────────────────────────────


class TestChannelModeMapLookup:
    def test_known_channel_returns_mapped_mode(self):
        m = ChannelModeMap(
            default=TTSMode.NONE,
            channels={"desktop_mate": TTSMode.STREAMING, "telegram": TTSMode.ATTACHMENT},
        )
        assert m.lookup("desktop_mate") is TTSMode.STREAMING
        assert m.lookup("telegram") is TTSMode.ATTACHMENT

    def test_unknown_channel_returns_default(self):
        m = ChannelModeMap(default=TTSMode.NONE, channels={"desktop_mate": TTSMode.STREAMING})
        assert m.lookup("slack") is TTSMode.NONE

    def test_none_returns_default(self):
        m = ChannelModeMap(default=TTSMode.STREAMING, channels={})
        assert m.lookup(None) is TTSMode.STREAMING

    def test_default_field_default_is_none(self):
        # ChannelModeMap() with no args = all-NONE map (used for empty YAML files).
        m = ChannelModeMap()
        assert m.default is TTSMode.NONE
        assert m.channels == {}
        assert m.lookup("anything") is TTSMode.NONE


# ── load_channel_modes ────────────────────────────────────────────────


class TestLoadChannelModes:
    def test_happy_path_parses_default_and_channels(self, tmp_path: Path):
        p = tmp_path / "modes.yml"
        p.write_text(
            "default: none\n"
            "channels:\n"
            "  desktop_mate: streaming\n"
            "  telegram: attachment\n"
            "  slack: none\n"
        )
        m = load_channel_modes(p)
        assert m.default is TTSMode.NONE
        assert m.channels == {
            "desktop_mate": TTSMode.STREAMING,
            "telegram": TTSMode.ATTACHMENT,
            "slack": TTSMode.NONE,
        }

    def test_empty_file_returns_all_none_map(self, tmp_path: Path):
        p = tmp_path / "modes.yml"
        p.write_text("")
        m = load_channel_modes(p)
        assert m.default is TTSMode.NONE
        assert m.channels == {}

    def test_missing_default_key_implies_none(self, tmp_path: Path):
        p = tmp_path / "modes.yml"
        p.write_text("channels:\n  desktop_mate: streaming\n")
        m = load_channel_modes(p)
        assert m.default is TTSMode.NONE
        assert m.channels == {"desktop_mate": TTSMode.STREAMING}

    def test_missing_channels_key_returns_empty_dict(self, tmp_path: Path):
        p = tmp_path / "modes.yml"
        p.write_text("default: streaming\n")
        m = load_channel_modes(p)
        assert m.default is TTSMode.STREAMING
        assert m.channels == {}

    def test_unknown_default_mode_raises_value_error_with_path(self, tmp_path: Path):
        p = tmp_path / "modes.yml"
        p.write_text("default: streamign\n")
        with pytest.raises(ValueError) as ei:
            load_channel_modes(p)
        msg = str(ei.value)
        assert "streamign" in msg
        assert "default" in msg
        assert str(p) in msg

    def test_unknown_channel_mode_raises_value_error_with_channel_name(self, tmp_path: Path):
        p = tmp_path / "modes.yml"
        p.write_text("channels:\n  telegram: brodcast\n")
        with pytest.raises(ValueError) as ei:
            load_channel_modes(p)
        msg = str(ei.value)
        assert "brodcast" in msg
        assert "telegram" in msg
        assert str(p) in msg

    def test_yaml_parse_error_propagates(self, tmp_path: Path):
        # Truly invalid YAML — unclosed flow sequence raises
        # yaml.scanner.ScannerError (a subclass of yaml.YAMLError).
        p = tmp_path / "modes.yml"
        p.write_text('default: "unterminated\n')
        with pytest.raises(yaml.YAMLError):
            load_channel_modes(p)

    def test_channels_not_a_mapping_raises_value_error(self, tmp_path: Path):
        # `channels:` is a list, not a mapping. Loader must catch this
        # before iterating (.items() on a list would AttributeError).
        p = tmp_path / "modes.yml"
        p.write_text("channels:\n  - desktop_mate\n  - slack\n")
        with pytest.raises(ValueError) as ei:
            load_channel_modes(p)
        msg = str(ei.value)
        assert "channels" in msg
        assert "must be a mapping" in msg
        assert "list" in msg
        assert str(p) in msg

    def test_top_level_not_a_mapping_raises_value_error(self, tmp_path: Path):
        p = tmp_path / "modes.yml"
        p.write_text("[1, 2, 3]\n")
        with pytest.raises(ValueError) as ei:
            load_channel_modes(p)
        msg = str(ei.value)
        assert "Top-level YAML must be a mapping" in msg
        assert "list" in msg
        assert str(p) in msg

    def test_file_not_found_raises(self, tmp_path: Path):
        p = tmp_path / "missing.yml"
        with pytest.raises(FileNotFoundError):
            load_channel_modes(p)
