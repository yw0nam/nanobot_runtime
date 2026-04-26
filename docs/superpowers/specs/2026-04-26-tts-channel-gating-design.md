# TTS Channel Gating — Design

**Date**: 2026-04-26
**Status**: Design (pre-implementation)
**Scope**: `nanobot_runtime` only — no nanobot upstream changes.

---

## 1. Goal

Stop `TTSHook` from synthesizing audio for inbound turns on channels that
cannot meaningfully play it. Today the hook fires on every turn and pushes
audio to DesktopMate regardless of which channel originated the request,
so a user chatting via Slack hears their reply spoken into the empty desk
where DesktopMate runs.

The fix declares per-channel TTS dispatch *modes* in a workspace YAML and
gates synthesis at the sink: only `streaming` channels invoke the live TTS
pipeline. `attachment` (voice-note) and `none` modes are also defined so
the data model is forward-compatible, but only `streaming` and `none` are
wired in this PR.

## 2. Non-goals

- **No `AttachmentTTSHook` implementation.** The `attachment` mode value
  exists in the enum and the loader accepts it, but no hook synthesizes
  voice-note files yet. A channel mapped to `attachment` today behaves
  identically to `none`.
- **No nanobot upstream changes.** No PR to add `channel` to
  `AgentHookContext`. We extract the channel name from `session_key`'s
  existing `<channel>:<...>` convention, which is already an established
  pattern in this codebase (see `LazyChannelTTSSink.get_reference_id_for_session`).
- **No hot reload.** Changes to `tts_channel_modes.yml` require a process
  restart — same policy as `tts_rules.yml`.
- **No rename of non-TTS env vars.** `LTM_*`, `IDLE_*`, `NANOBOT_CONFIG`,
  `WORKSPACE` keep their `YURI_` prefix in this PR. A separate cleanup PR
  may follow.

## 3. Assumptions (verified)

| # | Assumption | How verified |
|---|------------|--------------|
| A1 | nanobot `session_key` follows `<channel_name>:<...>` form | `nanobot/channels/slack.py:345` (`f"slack:{chat_id}:{thread_ts}"`); `nanobot/channels/telegram.py:792` (`f"telegram:{message.chat_id}:topic:{message_thread_id}"`); `LazyChannelTTSSink.get_reference_id_for_session` docstring confirms `"<channel>:<chat_id>"` is nanobot's convention |
| A2 | `session_key` may be `None` for some inbound paths | `slack.py:345` returns `None` for DMs and non-thread channel messages; `telegram.py:792` returns `None` when `message_thread_id is None`. We must treat `None` as "channel unknown → use `default` mode". |
| A3 | `TTSHook._sink_is_enabled()` is the only call site of `sink.is_enabled()` | grep across `nanobot_runtime` shows `is_enabled` is a sink-Protocol method only consumed by the hook |
| A4 | Pydantic + `yaml.safe_load` is the project standard for structured config | `.claude/rules/CODING_RULES.md` §6: *"Runtime config loaded from YAML files via `yaml.safe_load()`"*; existing `EmotionMapper.from_yaml()` follows this pattern |
| A5 | `AgentHookContext.session_key` is set per turn by nanobot's loop | `services/hooks/tts.py` lines 19-31 documents this contract; `_SessionState.session_key` already caches it for use in `get_reference_id_for_session` |

## 4. Design overview

```
launcher.py
   ├─ _resolve_tts_modes_path() → cwd/resources/tts_channel_modes.yml (or TTS_MODES_PATH override)
   └─ load_channel_modes(path) → ChannelModeMap (frozen Pydantic)
        └─ injected into LazyChannelTTSSink(mode_map=...)
             └─ TTSHook(sink=...)              # hook-side change is one line
                  └─ _sink_is_enabled(state.session_key)
                       └─ sink.is_enabled(session_key)
                            ├─ extract channel from session_key prefix
                            ├─ mode_map.lookup(channel) → TTSMode
                            └─ True iff mode == STREAMING and channel ready
```

Boundary of change is `nanobot_runtime`. Nothing in nanobot is touched.

## 5. Components

### 5.1 `services/tts/modes.py` (new)

```python
class TTSMode(str, Enum):
    STREAMING = "streaming"      # per-sentence chunks to a live sink
    ATTACHMENT = "attachment"    # full-turn audio as voice-note (NOT YET IMPLEMENTED)
    NONE = "none"                # text-only, no synthesis


class ChannelModeMap(BaseModel):
    model_config = ConfigDict(frozen=True)

    default: TTSMode = Field(default=TTSMode.NONE, description="Mode for channels not listed explicitly.")
    channels: dict[str, TTSMode] = Field(default_factory=dict, description="Explicit channel → mode mapping.")

    def lookup(self, channel_name: str | None) -> TTSMode:
        """None or unknown channel → default; known channel → mapped mode."""


def load_channel_modes(path: str | Path) -> ChannelModeMap:
    """yaml.safe_load(path).

    - File missing → FileNotFoundError (caller decides whether to fail or skip).
    - Empty file → ChannelModeMap() (all-NONE, via Pydantic field defaults).
    - Missing `default:` key → field default applies (NONE).
    - Missing `channels:` key → field default applies (empty dict).
    - Invalid mode string → ValueError naming the offending field and value.

    The loader does not supply defaults itself — Pydantic field defaults handle
    absence. The loader's job is to convert YAML scalars to TTSMode enum values
    and to raise ValueError on bad enum strings before construction.
    """
```

### 5.2 `services/hooks/tts.py` (modified — minimal)

Two signature changes, three call-site updates. Hook constructor unchanged.

```python
class TTSSink(Protocol):
    async def send_tts_chunk(self, chunk: TTSChunk) -> None: ...
    # Optional. Now takes session_key so sinks can gate per channel.
    # def is_enabled(self, session_key: str | None = None) -> bool: ...


def _sink_is_enabled(self, session_key: str | None = None) -> bool:
    fn = getattr(self._sink, "is_enabled", None)
    return not callable(fn) or fn(session_key)


def _dispatch_sentence(self, state: _SessionState, sentence: str) -> None:
    ...
    if not self._sink_is_enabled(state.session_key):   # was: ()
        return
    ...
    task = asyncio.create_task(
        self._synth_and_emit(text, emotion, sequence,
                             session_key=state.session_key,    # new kw
                             reference_id=reference_id)
    )


async def _synth_and_emit(self, text, emotion, sequence, *,
                          session_key: str | None,            # new kw
                          reference_id: str | None = None) -> None:
    if not self._sink_is_enabled(session_key):                # was: ()
        return
    ...
```

### 5.3 `services/channels/desktop_mate.py` (modified)

```python
def _channel_from_session_key(session_key: str | None) -> str | None:
    """Extract the channel-name prefix from nanobot's '<channel>:<chat_id>[:...]' form.

    Returns None if session_key is None, empty, or has no colon.
    """
    if not session_key:
        return None
    prefix, sep, _ = session_key.partition(":")
    return prefix if sep else None


class LazyChannelTTSSink:
    def __init__(self, mode_map: ChannelModeMap) -> None:    # required arg
        self._mode_map = mode_map

    def is_enabled(self, session_key: str | None = None) -> bool:
        mode = self._mode_map.lookup(_channel_from_session_key(session_key))
        if mode is not TTSMode.STREAMING:
            return False
        try:
            return get_desktop_mate_channel().is_tts_enabled_for_current_stream()
        except RuntimeError:
            return True   # channel not constructed yet — preserves existing race-tolerance

    # send_tts_chunk: unchanged
    # get_reference_id_for_session: unchanged
```

### 5.4 `launcher.py` (modified)

```python
def _resolve_tts_modes_path() -> str:
    return os.getenv(
        "TTS_MODES_PATH",
        os.path.join(os.getcwd(), "resources", "tts_channel_modes.yml"),
    )


def _build_tts_hook() -> TTSHook:
    rules_path = _resolve_tts_rules_path()
    if not os.path.exists(rules_path):
        raise FileNotFoundError(...)   # existing

    modes_path = _resolve_tts_modes_path()
    if not os.path.exists(modes_path):
        raise FileNotFoundError(
            f"TTS channel modes YAML not found at {modes_path!r}. Set "
            "TTS_MODES_PATH or place the file at "
            "<workspace>/resources/tts_channel_modes.yml. To run without TTS set "
            "TTS_ENABLED=0."
        )
    mode_map = load_channel_modes(modes_path)

    # ... existing pipeline ...
    return TTSHook(..., sink=LazyChannelTTSSink(mode_map=mode_map), ...)
```

Plus the env-var rename inside `_build_tts_hook`:

| Old | New |
|---|---|
| `YURI_TTS_URL` | `TTS_URL` |
| `YURI_TTS_REF_AUDIO` | `TTS_REF_AUDIO` |
| `YURI_TTS_BARRIER_TIMEOUT` | `TTS_BARRIER_TIMEOUT` |
| `YURI_TTS_RULES_PATH` | `TTS_RULES_PATH` |

And in `_hooks_factory`:

| Old | New |
|---|---|
| `YURI_TTS_ENABLED` | `TTS_ENABLED` |

### 5.5 `resources/tts_channel_modes.yml` (new — workspace file)

```yaml
# Per-channel TTS dispatch mode.
# - streaming:  per-sentence chunks to a live sink (DesktopMate, Unity)
# - attachment: full-turn audio as voice-note (Telegram, WhatsApp, Matrix) — DECLARED, NOT YET IMPLEMENTED
# - none:       text-only response, no synthesis (Slack, Discord, ...)
default: none
channels:
  desktop_mate: streaming
  telegram: attachment   # mode declared; ATTACHMENT pipeline TBD in a later PR
  slack: none
```

## 6. Data flow

### 6.1 Streaming channel (DesktopMate)

```
DesktopMate inbound (session_key="desktop_mate:chat42")
  → AgentLoop → TTSHook.on_stream/on_stream_end
       → _dispatch_sentence
            → sink.is_enabled("desktop_mate:chat42")
                 → _channel_from_session_key → "desktop_mate"
                 → mode_map.lookup → STREAMING
                 → get_desktop_mate_channel().is_tts_enabled_for_current_stream() → True
                 → True
            → asyncio.create_task(_synth_and_emit(..., session_key=...))
                 → sink.is_enabled() second-chance check → True
                 → synthesize → sink.send_tts_chunk → DesktopMate
```

### 6.2 None channel (Slack thread)

```
Slack inbound (session_key="slack:C123:T456")
  → ... → _dispatch_sentence
       → sink.is_enabled("slack:C123:T456")
            → mode_map.lookup("slack") → NONE
            → return False
       → early return: no synthesis, no task, no sequence bump
```

`on_stream_end` finds `state.pending` empty, barrier passes immediately, channel emits `stream_end` normally.

### 6.3 session_key is None (Slack DM, Telegram non-topic)

```
mode_map.lookup(None) → default (NONE) → False → skip synthesis
```

### 6.4 Attachment channel (Telegram, today)

```
Telegram inbound (session_key="telegram:chat789:topic:42")
  → ... → mode_map.lookup("telegram") → ATTACHMENT
       → mode is not STREAMING → False
       → skip synthesis (text-only response)
```

When `AttachmentTTSHook` is added in a future PR, it will use a separate
sink whose `is_enabled` returns True iff mode is ATTACHMENT. The two hooks
gate independently and never both fire on the same sentence.

## 7. Error handling

### Boot

| Scenario | Behavior |
|---|---|
| `TTS_ENABLED=0` | TTS hook not constructed; `tts_channel_modes.yml` not checked. |
| `TTS_ENABLED=1` & file missing | `FileNotFoundError` with actionable message naming `TTS_MODES_PATH` and `TTS_ENABLED=0` escape. |
| Empty file | `ChannelModeMap(default=NONE, channels={})`. Boot proceeds, all channels NONE. |
| YAML parse error | `yaml.YAMLError` propagates; boot aborts. |
| `default: streamign` (typo) | `ValueError("Invalid TTS mode 'streamign' for 'default' in <path>")`. |
| `channels: {telegram: brodcast}` | `ValueError` naming both the channel and the bad value. |
| `default:` key missing | Implicit `NONE` (per design). |
| `channels:` key missing | Empty dict; all channels resolve to `default`. |
| Unknown top-level keys | Silently ignored (yaml.safe_load returns dict; we read only the keys we know). |

### Runtime

| Scenario | Behavior |
|---|---|
| `session_key` is None | `_channel_from_session_key` returns None → `default` mode applies. |
| `session_key` has no colon (`"weird"`) | `partition(":")` gives empty `sep` → returns None → `default`. |
| Channel name not in map | `default` mode applies. |
| STREAMING channel + DesktopMate not yet constructed | `RuntimeError` caught; `is_enabled` returns True (preserves existing race-tolerance — sink will silently drop in `send_tts_chunk` if channel still missing). |
| Sink without `is_enabled` method (test mocks) | `_sink_is_enabled` returns True (backward compat preserved). |
| Sink with old `is_enabled()` signature (no kwarg) | `TypeError: takes 1 positional argument but 2 were given` — loud failure. Test mocks must be updated as part of this PR. |

### Logging

- One INFO at boot: `TTS modes loaded: streaming={desktop_mate}, attachment={telegram}, default=none`.
- No per-sentence gating logs (would be noisy; the gating's effect is observable via the absence of audio frames).

## 8. Configuration

### Environment variables

**This PR (TTS group, drops `YURI_` prefix):**

| Variable | Default | Purpose |
|---|---|---|
| `TTS_ENABLED` | `1` | Master TTS on/off. `0` skips hook + modes file check entirely. |
| `TTS_URL` | `http://192.168.0.41:8091` | Irodori synthesizer endpoint. |
| `TTS_REF_AUDIO` | (none) | Default reference audio ID. |
| `TTS_BARRIER_TIMEOUT` | `30` | Stream-end barrier seconds. |
| `TTS_RULES_PATH` | `<cwd>/resources/tts_rules.yml` | Emotion mapping YAML. |
| `TTS_MODES_PATH` | `<cwd>/resources/tts_channel_modes.yml` | Channel-mode YAML (new). |

**Out of scope, unchanged:** `YURI_LTM_*`, `YURI_IDLE_*`, `YURI_NANOBOT_CONFIG`, `YURI_WORKSPACE`.

### YAML schema

```yaml
default: <none|streaming|attachment>      # optional, defaults to "none"
channels:                                  # optional, defaults to {}
  <channel_name>: <none|streaming|attachment>
```

## 9. Migration

### Workspace `.env` update (single operator, no fade-out cycle)

In `yuri/.env`, rename:

```
YURI_TTS_ENABLED        → TTS_ENABLED
YURI_TTS_URL            → TTS_URL
YURI_TTS_REF_AUDIO      → TTS_REF_AUDIO
YURI_TTS_BARRIER_TIMEOUT → TTS_BARRIER_TIMEOUT
YURI_TTS_RULES_PATH     → TTS_RULES_PATH
```

(Add `TTS_MODES_PATH` if overriding the default location.)

### New file in workspace

Create `yuri/resources/tts_channel_modes.yml` with the contents shown in §5.5.

## 10. Testing plan

### `tests/services/tts/test_modes.py` (new)

```
TestLoadChannelModes
  test_happy_path_parses_default_and_channels
  test_empty_file_returns_all_none_map
  test_missing_default_key_implies_none
  test_missing_channels_key_returns_empty_dict
  test_unknown_default_mode_raises_value_error_with_path
  test_unknown_channel_mode_raises_value_error_with_channel_name
  test_yaml_parse_error_propagates
  test_file_not_found_raises

TestChannelModeMapLookup
  test_lookup_known_channel_returns_mapped_mode
  test_lookup_none_returns_default
  test_lookup_unknown_channel_returns_default
```

### `tests/services/channels/test_desktop_mate.py` (modified — additions only)

```
Test_ChannelFromSessionKey
  test_extracts_prefix_from_slack_form
  test_returns_none_for_none_input
  test_returns_none_for_no_colon_input

TestLazyChannelTTSSinkIsEnabled
  test_streaming_channel_with_active_desktop_mate_returns_true
  test_streaming_channel_without_desktop_mate_returns_true        # race-tolerant
  test_streaming_channel_with_tts_off_in_channel_returns_false
  test_none_channel_returns_false
  test_attachment_channel_returns_false                            # this sink is streaming-only
  test_session_key_none_uses_default_mode
  test_unknown_channel_in_session_key_uses_default_mode
```

Existing send-frame tests untouched.

### `tests/services/hooks/test_tts_hook.py` (modified — additions only)

```
TestTTSHookSessionKeyPlumbing
  test_dispatch_sentence_passes_session_key_to_is_enabled
  test_synth_and_emit_passes_session_key_in_second_chance_check
  test_disabled_sink_skips_dispatch_no_task_no_sequence_bump
  test_sink_without_is_enabled_method_treated_as_always_enabled
```

### `tests/test_launcher.py` (modified — env-var rename + new helper tests)

- Update existing `test_tts_rules_path_env_override_wins` to use `TTS_RULES_PATH`.
- Existing `_clear_yuri_env` autouse fixture extended to also strip `TTS_*`. (No rename — LTM/IDLE still use `YURI_`.)

```
TestResolveTtsModesPath
  test_modes_path_defaults_to_cwd_resources_modes_yml
  test_modes_path_env_override_wins

TestBuildTtsHookFailsWhenModesMissing
  test_raises_file_not_found_with_actionable_message
  test_does_not_check_modes_when_tts_disabled
```

### E2E

- Existing DesktopMate scenarios in `tests/e2e/test_live_scenarios.py` must still pass with the new `mode_map={desktop_mate: streaming, default: none}`.
- E2E conftest must lay down `tts_channel_modes.yml` in the e2e workspace.
- **No new e2e scenarios.** Slack-side gating is covered by unit tests; an e2e Slack mock is out of scope.

## 11. Verification criteria

A definition-of-done checklist for the implementation PR. Each item is independently verifiable.

1. `pytest tests/services/tts/test_modes.py` — all green.
2. `pytest tests/services/channels/test_desktop_mate.py` — all green (existing + new).
3. `pytest tests/services/hooks/test_tts_hook.py` — all green (existing + new).
4. `pytest tests/test_launcher.py` — all green (after env-var rename).
5. `bash scripts/e2e.sh` — all green (per CODING_RULES §10).
6. Boot with `TTS_ENABLED=1` and modes file deleted → process exits with the FileNotFoundError message text shown in §5.4.
7. Boot with `TTS_ENABLED=0` and modes file deleted → process boots normally.
8. Manual: edit `yuri/resources/tts_channel_modes.yml` to set `slack: streaming`, restart, send a Slack message — observe synth task created (logged INFO at boot, audible if DesktopMate connected). Revert. (Sanity check that the gate is the only thing in the way.)
9. `grep -r 'YURI_TTS_' src/ tests/ scripts/` returns zero hits.

## 12. Out of scope / future work

- `AttachmentTTSHook`: separate hook lifecycle (turn-aggregate, not per-sentence), different sink for voice-note file delivery. Likely targets Telegram first.
- Rename of `YURI_LTM_*`, `YURI_IDLE_*`, `YURI_NANOBOT_CONFIG`, `YURI_WORKSPACE` to drop the workspace-name prefix. Mechanical change but separate scope.
- Hot reload of `tts_channel_modes.yml`.
- Channel-name validation (warn if a YAML key isn't a known nanobot channel). Risky — depends on nanobot's channel registry being initialized before YAML load, and a typo silently falls through to `default` which is `none` (safe). Not worth the wiring.

## 13. Open items pre-implementation

None. All assumptions in §3 are verified against current source.
