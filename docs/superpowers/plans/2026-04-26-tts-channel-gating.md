# TTS Channel Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `TTSHook` from synthesizing audio for inbound turns on channels that can't play it (Slack, Discord, future text-only channels) by gating per-channel via a workspace YAML.

**Architecture:** Add a `TTSMode` enum + `ChannelModeMap` Pydantic model loaded from `resources/tts_channel_modes.yml`. Inject the map into `LazyChannelTTSSink`. The sink's `is_enabled(session_key)` extracts the channel name from nanobot's `<channel>:<chat_id>` session-key convention and short-circuits when the mode isn't `STREAMING`. `TTSHook` is touched in three places to thread `state.session_key` into the sink check. No nanobot upstream changes. ATTACHMENT mode is data-model-only this PR — no implementation.

**Tech Stack:** Python 3.10+, Pydantic v2, `yaml.safe_load`, pytest, loguru. Existing repo: `nanobot_runtime`. Companion workspace: `../yuri`.

**Reference:** Full design in `docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md`.

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `src/nanobot_runtime/services/tts/modes.py` | CREATE | `TTSMode` enum, frozen `ChannelModeMap`, `load_channel_modes(path)` YAML loader. Pure data + I/O, no runtime dependencies. |
| `tests/services/tts/test_modes.py` | CREATE | Loader + lookup unit tests. |
| `src/nanobot_runtime/services/channels/desktop_mate.py` | MODIFY | Add `_channel_from_session_key` helper. Inject `mode_map` into `LazyChannelTTSSink.__init__`. Rewrite `is_enabled(session_key)` to gate on mode before falling through to existing channel-readiness check. |
| `tests/services/channels/test_desktop_mate.py` | MODIFY | Add `Test_ChannelFromSessionKey` and `TestLazyChannelTTSSinkIsEnabled` classes (existing send-frame tests untouched). |
| `src/nanobot_runtime/services/hooks/tts.py` | MODIFY | `TTSSink` Protocol comment-spec gains optional `session_key` arg. `_sink_is_enabled` accepts and forwards it. `_dispatch_sentence` passes `state.session_key`. `_synth_and_emit` gains `session_key` kwarg for second-chance check. |
| `tests/services/hooks/test_tts_hook.py` | MODIFY | Add `TestTTSHookSessionKeyPlumbing`. |
| `src/nanobot_runtime/launcher.py` | MODIFY | Add `_resolve_tts_modes_path()`. In `_build_tts_hook()`: read modes YAML (FileNotFoundError if missing), inject `mode_map` into `LazyChannelTTSSink`. Rename TTS env vars (`YURI_TTS_*` → `TTS_*`). |
| `tests/test_launcher.py` | MODIFY | Rename `YURI_TTS_RULES_PATH` → `TTS_RULES_PATH` in existing test. Extend `_clear_yuri_env` autouse fixture to also strip `TTS_*`. Add `TestResolveTtsModesPath` and `TestBuildTtsHookFailsWhenModesMissing`. |
| `tests/regression/harness.py` | MODIFY | Update `DirectSink.is_enabled` signature to accept `session_key: str \| None = None` (body unchanged — channel readiness doesn't need it, but the new TTSHook call site passes the key positionally). |
| `tests/e2e/conftest.py` | MODIFY | Line ~139: `YURI_TTS_URL` → `TTS_URL`. |
| `tests/e2e/README.md` | MODIFY | Line ~20: doc reference `YURI_TTS_URL` → `TTS_URL`. |
| `docs/setup.md` | MODIFY | Lines 34, 35, 175, 176: `YURI_TTS_ENABLED` / `YURI_TTS_URL` → `TTS_ENABLED` / `TTS_URL`. |
| `../yuri/.env` | MODIFY | Workspace `.env` — rename `YURI_TTS_*` → `TTS_*`. |
| `../yuri/resources/tts_channel_modes.yml` | CREATE | Workspace mode declaration (default + per-channel). |

`docs/operations.md`, top-level `README.md`, and other lines in `tests/e2e/README.md` reference only `YURI_IDLE_*` / `YURI_WORKSPACE`, which are out of scope for this PR per the spec §2 non-goals.

**Branch:** Continue on `feat/tts-channel-gating` (already contains the spec). All commits land on this branch; merge as a single PR.

---

## Task 1: `modes.py` — data model + YAML loader

Pure-Python file with the enum, the frozen Pydantic map, and the loader. No external runtime state, perfectly TDD-able.

**Files:**

- Create: `src/nanobot_runtime/services/tts/modes.py`
- Create: `tests/services/tts/test_modes.py`

### Step 1.1: Write failing tests for `TTSMode` enum

- [ ] Create `tests/services/tts/test_modes.py` with this content:

```python
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
```

### Step 1.2: Run to verify failure

- [ ] Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && pytest tests/services/tts/test_modes.py -v`
- [ ] Expected: collection error or ImportError — `nanobot_runtime.services.tts.modes` module does not exist.

### Step 1.3: Implement `TTSMode` enum

- [ ] Create `src/nanobot_runtime/services/tts/modes.py`:

```python
"""Channel → TTS mode mapping loaded from a workspace YAML.

The launcher loads this once at boot and injects the resulting
``ChannelModeMap`` into ``LazyChannelTTSSink``. The hook never sees the
map directly — it only calls ``sink.is_enabled(session_key)``, and the
sink consults the map to decide.

YAML shape::

    default: none
    channels:
      desktop_mate: streaming
      telegram: attachment   # mode declared; ATTACHMENT pipeline TBD
      slack: none

Channels not listed default to the value of ``default:`` (which itself
defaults to ``none`` if absent). Unknown mode strings raise ``ValueError``
at boot so an operator typo fails loud.
"""
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class TTSMode(str, Enum):
    """TTS dispatch mode per channel.

    Inherits from ``str`` for trivial YAML round-tripping and Pydantic
    field coercion.
    """

    STREAMING = "streaming"
    ATTACHMENT = "attachment"
    NONE = "none"
```

### Step 1.4: Run to verify TTSMode tests pass

- [ ] Run: `pytest tests/services/tts/test_modes.py::TestTTSMode -v`
- [ ] Expected: 5 PASS.

### Step 1.5: Add failing tests for `ChannelModeMap.lookup`

- [ ] Append to `tests/services/tts/test_modes.py`:

```python
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
```

### Step 1.6: Run to verify failure

- [ ] Run: `pytest tests/services/tts/test_modes.py::TestChannelModeMapLookup -v`
- [ ] Expected: ImportError on `ChannelModeMap`.

### Step 1.7: Implement `ChannelModeMap`

- [ ] Append to `src/nanobot_runtime/services/tts/modes.py`:

```python
class ChannelModeMap(BaseModel):
    """Resolves channel name → TTS mode. Frozen post-construction."""

    model_config = ConfigDict(frozen=True)

    default: TTSMode = Field(
        default=TTSMode.NONE,
        description="Mode for channels not listed explicitly in `channels`.",
    )
    channels: dict[str, TTSMode] = Field(
        default_factory=dict,
        description="Explicit channel-name → TTSMode mapping.",
    )

    def lookup(self, channel_name: str | None) -> TTSMode:
        """Return the mode for ``channel_name``, or ``default`` if unknown.

        ``None`` (e.g. session_key was None upstream — Slack DM, Telegram
        non-topic) maps to ``default``.
        """
        if channel_name is None:
            return self.default
        return self.channels.get(channel_name, self.default)
```

### Step 1.8: Run to verify ChannelModeMap tests pass

- [ ] Run: `pytest tests/services/tts/test_modes.py::TestChannelModeMapLookup -v`
- [ ] Expected: 4 PASS.

### Step 1.9: Add failing tests for `load_channel_modes`

- [ ] Append to `tests/services/tts/test_modes.py`:

```python
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
```

### Step 1.10: Run to verify failure

- [ ] Run: `pytest tests/services/tts/test_modes.py::TestLoadChannelModes -v`
- [ ] Expected: ImportError on `load_channel_modes`.

### Step 1.11: Implement `load_channel_modes`

- [ ] Append to `src/nanobot_runtime/services/tts/modes.py`:

```python
def load_channel_modes(path: str | Path) -> ChannelModeMap:
    """Parse the YAML at ``path`` into a ``ChannelModeMap``.

    - File missing → ``FileNotFoundError`` (caller decides whether to
      fail or skip; the launcher fails loud at boot).
    - Empty file → ``ChannelModeMap()`` (all-NONE; via Pydantic field
      defaults, no special-case in this loader).
    - Missing ``default:`` or ``channels:`` keys → field defaults apply.
    - Invalid mode strings → ``ValueError`` naming the field, value, and
      file path so an operator typo is immediately actionable.
    - Top-level not a mapping (e.g. ``[1, 2, 3]``) → ``ValueError``.
    - ``channels:`` not a mapping (e.g. a list) → ``ValueError``. Caught
      explicitly so the operator gets a clear message instead of an
      AttributeError on ``.items()``.

    The loader's only job is YAML I/O, type-shape validation, and
    converting raw mode strings to ``TTSMode`` enum values. It never
    substitutes defaults itself — Pydantic field defaults handle absence.
    """
    p = Path(path)
    raw = yaml.safe_load(p.read_text())
    if raw is None:
        return ChannelModeMap()
    if not isinstance(raw, dict):
        raise ValueError(
            f"Top-level YAML must be a mapping in {p}, got {type(raw).__name__}"
        )

    kwargs: dict[str, object] = {}

    if "default" in raw:
        try:
            kwargs["default"] = TTSMode(raw["default"])
        except ValueError as e:
            raise ValueError(
                f"Invalid TTS mode {raw['default']!r} for 'default' in {p}"
            ) from e

    if "channels" in raw and raw["channels"] is not None:
        chans_raw = raw["channels"]
        if not isinstance(chans_raw, dict):
            raise ValueError(
                f"'channels' must be a mapping in {p}, got {type(chans_raw).__name__}"
            )
        channels: dict[str, TTSMode] = {}
        for name, mode_str in chans_raw.items():
            try:
                channels[name] = TTSMode(mode_str)
            except ValueError as e:
                raise ValueError(
                    f"Invalid TTS mode {mode_str!r} for channel {name!r} in {p}"
                ) from e
        kwargs["channels"] = channels

    return ChannelModeMap(**kwargs)
```

### Step 1.12: Run all `test_modes.py` to verify pass

- [ ] Run: `pytest tests/services/tts/test_modes.py -v`
- [ ] Expected: 19 PASS, 0 FAIL. (5 TTSMode + 4 ChannelModeMapLookup + 10 LoadChannelModes.)

### Step 1.13: Commit

- [ ] Run:

```bash
git add src/nanobot_runtime/services/tts/modes.py tests/services/tts/test_modes.py
git commit -m "$(cat <<'EOF'
feat(tts): TTSMode enum + ChannelModeMap + YAML loader

Pure data + I/O module. No runtime dependencies on hooks or channels.
Loader fails loud on unknown mode strings (ValueError naming the field,
value, and file path); empty files and missing keys silently fall through
to Pydantic field defaults (default=NONE, channels={}).

Refs: docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md §5.1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `_channel_from_session_key` helper in `desktop_mate.py`

Pure helper extracting the channel-name prefix from nanobot's `<channel>:<chat_id>[:...]` session-key convention. Tested in isolation before being used by the sink.

**Files:**

- Modify: `src/nanobot_runtime/services/channels/desktop_mate.py` (add helper near `LazyChannelTTSSink`)
- Modify: `tests/services/channels/test_desktop_mate.py` (add `Test_ChannelFromSessionKey` class)

### Step 2.1: Write failing tests

- [ ] Open `tests/services/channels/test_desktop_mate.py`. Find the existing import line for `desktop_mate` symbols and add `_channel_from_session_key`. Add this test class at the bottom of the file:

```python
# ── _channel_from_session_key ─────────────────────────────────────────


class Test_ChannelFromSessionKey:
    """Helper that extracts the channel-name prefix from nanobot's
    '<channel>:<chat_id>[:...]' session_key convention."""

    def test_extracts_prefix_from_slack_thread_form(self):
        assert _channel_from_session_key("slack:C123:T456") == "slack"

    def test_extracts_prefix_from_simple_form(self):
        assert _channel_from_session_key("desktop_mate:abc") == "desktop_mate"

    def test_returns_none_for_none_input(self):
        assert _channel_from_session_key(None) is None

    def test_returns_none_for_empty_string(self):
        assert _channel_from_session_key("") is None

    def test_returns_none_for_no_colon_input(self):
        assert _channel_from_session_key("weird") is None
```

Add the import at the top of the test file alongside other `desktop_mate` imports:

```python
from nanobot_runtime.services.channels.desktop_mate import _channel_from_session_key
```

### Step 2.2: Run to verify failure

- [ ] Run: `pytest tests/services/channels/test_desktop_mate.py::Test_ChannelFromSessionKey -v`
- [ ] Expected: ImportError on `_channel_from_session_key`.

### Step 2.3: Implement helper

- [ ] Open `src/nanobot_runtime/services/channels/desktop_mate.py`. Find the line `class LazyChannelTTSSink:` (around line 324). Immediately before that class, add this module-level helper:

```python
def _channel_from_session_key(session_key: str | None) -> str | None:
    """Extract the channel-name prefix from nanobot's '<channel>:<chat_id>[:...]' form.

    nanobot constructs session keys like ``"slack:C123:T456"`` (slack.py:345),
    ``"telegram:42:topic:7"`` (telegram.py:792), and ``"desktop_mate:<chat_id>"``.
    The prefix up to the first colon is the channel name; everything after is
    chat- or thread-scoped opaque data.

    Returns ``None`` for ``None`` input, an empty string, or a string with no
    colon — these all resolve to the channel-mode map's ``default`` mode at
    the call site.
    """
    if not session_key:
        return None
    prefix, sep, _ = session_key.partition(":")
    return prefix if sep else None
```

### Step 2.4: Run to verify pass

- [ ] Run: `pytest tests/services/channels/test_desktop_mate.py::Test_ChannelFromSessionKey -v`
- [ ] Expected: 5 PASS.

### Step 2.5: Commit

- [ ] Run:

```bash
git add src/nanobot_runtime/services/channels/desktop_mate.py tests/services/channels/test_desktop_mate.py
git commit -m "$(cat <<'EOF'
feat(channels): add _channel_from_session_key helper

Module-level helper that extracts the channel-name prefix from nanobot's
'<channel>:<chat_id>[:...]' session-key convention. Used by the next
commit's LazyChannelTTSSink mode-gating; tested in isolation now so the
parsing rules don't have to be inferred from gating-test assertions.

Refs: docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md §5.3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `LazyChannelTTSSink` mode-gating

Inject `ChannelModeMap` into the sink. Rewrite `is_enabled` to gate on mode (config) before falling through to the existing channel-readiness check (runtime).

**Files:**

- Modify: `src/nanobot_runtime/services/channels/desktop_mate.py` (`LazyChannelTTSSink` class)
- Modify: `tests/services/channels/test_desktop_mate.py` (add `TestLazyChannelTTSSinkIsEnabled`)

### Step 3.1: Write failing tests

- [ ] Append to `tests/services/channels/test_desktop_mate.py`. Add necessary imports at the top alongside the others:

```python
from unittest.mock import patch

from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode
```

Then add at the bottom:

```python
# ── LazyChannelTTSSink.is_enabled mode-gating ─────────────────────────


@pytest.fixture
def streaming_only_map() -> ChannelModeMap:
    """desktop_mate=streaming, telegram=attachment, default=none."""
    return ChannelModeMap(
        default=TTSMode.NONE,
        channels={
            "desktop_mate": TTSMode.STREAMING,
            "telegram": TTSMode.ATTACHMENT,
        },
    )


class TestLazyChannelTTSSinkIsEnabled:
    def test_streaming_channel_with_active_desktop_mate_returns_true(
        self, streaming_only_map: ChannelModeMap
    ):
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        with patch(
            "nanobot_runtime.services.channels.desktop_mate.get_desktop_mate_channel"
        ) as g:
            g.return_value.is_tts_enabled_for_current_stream.return_value = True
            assert sink.is_enabled("desktop_mate:chat42") is True

    def test_streaming_channel_without_desktop_mate_returns_true(
        self, streaming_only_map: ChannelModeMap
    ):
        # Race-tolerance: channel not yet constructed at sink-call time. Existing
        # behaviour preserved so the hook can do useful work; sink will silently
        # drop in send_tts_chunk if channel is still missing at delivery time.
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        with patch(
            "nanobot_runtime.services.channels.desktop_mate.get_desktop_mate_channel",
            side_effect=RuntimeError("channel not constructed"),
        ):
            assert sink.is_enabled("desktop_mate:chat42") is True

    def test_streaming_channel_with_tts_off_in_channel_returns_false(
        self, streaming_only_map: ChannelModeMap
    ):
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        with patch(
            "nanobot_runtime.services.channels.desktop_mate.get_desktop_mate_channel"
        ) as g:
            g.return_value.is_tts_enabled_for_current_stream.return_value = False
            assert sink.is_enabled("desktop_mate:chat42") is False

    def test_none_channel_returns_false(self, streaming_only_map: ChannelModeMap):
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        # No need to patch — mode gate short-circuits before readiness check.
        assert sink.is_enabled("slack:C123:T456") is False

    def test_attachment_channel_returns_false(self, streaming_only_map: ChannelModeMap):
        # This sink is streaming-only; ATTACHMENT will be picked up by a future
        # AttachmentTTSHook with its own sink (out of scope per spec §2).
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        assert sink.is_enabled("telegram:42:topic:7") is False

    def test_session_key_none_uses_default_mode(self, streaming_only_map: ChannelModeMap):
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        assert sink.is_enabled(None) is False  # default is NONE

    def test_unknown_channel_in_session_key_uses_default_mode(
        self, streaming_only_map: ChannelModeMap
    ):
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        assert sink.is_enabled("discord:guild:chan") is False  # default is NONE

    def test_no_arg_call_falls_through_to_default(self, streaming_only_map: ChannelModeMap):
        # Backward-compat: callers passing no session_key (e.g. legacy mocks)
        # get the same treatment as session_key=None.
        sink = LazyChannelTTSSink(mode_map=streaming_only_map)
        assert sink.is_enabled() is False  # default is NONE
```

### Step 3.2: Run to verify failure

- [ ] Run: `pytest tests/services/channels/test_desktop_mate.py::TestLazyChannelTTSSinkIsEnabled -v`
- [ ] Expected: TypeError — `LazyChannelTTSSink.__init__()` takes no `mode_map` kwarg yet.

### Step 3.3: Implement the gating

- [ ] In `src/nanobot_runtime/services/channels/desktop_mate.py`, add this import near the top alongside other `nanobot_runtime` imports:

```python
from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode
```

- [ ] Replace the entire `LazyChannelTTSSink` class. The class currently has these methods (per `tts.py:332`-ish onward in the original file): `is_enabled(self) -> bool`, `send_tts_chunk(self, chunk)`, and `get_reference_id_for_session(self, session_key)`. Keep `send_tts_chunk` and `get_reference_id_for_session` byte-for-byte identical (only the lookup-helper bit at the bottom of `get_reference_id_for_session` was already using `partition(":")` — leave it alone). Replace just `__init__` and `is_enabled`:

```python
class LazyChannelTTSSink:
    """Lazily resolve ``DesktopMateChannel`` + gate per channel TTS mode.

    Avoids ordering constraints between hook factory and channel
    construction: the channel is resolved per call to ``send_tts_chunk``
    rather than wired in at sink construction.

    The sink performs *two* checks in series before allowing synthesis:

    1. **Mode gate**: ``mode_map.lookup(channel) == STREAMING``.
    2. **Channel readiness**: the active DesktopMate stream has TTS on.

    If the mode gate rejects, the readiness check is skipped — there's
    nothing to deliver to anyway. See the design spec §5.3 for why
    ``streaming`` is operationally bound to DesktopMate in this PR.
    """

    def __init__(self, mode_map: ChannelModeMap) -> None:
        self._mode_map = mode_map

    def is_enabled(self, session_key: str | None = None) -> bool:
        mode = self._mode_map.lookup(_channel_from_session_key(session_key))
        if mode is not TTSMode.STREAMING:
            return False
        try:
            return get_desktop_mate_channel().is_tts_enabled_for_current_stream()
        except RuntimeError:
            return True  # No channel yet — preserves existing race-tolerance.

    # send_tts_chunk: unchanged from prior implementation.
    # get_reference_id_for_session: unchanged from prior implementation.
```

> Important: do NOT delete `send_tts_chunk` or `get_reference_id_for_session`. Only replace `__init__` and `is_enabled`. Verify after editing that the file still has both methods.

### Step 3.4: Run to verify pass

- [ ] Run: `pytest tests/services/channels/test_desktop_mate.py::TestLazyChannelTTSSinkIsEnabled -v`
- [ ] Expected: 8 PASS.

### Step 3.5: Run the full `test_desktop_mate.py` to catch regressions

- [ ] Run: `pytest tests/services/channels/test_desktop_mate.py -v`
- [ ] Expected: all green. Existing send-frame and reference-id tests untouched.

### Step 3.6: Commit

- [ ] Run:

```bash
git add src/nanobot_runtime/services/channels/desktop_mate.py tests/services/channels/test_desktop_mate.py
git commit -m "$(cat <<'EOF'
feat(channels): LazyChannelTTSSink gates is_enabled by channel mode

Sink now requires a ChannelModeMap at construction time and consults it
in is_enabled(session_key) before falling through to the existing
DesktopMate readiness check. Mode-NONE / mode-ATTACHMENT / unknown-channel
short-circuit to False; mode-STREAMING preserves the prior is_enabled
behaviour exactly.

The launcher commit will inject the map; tests cover the sink in
isolation with mocked channel state.

Refs: docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md §5.3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `TTSHook` session_key threading

Three call-site updates in `tts.py` plus a small signature update to the regression harness's `DirectSink`. Hook constructor unchanged.

**Files:**

- Modify: `src/nanobot_runtime/services/hooks/tts.py`
- Modify: `tests/services/hooks/test_tts_hook.py` (add `_GatedFakeSink` + new test functions; existing fakes/`_make_hook`/`_ctx` style preserved)
- Modify: `tests/regression/harness.py` (`DirectSink.is_enabled` signature)

### Step 4.1: Add `_GatedFakeSink` helper + failing tests

The existing file uses module-level fake classes (`_FakeChunker`, `_FakePreprocessor`, `_FakeSink`, `_FakeSynthesizer`), a `_make_hook(...)` factory returning `(hook, sink, synth)`, a `_ctx(iteration=0, session_key="test-session")` helper, and plain `async def test_*` functions (no test classes). Match that style.

- [ ] Open `tests/services/hooks/test_tts_hook.py`. Add a new fake class right after the existing `_FakeSink` definition:

```python
class _GatedFakeSink:
    """Like _FakeSink but exposes a configurable is_enabled gate that
    records every call's session_key. Used to verify the hook threads
    AgentHookContext.session_key into both the first-pass dispatch check
    and the second-chance check inside _synth_and_emit.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.chunks: list[TTSChunk] = []
        self.is_enabled_calls: list[str | None] = []
        self._enabled = enabled

    def is_enabled(self, session_key: str | None = None) -> bool:
        self.is_enabled_calls.append(session_key)
        return self._enabled

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        self.chunks.append(chunk)
```

- [ ] At the bottom of the file (after the existing tests), add three new test functions matching the existing `async def test_*` style:

```python
# ── session_key plumbing into sink.is_enabled ─────────────────────────


async def test_dispatch_sentence_passes_session_key_to_is_enabled() -> None:
    """Hook must thread AgentHookContext.session_key into sink.is_enabled
    so mode-gating sinks (LazyChannelTTSSink) can decide per channel.
    Both the first-pass check in _dispatch_sentence and the second-chance
    check inside _synth_and_emit must receive the same key.
    """
    sink = _GatedFakeSink(enabled=True)
    synth = _FakeSynthesizer()
    hook = TTSHook(
        chunker_factory=_FakeChunker,
        preprocessor=_FakePreprocessor(),
        emotion_mapper=_FakeEmotionMapper(),
        synthesizer=synth,
        sink=sink,
    )
    ctx = _ctx(session_key="slack:C123:T456")

    await hook.on_stream(ctx, "Hello there.")
    await hook.on_stream_end(ctx, resuming=False)

    # First-pass + second-chance: both must use the same session_key.
    assert sink.is_enabled_calls == ["slack:C123:T456", "slack:C123:T456"]


async def test_disabled_sink_skips_dispatch_no_synth_no_chunk_no_sequence_bump() -> None:
    """When the sink reports disabled at dispatch time, the hook must
    skip synthesis entirely: no synth call, no emitted chunk, and the
    per-session sequence counter must not advance (so the next enabled
    sentence still gets sequence 0).
    """
    sink = _GatedFakeSink(enabled=False)
    synth = _FakeSynthesizer()
    hook = TTSHook(
        chunker_factory=_FakeChunker,
        preprocessor=_FakePreprocessor(),
        emotion_mapper=_FakeEmotionMapper(),
        synthesizer=synth,
        sink=sink,
    )
    ctx = _ctx(session_key="slack:C123:T456")

    await hook.on_stream(ctx, "Hello there. Second sentence.")
    await hook.on_stream_end(ctx, resuming=False)

    assert synth.calls == []
    assert sink.chunks == []
    # Two sentences attempted; one is_enabled call per dispatch attempt.
    # No second-chance call ever fires because no task was created.
    assert sink.is_enabled_calls == ["slack:C123:T456", "slack:C123:T456"]


async def test_sink_without_is_enabled_method_is_treated_as_always_enabled() -> None:
    """Backward compat: bare sinks (existing _FakeSink with no is_enabled
    method) must still receive chunks. Many existing tests in this file
    rely on this; the new gating must not break them.
    """
    sink = _FakeSink()  # existing fake — has no is_enabled method
    synth = _FakeSynthesizer()
    hook = TTSHook(
        chunker_factory=_FakeChunker,
        preprocessor=_FakePreprocessor(),
        emotion_mapper=_FakeEmotionMapper(),
        synthesizer=synth,
        sink=sink,
    )
    ctx = _ctx()

    await hook.on_stream(ctx, "Hello there.")
    await hook.on_stream_end(ctx, resuming=False)

    assert synth.calls == ["Hello there."]
    assert len(sink.chunks) == 1
```

### Step 4.2: Run to verify failure

- [ ] Run: `pytest tests/services/hooks/test_tts_hook.py::test_dispatch_sentence_passes_session_key_to_is_enabled -v`
- [ ] Expected: AssertionError. Current code calls `fn()` (no args) inside `_sink_is_enabled`, so `is_enabled_calls` would be `[None, None]`, not the session key.

### Step 4.3: Implement the threading

- [ ] In `src/nanobot_runtime/services/hooks/tts.py`, update the `TTSSink` Protocol comment-spec (line ~99–100):

```python
class TTSSink(Protocol):
    async def send_tts_chunk(self, chunk: TTSChunk) -> None: ...

    # Optional. When present and returning False, the hook skips synthesis
    # entirely for this sentence — saving GPU / network traffic for clients
    # that can't play audio. Sinks that don't implement this method are
    # treated as "always enabled" for backward compatibility.
    #
    # The optional ``session_key`` argument lets sinks gate per-channel
    # (see LazyChannelTTSSink): the hook always passes the active session's
    # key, but the keyword has a None default so existing call sites and
    # legacy mocks keep working.
    # def is_enabled(self, session_key: str | None = None) -> bool: ...
```

- [ ] Update `_sink_is_enabled` (line ~232):

```python
def _sink_is_enabled(self, session_key: str | None = None) -> bool:
    fn = getattr(self._sink, "is_enabled", None)
    return not callable(fn) or fn(session_key)
```

- [ ] Update `_dispatch_sentence` (line ~236) — change the `_sink_is_enabled()` call to pass `state.session_key`, and pass `session_key=state.session_key` into the `_synth_and_emit` task:

```python
def _dispatch_sentence(self, state: _SessionState, sentence: str) -> None:
    text, emotion = self._preprocessor.process(sentence)
    if not text or not any(ch.isalnum() for ch in text):
        return
    if not self._sink_is_enabled(state.session_key):
        return
    reference_id = self._resolve_reference_id(state.session_key)
    sequence = state.sequence
    state.sequence += 1
    task = asyncio.create_task(
        self._synth_and_emit(
            text,
            emotion,
            sequence,
            session_key=state.session_key,
            reference_id=reference_id,
        )
    )
    state.pending.append(task)
```

- [ ] Update `_synth_and_emit` (line ~255) — add `session_key` kwarg and use it in the second-chance check:

```python
async def _synth_and_emit(
    self,
    text: str,
    emotion: str | None,
    sequence: int,
    *,
    session_key: str | None,
    reference_id: str | None = None,
) -> None:
    # Second-chance check: sink's enabled state can change between task
    # scheduled and task running (e.g. channel registers the stream as
    # off after dispatch). Re-checking with the same session_key avoids
    # a wasted synthesize() call.
    if not self._sink_is_enabled(session_key):
        return
    try:
        audio_b64 = await self._synthesizer.synthesize(text, reference_id=reference_id)
    except Exception:
        logger.exception("TTS synth failed (seq={})", sequence)
        audio_b64 = None
    keyframes = self._emotion_mapper.map(emotion)
    chunk = TTSChunk(
        sequence=sequence,
        text=text,
        audio_base64=audio_b64,
        emotion=emotion,
        keyframes=keyframes,
    )
    try:
        await self._sink.send_tts_chunk(chunk)
    except Exception:
        logger.exception("TTS sink emission failed (seq={})", sequence)
```

### Step 4.4: Run new tests to verify pass

- [ ] Run: `pytest tests/services/hooks/test_tts_hook.py -k "session_key or sink_without_is_enabled or disabled_sink" -v`
- [ ] Expected: 3 PASS — `test_dispatch_sentence_passes_session_key_to_is_enabled`, `test_disabled_sink_skips_dispatch_no_synth_no_chunk_no_sequence_bump`, `test_sink_without_is_enabled_method_is_treated_as_always_enabled`.

### Step 4.5: Run full hook tests for regressions

- [ ] Run: `pytest tests/services/hooks/test_tts_hook.py -v`
- [ ] Expected: all green. The pre-existing tests use the bare `_FakeSink` (no `is_enabled` method) and so exercise the backward-compat path automatically. Nothing else in this file should regress.

### Step 4.6: Update `DirectSink` in the regression harness

The regression harness in `tests/regression/harness.py` defines its own sink and instantiates `TTSHook` directly, so it has a `is_enabled` call site outside `tests/services/hooks/`. The new TTSHook passes `session_key` positionally; without this update, regression tests (which run as part of Step 8.2 `pytest tests/ --ignore=tests/e2e`) raise `TypeError: is_enabled() takes 1 positional argument but 2 were given`.

- [ ] Open `tests/regression/harness.py`. Find the `DirectSink` class (around line 88) and update only the `is_enabled` signature — body unchanged:

```python
class DirectSink:
    """Sink that forwards to a specific channel (not via module registry).

    Regression tests instantiate one channel per scenario and want to
    avoid cross-test state leakage from the module-level _LATEST_CHANNEL.
    The channel is also an ``is_enabled``-aware TTSSink so TTSHook's skip
    path is exercised.
    """

    def __init__(self, channel: DesktopMateChannel):
        self._channel = channel

    def is_enabled(self, session_key: str | None = None) -> bool:
        # Channel readiness doesn't depend on the key; the key is part of
        # the new TTSHook → TTSSink protocol but the channel only needs
        # to know whether its own current stream is enabled.
        return self._channel.is_tts_enabled_for_current_stream()

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        await self._channel.send_tts_chunk(chunk)
```

### Step 4.7: Run regression harness tests

- [ ] Run: `pytest tests/regression/ -v`
- [ ] Expected: all green. (No assertion changes in regression tests — only the sink signature, which they don't directly assert on.)

### Step 4.8: Commit

- [ ] Run:

```bash
git add src/nanobot_runtime/services/hooks/tts.py tests/services/hooks/test_tts_hook.py tests/regression/harness.py
git commit -m "$(cat <<'EOF'
feat(tts): TTSHook threads session_key into sink.is_enabled

Three call-site updates in tts.py: _sink_is_enabled accepts session_key,
_dispatch_sentence passes state.session_key, _synth_and_emit gains a
session_key kwarg for the second-chance check. Hook constructor and
existing per-session state lifecycle are unchanged.

Backward compat preserved: sinks that omit is_enabled entirely are still
treated as always-enabled. Sinks that implement is_enabled with the new
session_key kwarg (default None) work without further changes.

regression/harness.py DirectSink updated to match the new signature so
the regression suite keeps passing — it was the only is_enabled call
site outside the hook tests.

Refs: docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md §5.2, §9

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Launcher integration + TTS env var rename

Wire the modes loader and the new sink. Rename all TTS env vars in launcher + `test_launcher.py`. Add `_resolve_tts_modes_path` and the FileNotFoundError guard.

**Files:**

- Modify: `src/nanobot_runtime/launcher.py`
- Modify: `tests/test_launcher.py`

### Step 5.1: Write failing tests for modes path resolver + missing-file guard

- [ ] In `tests/test_launcher.py`, find the existing `_clear_yuri_env` autouse fixture (around line 19-24). Add `TTS_` to the prefix tuple:

```python
@pytest.fixture(autouse=True)
def _clear_yuri_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe inherited YURI_* and TTS_* env vars so each test starts from defaults."""
    for k in list(os.environ):
        if k.startswith("YURI_") or k.startswith("TTS_"):
            monkeypatch.delenv(k, raising=False)
```

- [ ] Find the existing `test_tts_rules_path_env_override_wins` test and rename the env var inside it:

```python
def test_tts_rules_path_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_RULES_PATH", "/tmp/custom_rules.yml")
    assert _resolve_tts_rules_path() == "/tmp/custom_rules.yml"
```

- [ ] Append new test classes at the bottom of `tests/test_launcher.py`:

```python
# ── _resolve_tts_modes_path ────────────────────────────────────────────


class TestResolveTtsModesPath:
    def test_modes_path_defaults_to_cwd_resources_modes_yml(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        from nanobot_runtime.launcher import _resolve_tts_modes_path
        expected = str(tmp_path / "resources" / "tts_channel_modes.yml")
        assert _resolve_tts_modes_path() == expected

    def test_modes_path_env_override_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TTS_MODES_PATH", "/tmp/custom_modes.yml")
        from nanobot_runtime.launcher import _resolve_tts_modes_path
        assert _resolve_tts_modes_path() == "/tmp/custom_modes.yml"


# ── _build_tts_hook fail-loud guards ───────────────────────────────────


class TestBuildTtsHookFailsWhenModesMissing:
    def test_raises_file_not_found_with_actionable_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # Lay down a valid rules file so we get past the first guard.
        rules = tmp_path / "tts_rules.yml"
        rules.write_text("rules: []\n")  # adjust if EmotionMapper requires more
        monkeypatch.setenv("TTS_RULES_PATH", str(rules))
        # Don't create the modes file → expect FileNotFoundError.
        monkeypatch.setenv("TTS_MODES_PATH", str(tmp_path / "missing.yml"))

        from nanobot_runtime.launcher import _build_tts_hook
        with pytest.raises(FileNotFoundError) as ei:
            _build_tts_hook()
        msg = str(ei.value)
        assert "TTS_MODES_PATH" in msg
        assert "TTS_ENABLED=0" in msg

    def test_does_not_check_modes_when_tts_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # When TTS_ENABLED=0, _hooks_factory must skip _build_tts_hook
        # entirely — no rules check, no modes check.
        #
        # We isolate the TTS branch by stubbing the only other branches
        # _hooks_factory exercises (LTM hook construction and the idle
        # cron install) so a failure in this test can ONLY mean the TTS
        # branch was incorrectly entered. No bare-except — every failure
        # mode is named.
        monkeypatch.setattr(
            "nanobot_runtime.launcher.build_ltm_hooks",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            "nanobot_runtime.launcher.install_idle_system_job",
            lambda **k: None,  # never actually installed (we set IDLE off)
        )
        monkeypatch.setenv("TTS_ENABLED", "0")
        monkeypatch.setenv("YURI_IDLE_ENABLED", "0")
        monkeypatch.chdir(tmp_path)  # no resources/ dir at all

        from unittest.mock import MagicMock
        from nanobot_runtime.launcher import _hooks_factory
        loop = MagicMock()
        loop.cron_service = None  # not consulted because IDLE is off

        hooks = _hooks_factory(loop)

        # LTM stubbed → []. TTS off → no append. Idle off → no install.
        # Result must be exactly the LTM stub's output: empty list.
        assert hooks == []
```

### Step 5.2: Run to verify failure

- [ ] Run: `pytest tests/test_launcher.py::TestResolveTtsModesPath tests/test_launcher.py::TestBuildTtsHookFailsWhenModesMissing -v`
- [ ] Expected: ImportError on `_resolve_tts_modes_path` or AttributeError.

### Step 5.3: Implement the launcher changes

- [ ] In `src/nanobot_runtime/launcher.py`, add this import alongside the existing `nanobot_runtime` imports:

```python
from nanobot_runtime.services.tts.modes import load_channel_modes
```

- [ ] Add the modes-path resolver right after `_resolve_tts_rules_path`:

```python
def _resolve_tts_modes_path() -> str:
    """Resolve the TTS channel-mode YAML path at call time, not import time.

    Defaults to ``<cwd>/resources/tts_channel_modes.yml``. Same lazy-resolve
    pattern as ``_resolve_tts_rules_path`` so test harnesses that ``chdir``
    after import still get the right path.
    """
    return os.getenv(
        "TTS_MODES_PATH",
        os.path.join(os.getcwd(), "resources", "tts_channel_modes.yml"),
    )
```

- [ ] Modify `_build_tts_hook` to load the modes file and inject the map. The function currently looks like (paraphrased — preserve everything else exactly):

```python
def _build_tts_hook() -> TTSHook:
    rules_path = _resolve_tts_rules_path()
    if not os.path.exists(rules_path):
        raise FileNotFoundError(...)  # existing
    emotion_mapper = EmotionMapper.from_yaml(rules_path)
    synthesizer = IrodoriClient(
        base_url=os.getenv("YURI_TTS_URL", "http://192.168.0.41:8091"),
        reference_id=os.getenv("YURI_TTS_REF_AUDIO"),
    )
    return TTSHook(
        chunker_factory=SentenceChunker,
        preprocessor=Preprocessor(known_emojis=emotion_mapper.known_emojis),
        emotion_mapper=emotion_mapper,
        synthesizer=synthesizer,
        sink=LazyChannelTTSSink(),
        barrier_timeout_seconds=float(os.getenv("YURI_TTS_BARRIER_TIMEOUT", "30")),
    )
```

Replace it with:

```python
def _build_tts_hook() -> TTSHook:
    """Assemble the TTS pipeline wired to DesktopMateChannel.

    The sink uses :class:`LazyChannelTTSSink` so the hook is independent of
    channel start-up order — the channel is resolved per-chunk at send
    time. If TTS is fully disabled for a workspace, set ``TTS_ENABLED=0``
    and the factory returns no TTS hook.

    A missing TTS rules YAML or modes YAML is treated as a misconfiguration,
    not a degraded mode — the operator gets a loud, actionable failure at
    startup.
    """
    rules_path = _resolve_tts_rules_path()
    if not os.path.exists(rules_path):
        raise FileNotFoundError(
            f"TTS rules YAML not found at {rules_path!r}. Set "
            "TTS_RULES_PATH or place the file at "
            "<workspace>/resources/tts_rules.yml. To run without TTS set "
            "TTS_ENABLED=0."
        )
    modes_path = _resolve_tts_modes_path()
    if not os.path.exists(modes_path):
        raise FileNotFoundError(
            f"TTS channel modes YAML not found at {modes_path!r}. Set "
            "TTS_MODES_PATH or place the file at "
            "<workspace>/resources/tts_channel_modes.yml. To run without TTS set "
            "TTS_ENABLED=0."
        )
    mode_map = load_channel_modes(modes_path)
    emotion_mapper = EmotionMapper.from_yaml(rules_path)
    synthesizer = IrodoriClient(
        base_url=os.getenv("TTS_URL", "http://192.168.0.41:8091"),
        reference_id=os.getenv("TTS_REF_AUDIO"),
    )
    return TTSHook(
        chunker_factory=SentenceChunker,
        preprocessor=Preprocessor(known_emojis=emotion_mapper.known_emojis),
        emotion_mapper=emotion_mapper,
        synthesizer=synthesizer,
        sink=LazyChannelTTSSink(mode_map=mode_map),
        barrier_timeout_seconds=float(os.getenv("TTS_BARRIER_TIMEOUT", "30")),
    )
```

- [ ] Update the `TTS_RULES_PATH` reference inside `_resolve_tts_rules_path`:

```python
def _resolve_tts_rules_path() -> str:
    """Resolve the TTS rules YAML path at call time, not import time.

    Defaults to ``<cwd>/resources/tts_rules.yml`` so the launcher — which
    lives inside the package, not next to the workspace — picks up the
    workspace's own rules. Resolved per call so test harnesses that
    `chdir` after import still get the right path.
    """
    return os.getenv(
        "TTS_RULES_PATH",
        os.path.join(os.getcwd(), "resources", "tts_rules.yml"),
    )
```

- [ ] Update `_hooks_factory` to use `TTS_ENABLED`:

```python
if os.getenv("TTS_ENABLED", "1") != "0":
    hooks.append(_build_tts_hook())
```

### Step 5.4: Run to verify pass

- [ ] Run: `pytest tests/test_launcher.py -v`
- [ ] Expected: all green. The existing `_build_idle_config` tests still pass (they use `YURI_IDLE_*` which is out of scope). The renamed rules-path test passes against `TTS_RULES_PATH`. The new modes tests pass.

### Step 5.5: Commit

- [ ] Run:

```bash
git add src/nanobot_runtime/launcher.py tests/test_launcher.py
git commit -m "$(cat <<'EOF'
feat(launcher): wire ChannelModeMap into TTSHook + drop YURI_ from TTS env vars

_build_tts_hook now loads tts_channel_modes.yml at boot and injects the
ChannelModeMap into LazyChannelTTSSink. Missing modes file with
TTS_ENABLED=1 fails loud (FileNotFoundError naming TTS_MODES_PATH and
TTS_ENABLED=0 escape).

TTS env vars renamed (YURI_TTS_* → TTS_*) per spec §8 since
nanobot_runtime is workspace-neutral. LTM/IDLE vars keep YURI_ prefix
(out of scope per spec §2).

Refs: docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md §5.4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: E2E conftest + docs rename

Mechanical find-replace in three files. No new tests, but the e2e suite must still pass after the rename.

**Files:**

- Modify: `tests/e2e/conftest.py` (line 139)
- Modify: `tests/e2e/README.md` (line 20)
- Modify: `docs/setup.md` (lines 34, 35, 175, 176)

### Step 6.1: Update e2e conftest

- [ ] In `tests/e2e/conftest.py`, change line 139 from:

```python
    tts_url = os.getenv("YURI_TTS_URL", "http://192.168.0.41:8091")
```

to:

```python
    tts_url = os.getenv("TTS_URL", "http://192.168.0.41:8091")
```

### Step 6.2: Update e2e README reference

- [ ] In `tests/e2e/README.md`, locate line ~20 (description of the TTS URL env var) and change `YURI_TTS_URL` → `TTS_URL`. Other lines in this file reference `YURI_WORKSPACE` and `YURI_IDLE_*` — leave those alone (out of scope per spec §2).

### Step 6.3: Update docs/setup.md

- [ ] In `docs/setup.md`:
  - Line 34: `YURI_TTS_ENABLED=0` → `TTS_ENABLED=0`
  - Line 35 (and any text in the same block): leave `YURI_LTM_URL=` references as-is; only change `YURI_TTS_*` → `TTS_*`
  - Line 175: change `YURI_TTS_ENABLED` → `TTS_ENABLED`
  - Line 176: change `YURI_TTS_URL` → `TTS_URL`; leave `YURI_LTM_URL` and `YURI_IDLE_ENABLED` as-is

Use grep to confirm no leftover `YURI_TTS_` in this file:

```bash
grep -n 'YURI_TTS_' docs/setup.md
```

Expected: no output.

### Step 6.4: Run e2e suite to verify nothing regressed

- [ ] Run: `bash scripts/e2e.sh`
- [ ] Expected: all e2e scenarios pass. (Note: e2e requires the workspace `.env` and `tts_channel_modes.yml` from Task 7 — if you run this step before Task 7, e2e will fail at the gateway boot. Either run Task 7 first, or skip Step 6.4 until after Task 7.)

> If running tasks sequentially: defer Step 6.4 to Task 8 verification. The commit can still go in here.

### Step 6.5: Commit

- [ ] Run:

```bash
git add tests/e2e/conftest.py tests/e2e/README.md docs/setup.md
git commit -m "$(cat <<'EOF'
chore: rename YURI_TTS_* → TTS_* in e2e harness and docs

E2E conftest probes the same TTS_URL the launcher reads, so they must
migrate together — mismatched names would cause health-check to probe a
different endpoint than the gateway uses.

Operator-facing docs in docs/setup.md updated to match. Out-of-scope
references (YURI_LTM_*, YURI_IDLE_*, YURI_WORKSPACE) untouched.

Refs: docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md §9

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Workspace setup (yuri/)

Workspace-side files outside this repo. The launcher won't boot without these post-rename.

**Files:**

- Modify: `../yuri/.env` (rename TTS env vars)
- Create: `../yuri/resources/tts_channel_modes.yml`

### Step 7.1: Update workspace `.env`

- [ ] Open `../yuri/.env`. For each line starting with `YURI_TTS_`, drop the `YURI_` prefix:

```
YURI_TTS_ENABLED=...        →  TTS_ENABLED=...
YURI_TTS_URL=...            →  TTS_URL=...
YURI_TTS_REF_AUDIO=...      →  TTS_REF_AUDIO=...
YURI_TTS_BARRIER_TIMEOUT=... →  TTS_BARRIER_TIMEOUT=...
YURI_TTS_RULES_PATH=...     →  TTS_RULES_PATH=...
```

(Add a `TTS_MODES_PATH=...` line only if you want to override the default `<cwd>/resources/tts_channel_modes.yml`.)

`YURI_LTM_*`, `YURI_IDLE_*`, `YURI_NANOBOT_CONFIG`, `YURI_WORKSPACE` — leave alone.

### Step 7.2: Create the channel-modes YAML

- [ ] Create `../yuri/resources/tts_channel_modes.yml`:

```yaml
# Per-channel TTS dispatch mode.
# - streaming:  per-sentence chunks to a live sink (DesktopMate, Unity)
# - attachment: full-turn audio as voice-note (Telegram, WhatsApp, Matrix) — DECLARED, NOT YET IMPLEMENTED
# - none:       text-only response, no synthesis (Slack, Discord, ...)
default: none
channels:
  desktop_mate: streaming
  telegram: attachment   # mode declared; ATTACHMENT pipeline TBD
  slack: none
```

### Step 7.3: No git commit for workspace files

- [ ] These files live in `../yuri`, which is a separate repo (and per `.gitignore` is largely untracked). Do NOT add them to the `nanobot_runtime` git index. Just save them to disk.

> If `../yuri/.env` is tracked in the workspace's own git, commit there separately with: `cd ../yuri && git add .env resources/tts_channel_modes.yml && git commit -m "chore: align with TTS_* env-var rename"`. Otherwise leave them as untracked workspace state.

---

## Task 8: Final verification

Run every check from the spec's verification criteria (§11). This task produces no commits unless a check fails and a fix is needed.

### Step 8.1: Unit test sweep

- [ ] Run: `pytest tests/services/tts/test_modes.py tests/services/channels/test_desktop_mate.py tests/services/hooks/test_tts_hook.py tests/test_launcher.py -v`
- [ ] Expected: all green.

### Step 8.2: Full unit test run

- [ ] Run: `pytest tests/ --ignore=tests/e2e -v`
- [ ] Expected: all green.

### Step 8.3: E2E sweep

- [ ] Run: `bash scripts/e2e.sh`
- [ ] Expected: all green. Existing DesktopMate scenarios pass against the new `mode_map={desktop_mate: streaming, default: none}`.

### Step 8.4: FileNotFoundError boot verification

- [ ] Backup the modes file: `mv ../yuri/resources/tts_channel_modes.yml /tmp/tts_modes_bak.yml`
- [ ] Run the launcher: `cd ../yuri && TTS_ENABLED=1 python -m nanobot_runtime.launcher` (or however the workspace boots)
- [ ] Expected: process exits with the FileNotFoundError text from spec §5.4 — message contains `TTS_MODES_PATH`, the path it looked at, and `TTS_ENABLED=0` as the escape.
- [ ] Restore: `mv /tmp/tts_modes_bak.yml ../yuri/resources/tts_channel_modes.yml`

### Step 8.5: TTS-disabled boot verification

- [ ] Move the modes file again: `mv ../yuri/resources/tts_channel_modes.yml /tmp/tts_modes_bak.yml`
- [ ] Run with TTS off: `cd ../yuri && TTS_ENABLED=0 python -m nanobot_runtime.launcher` (interrupt after boot completes — we're verifying it boots, not that it runs forever)
- [ ] Expected: launcher boots normally; no modes-file check.
- [ ] Restore: `mv /tmp/tts_modes_bak.yml ../yuri/resources/tts_channel_modes.yml`

### Step 8.6: Manual gate isolation (per spec §11.8)

- [ ] (a) Default config — connect DesktopMate FE, send a message → observe TTS frames in the WS log.
- [ ] (b) Edit `../yuri/resources/tts_channel_modes.yml`: change `desktop_mate: streaming` → `desktop_mate: none`. Restart the launcher. Repeat the message → observe NO TTS frames.
- [ ] (c) Revert the YAML.
- [ ] DO NOT test by setting `slack: streaming` (per spec §11.8 caveat).

### Step 8.7: Grep verification (per spec §11.9)

- [ ] Run from the repo root:

```bash
git ls-files | xargs grep -l 'YURI_TTS_' | grep -v 'docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md'
```

- [ ] Expected: no output. (The spec is the only allowed reference, in §5.4 / §9 documenting the migration.)

### Step 8.8: Push the branch

- [ ] Run: `git push -u origin feat/tts-channel-gating`

### Step 8.9: Open the PR

- [ ] Run:

```bash
gh pr create --title "feat: TTS channel-mode gating" --body "$(cat <<'EOF'
## Summary

- Adds per-channel `TTSMode` declared in `resources/tts_channel_modes.yml`.
- `LazyChannelTTSSink.is_enabled(session_key)` short-circuits when the channel's mode isn't `STREAMING`, stopping audio leaks into DesktopMate from Slack/text-only turns.
- `ATTACHMENT` mode is reserved in the data model for future Telegram/WhatsApp voice-note support — not implemented this PR.
- TTS env vars renamed `YURI_TTS_*` → `TTS_*` (workspace-neutral runtime). LTM/IDLE keep `YURI_` prefix; out of scope.

Full design: `docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md`
Implementation plan: `docs/superpowers/plans/2026-04-26-tts-channel-gating.md`

## Test plan

- [x] Unit: `tests/services/tts/test_modes.py` (new, 17 tests)
- [x] Unit: `tests/services/channels/test_desktop_mate.py` (additions: `Test_ChannelFromSessionKey`, `TestLazyChannelTTSSinkIsEnabled`)
- [x] Unit: `tests/services/hooks/test_tts_hook.py` (additions: `TestTTSHookSessionKeyPlumbing`)
- [x] Unit: `tests/test_launcher.py` (rename + `TestResolveTtsModesPath`, `TestBuildTtsHookFailsWhenModesMissing`)
- [x] E2E: existing scenarios pass against new `mode_map`
- [x] Manual: gate isolation via `desktop_mate: none` toggle
- [x] Manual: FileNotFoundError on missing modes file with `TTS_ENABLED=1`
- [x] Manual: clean boot with `TTS_ENABLED=0` and no modes file
- [x] grep: no `YURI_TTS_` references outside the spec

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] Return the PR URL.

---

## Self-Review

Performed inline against `docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md`. Spec section → plan task mapping:

| Spec § | Plan task | Notes |
|---|---|---|
| §3 Assumptions A1–A5 | Task 2 + Task 3 (both rely on session_key prefix convention) | Verified; assumptions encoded into helper + sink behaviour |
| §4 Design overview | Tasks 1–5 collectively | |
| §5.1 modes.py | Task 1 | All three pieces (enum, map, loader) covered by 17 unit tests |
| §5.2 tts.py | Task 4 | Three call-site updates + new test class |
| §5.3 desktop_mate.py | Task 2 (helper) + Task 3 (sink) | Operational note encoded into class docstring + tests for ATTACHMENT and unknown-channel cases |
| §5.4 launcher.py | Task 5 | All env var renames + modes loader wired |
| §5.5 tts_channel_modes.yml | Task 7 | Workspace-side, not committed to runtime repo |
| §6 Data flow | Indirectly: Tasks 1–5 implement; Step 8.6 manually verifies | |
| §7 Error handling — Boot | Task 1 (loader ValueErrors) + Task 5 (FileNotFoundError) | |
| §7 Error handling — Runtime | Task 3 (mode-gate paths) + Task 4 (no-`is_enabled` fallback) | |
| §8 Configuration | Tasks 5, 7 | All env vars renamed; YAML schema in §5.5 covered by Task 7 |
| §9 Migration — `.env` | Task 7 Step 7.1 | |
| §9 Migration — new YAML | Task 7 Step 7.2 | |
| §9 Migration — repo file updates | Task 6 (4 renames) + Task 4 Step 4.6 (`harness.py` signature) | All five files in the table covered. Harness lands in Task 4 with the hook change so the regression suite never observes a signature mismatch. |
| §10 Testing plan | Tasks 1–5 (each task includes its TDD tests) | |
| §11 Verification criteria | Task 8 (all 9 items mapped to Steps 8.1–8.7) | Steps 8.8/8.9 add push + PR |
| §12 Out of scope | Honoured: no AttachmentTTSHook, no LTM/IDLE rename, no multi-sink routing, no hot reload | |

**Placeholder scan:** No "TBD"/"TODO"/"implement later". Task 4 was rewritten in v2 to use the test file's actual style (`_FakeChunker`/`_FakePreprocessor`/`_FakeSink`/`_FakeSynthesizer`/`_ctx` module-level fakes, plain `async def test_*` functions) plus a new `_GatedFakeSink` helper — no fictional `tts_hook_factory` fixture or `MagicMock`/`AsyncMock` style.

**Type / signature consistency:** `ChannelModeMap.lookup(channel_name: str | None) -> TTSMode` — used identically in Task 1 (impl + tests), Task 3 (sink), Task 4 (test isolation). `_channel_from_session_key(session_key: str | None) -> str | None` — same signature in Task 2 helper, Task 3 sink consumer, Task 5 (no consumer; only used via sink). `is_enabled(session_key: str | None = None) -> bool` — same signature in Task 3 sink, Task 4 hook caller, Task 4 test mocks.

**Scope:** Single PR. Ten files modified, two created, ~270 LOC implementation + ~150 LOC tests. Fits one review cycle.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-tts-channel-gating.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
