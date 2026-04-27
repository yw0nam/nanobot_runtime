# TTS Attachment Pipeline (Telegram) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the ATTACHMENT TTS dispatch mode so Telegram (and any future ATTACHMENT-mode channel) receives one OGG/Opus voice-note per agent turn, while preserving the existing STREAMING path to DesktopMate.

**Architecture:** A new `AttachmentTTSSink` buffers `TTSChunk`s per session, and on session-end concatenates the WAV fragments, encodes them to OGG/Opus via an ffmpeg subprocess, and publishes the resulting voice-note as an `OutboundMessage` on the existing `MessageBus` so nanobot's `ChannelManager` routes it through `TelegramChannel.send()` (which already calls `bot.send_voice` for `.ogg` media). The `TTSSink` ABC gains an `on_session_end` notification so the sink learns when the TTS Barrier has drained. A new `MultiplexingTTSSink` routes per-chunk dispatches to either the existing `LazyChannelTTSSink` (STREAMING) or the new `AttachmentTTSSink` (ATTACHMENT) based on the channel mode lookup. The launcher swaps its single sink for the multiplexer.

**Tech Stack:** Python 3.10+ with native union/generic syntax, Pydantic v2 (no dataclasses for config), Loguru, `asyncio` subprocess + system `ffmpeg` for Opus encoding, pytest + `pytest-asyncio`, nanobot `MessageBus` / `OutboundMessage` / `AgentHook` interfaces.

**Constraints (from user):**
- All code lives in `yuri/nanobot_runtime/`. Do NOT modify `yuri/nanobot/` (upstream fork) or anything in `yuri/` outside `nanobot_runtime/`.
- `yuri/` itself is not a git repo; only `yuri/nanobot_runtime/` is. All commits land there, on a `feat/` branch off `develop`, then PR'd to `develop`.
- TTS rules in CODING_RULES.md apply: absolute imports, no `from __future__`, Pydantic v2 + `Field(description=)`, Loguru only, `T | None`, snake_case files, `# ── Section ──` comment style, fail loud, TDD mandatory.

---

## File Structure

### New files

| Path | Responsibility |
|------|----------------|
| `src/nanobot_runtime/services/tts/encoder.py` | `VoiceEncoder` ABC + `OpusEncoder` concrete (ffmpeg subprocess). ABC defines `encode_wav_chunks(chunks) -> bytes`. Tests fakes inherit the ABC so a missing method fails at construction. |
| `src/nanobot_runtime/services/tts/attachment_sink.py` | `AttachmentTTSSink` — implements `TTSSink`. Buffers `(sequence, audio_bytes)` per session, flushes via encoder + bus on `on_session_end`. |
| `src/nanobot_runtime/services/tts/multiplex_sink.py` | `MultiplexingTTSSink` — implements `TTSSink`. Looks up `ChannelModeMap` per call and forwards to the appropriate downstream sink. |
| `tests/services/tts/test_encoder.py` | Encoder unit tests. |
| `tests/services/tts/test_attachment_sink.py` | AttachmentTTSSink unit tests with fake bus + fake encoder. |
| `tests/services/tts/test_multiplex_sink.py` | MultiplexingTTSSink unit tests with fake child sinks. |
| `tests/services/hooks/test_tts_hook_session_end.py` | TTSHook regression: `on_session_end` is awaited after barrier on `on_stream_end(resuming=False)` and NOT on `resuming=True`. |

### Modified files

| Path | Change |
|------|--------|
| `src/nanobot_runtime/services/hooks/tts.py` | Add `on_session_end(session_key: str \| None) -> None` as `@abstractmethod` on `TTSSink` ABC — every subclass must explicitly implement (no silent inheritance of behavior). `TTSHook.on_stream_end(resuming=False)` calls it after the TTS Barrier. |
| `src/nanobot_runtime/services/channels/desktop_mate.py` | `LazyChannelTTSSink` overrides `on_session_end` as an explicit no-op with a docstring (so streaming path stays unchanged). No behavior change. |
| `src/nanobot_runtime/services/tts/modes.py` | Remove the "ATTACHMENT pipeline TBD — silently equivalent to NONE" warning block (lines 122-137). Replace with a passive log line listing implemented modes per channel. |
| `src/nanobot_runtime/launcher.py` | `_build_tts_hook` constructs `AttachmentTTSSink` + `LazyChannelTTSSink`, wraps both in `MultiplexingTTSSink`, passes to `TTSHook`. |
| `tests/services/channels/test_tts_sink_wiring.py` | Update to expect a multiplexer at the top of the wiring (existing test must not break). |

---

## Task 1: Add `on_session_end` to the TTSSink ABC

**Why first:** Every other component depends on this hook. Adding it as a concrete no-op (not abstract) means the existing `LazyChannelTTSSink` keeps working without changes — we only add an explicit override there for clarity.

**Files:**
- Modify: `src/nanobot_runtime/services/hooks/tts.py:96-122` (the `TTSSink` ABC)
- Test: `tests/services/hooks/test_tts_hook_session_end.py` (new)

- [ ] **Step 1.1: Write the failing test for the new hook contract**

Create `tests/services/hooks/test_tts_hook_session_end.py`:

```python
import asyncio
from typing import Any

import pytest
from nanobot.agent.hook import AgentHookContext

from nanobot_runtime.services.hooks.tts import TTSChunk, TTSHook, TTSSink


class _RecordingSink(TTSSink):
    def __init__(self) -> None:
        self.session_ends: list[str | None] = []
        self.chunks: list[TTSChunk] = []

    def is_enabled(self, session_key: str | None) -> bool:
        return True

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        self.chunks.append(chunk)

    async def on_session_end(self, session_key: str | None) -> None:
        self.session_ends.append(session_key)


class _StubChunker:
    def feed(self, delta: str) -> list[str]:
        return [delta] if delta else []

    def flush(self) -> str | None:
        return None


class _StubPreprocessor:
    def process(self, sentence: str) -> tuple[str, str | None]:
        return sentence, None


class _StubEmotionMapper:
    def map(self, emotion: str | None) -> list[dict[str, Any]]:
        return []


class _StubSynth:
    async def synthesize(self, text: str, *, reference_id: str | None = None) -> str | None:
        return "AAAA"


@pytest.fixture
def hook_with_recording_sink() -> tuple[TTSHook, _RecordingSink]:
    sink = _RecordingSink()
    hook = TTSHook(
        chunker_factory=_StubChunker,
        preprocessor=_StubPreprocessor(),
        emotion_mapper=_StubEmotionMapper(),
        synthesizer=_StubSynth(),
        sink=sink,
        barrier_timeout_seconds=2.0,
    )
    return hook, sink


class TestTTSHookSessionEnd:
    @pytest.mark.asyncio
    async def test_on_session_end_invoked_after_barrier_on_terminal_stream_end(
        self, hook_with_recording_sink: tuple[TTSHook, _RecordingSink]
    ) -> None:
        hook, sink = hook_with_recording_sink
        ctx = AgentHookContext(session_key="telegram:42")
        await hook.on_stream(ctx, "hello.")
        await hook.on_stream_end(ctx, resuming=False)
        assert sink.session_ends == ["telegram:42"]

    @pytest.mark.asyncio
    async def test_on_session_end_not_invoked_when_resuming_for_tool_call(
        self, hook_with_recording_sink: tuple[TTSHook, _RecordingSink]
    ) -> None:
        hook, sink = hook_with_recording_sink
        ctx = AgentHookContext(session_key="telegram:42")
        await hook.on_stream(ctx, "hello.")
        await hook.on_stream_end(ctx, resuming=True)
        assert sink.session_ends == []
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/hooks/test_tts_hook_session_end.py -v`
Expected: FAIL — `_RecordingSink` defines `on_session_end` so it instantiates fine, but `TTSHook.on_stream_end` does not call it yet (Step 1.4 adds that). So the assertion `assert sink.session_ends == ["telegram:42"]` fails with `AssertionError: assert [] == ['telegram:42']`. The second test (`resuming=True`) may PASS coincidentally — that's expected; it becomes meaningful only after Step 1.4 wires the call site.

- [ ] **Step 1.3: Add `on_session_end` to TTSSink ABC as abstractmethod**

Edit `src/nanobot_runtime/services/hooks/tts.py`. In the `TTSSink` class (around line 96-122), after the existing `is_enabled` abstractmethod, add:

```python
    @abstractmethod
    async def on_session_end(self, session_key: str | None) -> None:
        """Notify the sink that the TTS Barrier has drained for this session.

        The hook calls this exactly once per terminal ``on_stream_end``
        (i.e. ``resuming=False``), AFTER awaiting all pending synth tasks.
        Streaming sinks should implement this as a no-op; attachment-style
        sinks use it as the flush trigger.

        ``session_key`` is required positionally — pass ``None`` explicitly
        when no key is available.

        Made abstract (not a default no-op) so any sink that forgets the
        method fails loud at construction time with a clear ``TypeError``,
        not silently at the dispatch hot path.
        """
```

This is a **breaking change** for any downstream `TTSSink` subclass that has not yet implemented `on_session_end` — including:

- `LazyChannelTTSSink` → handled in Task 2 (explicit no-op override).
- `_RecordingSink` test fixture in `test_tts_hook_session_end.py` → already implements it (Step 1.1).
- Any other `TTSSink` subclasses in the test suite — Task 1.6 finds them via the regression sweep, and they get an explicit no-op override added in the same commit. The plan handles this via the regression sweep step, not separately.

- [ ] **Step 1.4: Wire `on_session_end` into `TTSHook.on_stream_end`**

Edit `src/nanobot_runtime/services/hooks/tts.py`, in `on_stream_end`. Currently the method ends by popping the per-session bucket. Change the tail to:

```python
        # Turn fully wrapped — drop the per-session bucket so the next turn
        # for the same session starts clean (sequence back to 0, fresh chunker).
        self._states.pop(key, None)

        # Notify sink AFTER state cleanup so a sink callback that re-enters
        # the hook (e.g. logging) sees a clean slate. Failures here must not
        # bubble — the agent turn has already completed successfully.
        try:
            await self._sink.on_session_end(key)
        except Exception:
            logger.exception(
                "TTS sink on_session_end raised (session={})", key
            )
```

The `try/except` is intentional and matches the existing `_synth_and_emit` pattern — sink failures must not corrupt agent-loop state. This is NOT silent swallowing: traceback is logged via `logger.exception`.

- [ ] **Step 1.5: Run test to verify it passes**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/hooks/test_tts_hook_session_end.py -v`
Expected: 2 PASSED.

- [ ] **Step 1.6: Run full hook test file to find any TTSSink subclasses missing the new abstractmethod**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/hooks/test_tts_hook.py tests/services/hooks/test_tts_hook_session_end.py -v`

Expected outcome: **failures of type `TypeError: Can't instantiate abstract class <FakeSink> with abstract method on_session_end`** for every test fake/subclass that hasn't implemented the new method. For each such fake, add an explicit no-op:

```python
    async def on_session_end(self, session_key: str | None) -> None:
        return None
```

Repeat run until clean. Do NOT skip this — silently letting a subclass fail at runtime defeats the point of making it abstract.

- [ ] **Step 1.7: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add src/nanobot_runtime/services/hooks/tts.py tests/services/hooks/test_tts_hook_session_end.py
git commit -m "feat(tts): add TTSSink.on_session_end + TTSHook barrier notification"
```

---

## Task 2: Explicit no-op override on `LazyChannelTTSSink`

**Why:** Make the streaming path's "I don't care about session_end" decision visible in the code, not relying on inheritance silence.

**Files:**
- Modify: `src/nanobot_runtime/services/channels/desktop_mate.py:344-419` (the `LazyChannelTTSSink` class)
- Test: `tests/services/channels/test_desktop_mate.py` (add one test, do not create a new file — co-locate with existing `LazyChannelTTSSink` tests)

- [ ] **Step 2.1: Write the test**

In `tests/services/channels/test_desktop_mate.py`, append a test that imports the existing fixtures pattern from the file and asserts:

```python
class TestLazyChannelTTSSinkSessionEnd:
    @pytest.mark.asyncio
    async def test_on_session_end_is_noop_and_does_not_raise(self) -> None:
        from nanobot_runtime.services.channels.desktop_mate import LazyChannelTTSSink
        from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode

        sink = LazyChannelTTSSink(
            mode_map=ChannelModeMap(default=TTSMode.NONE, channels={"desktop_mate": TTSMode.STREAMING})
        )
        # Should not raise even with no channel registered.
        await sink.on_session_end("desktop_mate:abc")
        await sink.on_session_end(None)
```

- [ ] **Step 2.2: Run the test to verify it currently passes (default no-op inherited from ABC)**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/channels/test_desktop_mate.py::TestLazyChannelTTSSinkSessionEnd -v`
Expected: PASS (because Task 1 already added a no-op default).

- [ ] **Step 2.3: Add explicit override on `LazyChannelTTSSink`**

In `src/nanobot_runtime/services/channels/desktop_mate.py`, inside the `LazyChannelTTSSink` class (around line 401, after `get_reference_id_for_session`), add:

```python
    async def on_session_end(self, session_key: str | None) -> None:
        """Streaming sinks have nothing to flush — chunks were emitted live.

        Explicit override (not relying on the ABC default) so a future
        contract change to ``on_session_end`` is visible in the streaming
        path during code review.
        """
        return None
```

- [ ] **Step 2.4: Re-run the test to confirm still passing**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/channels/test_desktop_mate.py::TestLazyChannelTTSSinkSessionEnd -v`
Expected: PASS.

- [ ] **Step 2.5: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add src/nanobot_runtime/services/channels/desktop_mate.py tests/services/channels/test_desktop_mate.py
git commit -m "feat(tts): explicit on_session_end no-op on LazyChannelTTSSink"
```

---

## Task 3: `OpusEncoder` (WAV → OGG/Opus via ffmpeg)

**Interface contract:** the encoder is an ABC, not a Protocol — consistent with `TTSSink`. The concrete `OpusEncoder` (ffmpeg subprocess) inherits from it. Test fakes also inherit from the ABC, so a fake that forgets a method fails loud at construction time. Existing dependency Protocols on `TTSHook` (`SentenceChunker`, `TextPreprocessor`, `EmotionMapper`, `TTSSynthesizer`) are intentionally left as Protocols in this PR — converting them is a separate, broader refactor and out of scope.

**Why:** Telegram's `bot.send_voice` only displays the voice-note UI (waveform + speed controls) when the file is OGG container with Opus codec. Other formats (mp3, wav) fall back to a generic audio attachment — visible but worse UX. This encoder is a focused, mockable boundary.

**Files:**
- Create: `src/nanobot_runtime/services/tts/encoder.py`
- Test: `tests/services/tts/test_encoder.py`

**Pre-flight:** Engineer must verify `ffmpeg` is on PATH. Run `ffmpeg -version` in the dev container. If absent, install via `apt install ffmpeg`. Document in `docs/setup.md` at the end of this task.

- [ ] **Step 3.1: Write the failing test**

Create `tests/services/tts/test_encoder.py`:

```python
import asyncio
import shutil
import struct
import wave
from io import BytesIO

import pytest

from nanobot_runtime.services.tts.encoder import OpusEncoder, VoiceEncoder, VoiceEncoderError


def _silent_wav(duration_seconds: float = 0.1, sample_rate: int = 16000) -> bytes:
    """Build a tiny WAV byte-string of silence for round-trip testing."""
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        n_frames = int(duration_seconds * sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


@pytest.fixture(scope="module")
def encoder() -> OpusEncoder:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH — install via `apt install ffmpeg`.")
    return OpusEncoder()


class TestOpusEncoder:
    def test_opus_encoder_is_voice_encoder_subclass(self) -> None:
        assert issubclass(OpusEncoder, VoiceEncoder)

    @pytest.mark.asyncio
    async def test_encode_wav_chunks_returns_ogg_opus_bytes(
        self, encoder: OpusEncoder
    ) -> None:
        chunks = [_silent_wav(0.1), _silent_wav(0.1)]
        result = await encoder.encode_wav_chunks(chunks)
        assert isinstance(result, bytes)
        assert len(result) > 0
        # OGG container magic
        assert result.startswith(b"OggS"), "expected OGG container magic"

    @pytest.mark.asyncio
    async def test_encode_empty_chunk_list_raises(
        self, encoder: OpusEncoder
    ) -> None:
        with pytest.raises(VoiceEncoderError, match="no audio chunks"):
            await encoder.encode_wav_chunks([])

    @pytest.mark.asyncio
    async def test_encode_invalid_wav_raises_with_ffmpeg_stderr(
        self, encoder: OpusEncoder
    ) -> None:
        with pytest.raises(VoiceEncoderError, match="ffmpeg"):
            await encoder.encode_wav_chunks([b"not-a-wav-at-all"])
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_encoder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nanobot_runtime.services.tts.encoder'`.

- [ ] **Step 3.3: Implement the encoder ABC + ffmpeg-backed concrete**

Create `src/nanobot_runtime/services/tts/encoder.py`:

```python
"""WAV → OGG/Opus encoder for ATTACHMENT-mode TTS delivery.

The ABC ``VoiceEncoder`` defines the contract; ``OpusEncoder`` is the
production implementation backed by an ffmpeg subprocess. Test fakes also
inherit from the ABC, so a fake that forgets a method fails at construction
time with a clear ``TypeError`` rather than at the dispatch hot path.

We accept a list of raw WAV byte-strings (one per synthesized sentence),
concatenate them via the ffmpeg ``concat`` demuxer, and produce a single
OGG/Opus stream suitable for Telegram's ``bot.send_voice`` (which shows
the waveform + speed UI only for OGG/Opus, not mp3 or generic audio).

ffmpeg is a runtime dependency: the encoder fails loud at first use if
the binary is missing rather than silently skipping voice delivery.
"""
import asyncio
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class VoiceEncoderError(RuntimeError):
    """Raised when an encoder cannot produce output (missing binary, bad input, ...)."""


class VoiceEncoder(ABC):
    """Contract for components that turn WAV chunks into a single voice-note byte-string.

    Promoted to ABC (rather than a typing.Protocol) so every implementation —
    production (`OpusEncoder`) and test fake — explicitly inherits the
    contract. Forgetting a method fails at construction time with a
    ``TypeError``, matching the pattern used by `TTSSink`.
    """

    @abstractmethod
    async def encode_wav_chunks(self, chunks: list[bytes]) -> bytes:
        """Encode a sequence of WAV byte-strings into one packaged voice-note stream.

        Args:
            chunks: WAV byte-strings, in playback order.

        Returns:
            Encoded bytes ready to be saved to disk or attached to an
            outbound message.

        Raises:
            VoiceEncoderError: on missing dependencies, invalid input, or
                encoder-process failure. Implementations must never silently
                return empty bytes.
        """


class OpusEncoderConfig(BaseModel):
    """Tunable parameters for `OpusEncoder`. Pydantic per CODING_RULES §6."""

    model_config = ConfigDict(frozen=True)

    sample_rate_hz: int = Field(
        default=48_000,
        ge=8_000,
        le=48_000,
        description="Output sample rate. Telegram's voice-note recommendation is 48kHz.",
    )
    bitrate_kbps: int = Field(
        default=32,
        ge=6,
        le=510,
        description="Opus target bitrate in kbps. 32 is a good speech default.",
    )
    application: str = Field(
        default="voip",
        description="Opus application mode: 'voip', 'audio', or 'lowdelay'.",
    )


class OpusEncoder(VoiceEncoder):
    """Concatenate WAV chunks and encode them to a single OGG/Opus byte-string via ffmpeg."""

    def __init__(self, config: OpusEncoderConfig | None = None) -> None:
        self._config: OpusEncoderConfig = config or OpusEncoderConfig()
        self._ffmpeg_path: str | None = shutil.which("ffmpeg")

    async def encode_wav_chunks(self, chunks: list[bytes]) -> bytes:
        """See `VoiceEncoder.encode_wav_chunks`."""
        if not chunks:
            raise VoiceEncoderError("no audio chunks to encode")
        if self._ffmpeg_path is None:
            raise VoiceEncoderError(
                "ffmpeg binary not found on PATH. Install with `apt install ffmpeg`."
            )

        with tempfile.TemporaryDirectory(prefix="nanobot_tts_") as tmp_str:
            tmp = Path(tmp_str)
            wav_paths: list[Path] = []
            for idx, wav in enumerate(chunks):
                p = tmp / f"chunk_{idx:04d}.wav"
                p.write_bytes(wav)
                wav_paths.append(p)

            concat_list = tmp / "concat.txt"
            concat_list.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in wav_paths)
            )

            return await self._run_ffmpeg(concat_list)

    # ── Internals ────────────────────────────────────────────────────────

    async def _run_ffmpeg(self, concat_list: Path) -> bytes:
        cfg = self._config
        cmd = [
            self._ffmpeg_path or "ffmpeg",  # narrowed: None case raised above
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-ar", str(cfg.sample_rate_hz),
            "-ac", "1",
            "-c:a", "libopus",
            "-b:a", f"{cfg.bitrate_kbps}k",
            "-application", cfg.application,
            "-f", "ogg",
            "pipe:1",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            raise VoiceEncoderError(f"ffmpeg failed (rc={proc.returncode}): {err_text}")
        if not stdout:
            raise VoiceEncoderError("ffmpeg produced empty output")
        logger.debug(
            "OpusEncoder: encoded {} input chunk file(s) → {} bytes OGG/Opus",
            sum(1 for _ in concat_list.read_text().splitlines() if _.strip()),
            len(stdout),
        )
        return stdout
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_encoder.py -v`
Expected: 3 PASSED (or skipped if no ffmpeg).

- [ ] **Step 3.5: Document the ffmpeg dependency**

Append to `docs/setup.md` (whatever section lists system dependencies — if none exists, add a `## System Dependencies` section near the top):

```markdown
## System Dependencies

- **ffmpeg** — required for ATTACHMENT-mode TTS (Telegram voice-note encoding).
  Install via `apt install ffmpeg` on Debian/Ubuntu, `brew install ffmpeg` on macOS.
  Used by `nanobot_runtime.services.tts.encoder.OpusEncoder` to concatenate
  per-sentence WAV chunks into a single OGG/Opus voice file.
```

- [ ] **Step 3.6: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add src/nanobot_runtime/services/tts/encoder.py tests/services/tts/test_encoder.py docs/setup.md
git commit -m "feat(tts): add OpusEncoder (WAV chunks → OGG/Opus via ffmpeg)"
```

---

## Task 4: `AttachmentTTSSink` — buffer + flush

**Files:**
- Create: `src/nanobot_runtime/services/tts/attachment_sink.py`
- Test: `tests/services/tts/test_attachment_sink.py`

### Design notes
- Buffers `(sequence, audio_bytes)` per session_key. `audio_base64` of `None` (synth failure) is dropped from the buffer with a warning so a single failed sentence doesn't corrupt the whole turn.
- `is_enabled(session_key)` returns True iff the channel mode is ATTACHMENT for the channel encoded in `session_key` ("<channel>:<chat_id>" prefix).
- On `on_session_end`: pops the buffer, sorts by sequence (defensive — should already be in order), encodes via `OpusEncoder`, writes the OGG to a temp file under the workspace `media_dir` (per nanobot's `get_media_dir(channel_name)` helper), then enqueues an `OutboundMessage(channel=..., chat_id=..., content="", media=[path])` on the bus.
- The empty `content=""` is intentional: text was already streamed during the turn via the channel's normal `send_delta` path. The voice file is a *separate* outbound; nanobot's `ChannelManager._send_with_retry` calls `channel.send(msg)` because the message has no `_stream_delta` / `_stream_end` markers, and `TelegramChannel.send` (telegram.py:478) iterates `msg.media` and dispatches `.ogg` files via `bot.send_voice`.

- [ ] **Step 4.1: Write the failing test**

Create `tests/services/tts/test_attachment_sink.py`:

```python
import asyncio
from typing import Any

import pytest
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus

from nanobot_runtime.services.hooks.tts import TTSChunk
from nanobot_runtime.services.tts.attachment_sink import AttachmentTTSSink
from nanobot_runtime.services.tts.encoder import VoiceEncoder
from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode


class _FakeEncoder(VoiceEncoder):
    """Stand-in for OpusEncoder — records inputs, returns canned bytes.

    Inherits from `VoiceEncoder` ABC so the fake satisfies the same
    contract as the production encoder. Forgetting `encode_wav_chunks`
    fails at construction time.
    """

    def __init__(self) -> None:
        self.calls: list[list[bytes]] = []
        self.return_value: bytes = b"OggS-fake-opus-payload"

    async def encode_wav_chunks(self, chunks: list[bytes]) -> bytes:
        self.calls.append(list(chunks))
        return self.return_value


@pytest.fixture
def mode_map_telegram_attachment() -> ChannelModeMap:
    return ChannelModeMap(
        default=TTSMode.NONE,
        channels={"telegram": TTSMode.ATTACHMENT, "desktop_mate": TTSMode.STREAMING},
    )


@pytest.fixture
def sink(
    tmp_path, mode_map_telegram_attachment: ChannelModeMap
) -> tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]:
    bus = MessageBus()
    encoder = _FakeEncoder()
    s = AttachmentTTSSink(
        mode_map=mode_map_telegram_attachment,
        bus=bus,
        encoder=encoder,
        media_dir=tmp_path,
    )
    return s, bus, encoder


def _wav(seq: int) -> str:
    """Stand-in base64 audio for testing — content irrelevant to sink logic."""
    import base64
    return base64.b64encode(f"WAV-{seq}".encode()).decode()


class TestAttachmentTTSSinkEnabled:
    def test_is_enabled_true_for_attachment_channel(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, _, _ = sink
        assert s.is_enabled("telegram:42") is True

    def test_is_enabled_false_for_streaming_channel(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, _, _ = sink
        assert s.is_enabled("desktop_mate:abc") is False

    def test_is_enabled_false_for_unknown_channel(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, _, _ = sink
        assert s.is_enabled("slack:C123") is False

    def test_is_enabled_false_for_none_session_key(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, _, _ = sink
        assert s.is_enabled(None) is False


class TestAttachmentTTSSinkFlush:
    @pytest.mark.asyncio
    async def test_flush_concatenates_chunks_and_publishes_outbound(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, bus, encoder = sink
        await s.send_tts_chunk(TTSChunk(sequence=0, text="hi", audio_base64=_wav(0), emotion=None, keyframes=[]))
        await s.send_tts_chunk(TTSChunk(sequence=1, text="there", audio_base64=_wav(1), emotion=None, keyframes=[]))
        await s.on_session_end("telegram:42")

        assert len(encoder.calls) == 1
        assert len(encoder.calls[0]) == 2  # both chunks fed to encoder

        msg: OutboundMessage = bus.outbound.get_nowait()
        assert msg.channel == "telegram"
        assert msg.chat_id == "42"
        assert msg.content == ""
        assert len(msg.media) == 1
        assert msg.media[0].endswith(".ogg")
        # File must actually exist for ChannelManager to read it.
        from pathlib import Path
        assert Path(msg.media[0]).exists()

    @pytest.mark.asyncio
    async def test_flush_drops_failed_synth_chunks(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, _, encoder = sink
        await s.send_tts_chunk(TTSChunk(sequence=0, text="ok", audio_base64=_wav(0), emotion=None, keyframes=[]))
        await s.send_tts_chunk(TTSChunk(sequence=1, text="fail", audio_base64=None, emotion=None, keyframes=[]))
        await s.send_tts_chunk(TTSChunk(sequence=2, text="ok", audio_base64=_wav(2), emotion=None, keyframes=[]))
        await s.on_session_end("telegram:42")
        assert len(encoder.calls[0]) == 2  # the None one was dropped

    @pytest.mark.asyncio
    async def test_flush_with_no_buffered_chunks_does_not_publish(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, bus, encoder = sink
        await s.on_session_end("telegram:42")
        assert encoder.calls == []
        assert bus.outbound.qsize() == 0

    @pytest.mark.asyncio
    async def test_chunks_for_disabled_session_are_ignored(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, bus, encoder = sink
        # send_tts_chunk gets called with the session embedded in the chunk?
        # No — TTSChunk has no session_key. The hook only calls send_tts_chunk
        # after is_enabled() returned True. So the sink trusts the hook.
        # This test instead verifies that on_session_end for a session whose
        # buffer is empty (because is_enabled gated dispatch) is a no-op.
        await s.on_session_end("slack:C123")
        assert encoder.calls == []
        assert bus.outbound.qsize() == 0

    @pytest.mark.asyncio
    async def test_two_concurrent_sessions_have_independent_buffers(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, bus, encoder = sink
        # Simulate interleaved chunks from two telegram chats.
        # The sink doesn't see session_key per chunk — it sees on_session_end
        # per session. So we test that flushing one session does NOT drain
        # the other. To do this we need the sink to associate buffered
        # chunks with their session. Implementation note: the sink must
        # therefore receive a session_key on send_tts_chunk OR maintain a
        # single "current session" — see implementation choice in 4.2.
        pytest.skip("Covered by implementation-choice decision; see Task 4.2")
```

- [ ] **Step 4.2: Run test to verify it fails AND resolve the per-session buffering question**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_attachment_sink.py -v`
Expected: FAIL with `ModuleNotFoundError`.

**Implementation choice (resolved here, locked in for Step 4.3):** `TTSChunk` has no `session_key` field, so the sink cannot associate an incoming chunk to a session from the chunk alone. There are two options:

- **A. Add `session_key: str | None` to `TTSChunk`** and have `TTSHook._dispatch_sentence` populate it. Cleanest, but touches the hook contract and the streaming sink (which currently ignores it).
- **B. Sink maintains a "current session" set by `is_enabled` returning True.** Hacky — `is_enabled` is a pure check and shouldn't have side effects.

**Decision: A.** The change to `TTSChunk` is additive (`Field(default=None)`), so DesktopMate streaming sink and existing tests are unaffected. We add this in Task 4 alongside the sink itself.

- [ ] **Step 4.3: Add `session_key` to `TTSChunk` and populate in TTSHook**

Edit `src/nanobot_runtime/services/hooks/tts.py`:

In `TTSChunk`, add the field:

```python
class TTSChunk(BaseModel):
    """Data emitted to the TTS sink per completed sentence."""

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(description="Zero-based index of this chunk within the current stream.")
    text: str = Field(description="Cleaned sentence text sent to the TTS engine.")
    audio_base64: str | None = Field(description="Base64-encoded WAV audio, or None on failure.")
    emotion: str | None = Field(description="Detected emotion emoji/tag, or None.")
    keyframes: list[dict[str, Any]] = Field(
        default_factory=list, description="Animation keyframe dicts for the emotion."
    )
    session_key: str | None = Field(
        default=None,
        description="Session this chunk belongs to (for multi-session sinks). None for legacy callers.",
    )
```

In `TTSHook._synth_and_emit`, when constructing the chunk, pass the session_key:

```python
        chunk = TTSChunk(
            sequence=sequence,
            text=text,
            audio_base64=audio_b64,
            emotion=emotion,
            keyframes=keyframes,
            session_key=session_key,
        )
```

(`session_key` is already a parameter to `_synth_and_emit` so no other plumbing is needed.)

- [ ] **Step 4.4: Implement `AttachmentTTSSink`**

Create `src/nanobot_runtime/services/tts/attachment_sink.py`:

```python
"""Buffer-and-flush TTS sink for ATTACHMENT-mode channels.

Per-session buffer of synthesized WAV chunks, flushed on ``on_session_end``
into a single OGG/Opus voice file enqueued onto the message bus.

The sink is the ATTACHMENT-mode analogue of ``LazyChannelTTSSink``:
- ``LazyChannelTTSSink`` pushes per-sentence frames live to DesktopMate.
- ``AttachmentTTSSink`` collects, encodes, and emits one voice-note per turn.

Both are routed by ``MultiplexingTTSSink`` based on ``ChannelModeMap``.
"""
import base64
import time
import uuid
from pathlib import Path

from loguru import logger
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus

from nanobot_runtime.services.hooks.tts import TTSChunk, TTSSink
from nanobot_runtime.services.tts.encoder import VoiceEncoder
from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode


class AttachmentTTSSink(TTSSink):
    """Collect TTSChunks per session, flush as a single OGG/Opus voice-note."""

    def __init__(
        self,
        *,
        mode_map: ChannelModeMap,
        bus: MessageBus,
        encoder: VoiceEncoder,
        media_dir: Path,
    ) -> None:
        self._mode_map: ChannelModeMap = mode_map
        self._bus: MessageBus = bus
        self._encoder: VoiceEncoder = encoder
        self._media_dir: Path = media_dir
        self._media_dir.mkdir(parents=True, exist_ok=True)
        # session_key -> list[(sequence, wav_bytes)]
        self._buffers: dict[str, list[tuple[int, bytes]]] = {}

    # ── Sink contract ───────────────────────────────────────────────────

    def is_enabled(self, session_key: str | None) -> bool:
        channel = self._channel_from_session_key(session_key)
        return self._mode_map.lookup(channel) is TTSMode.ATTACHMENT

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        if chunk.session_key is None:
            logger.warning(
                "AttachmentTTSSink: chunk seq={} has no session_key; dropping",
                chunk.sequence,
            )
            return
        if chunk.audio_base64 is None:
            logger.warning(
                "AttachmentTTSSink: chunk seq={} has no audio (synth failed); dropping",
                chunk.sequence,
            )
            return
        try:
            wav = base64.b64decode(chunk.audio_base64)
        except Exception:
            logger.exception(
                "AttachmentTTSSink: chunk seq={} audio_base64 decode failed; dropping",
                chunk.sequence,
            )
            return
        self._buffers.setdefault(chunk.session_key, []).append((chunk.sequence, wav))

    async def on_session_end(self, session_key: str | None) -> None:
        if session_key is None:
            return
        buffered = self._buffers.pop(session_key, None)
        if not buffered:
            return
        # Sort defensively — TTSHook dispatches in order but synth tasks
        # complete out-of-order, and the sink receives them in completion
        # order. Sequence numbers restore audio order.
        buffered.sort(key=lambda pair: pair[0])
        wav_chunks = [wav for _, wav in buffered]
        try:
            opus_bytes = await self._encoder.encode_wav_chunks(wav_chunks)
        except Exception:
            logger.exception(
                "AttachmentTTSSink: encoder failed for session={}; dropping {} chunks",
                session_key, len(wav_chunks),
            )
            return
        channel = self._channel_from_session_key(session_key)
        chat_id = self._chat_id_from_session_key(session_key)
        if channel is None or chat_id is None:
            logger.warning(
                "AttachmentTTSSink: malformed session_key={}; dropping voice", session_key
            )
            return
        out_path = self._write_voice_file(channel, chat_id, opus_bytes)
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="",
                media=[str(out_path)],
                metadata={"_tts_attachment": True},
            )
        )
        logger.info(
            "🔊 TTS attachment dispatched: channel={} chat_id={} chunks={} bytes={}",
            channel, chat_id, len(wav_chunks), len(opus_bytes),
        )

    # ── Internals ───────────────────────────────────────────────────────

    @staticmethod
    def _channel_from_session_key(session_key: str | None) -> str | None:
        if not session_key:
            return None
        prefix, sep, _ = session_key.partition(":")
        return prefix if sep else None

    @staticmethod
    def _chat_id_from_session_key(session_key: str | None) -> str | None:
        if not session_key:
            return None
        _, sep, rest = session_key.partition(":")
        if not sep or not rest:
            return None
        # nanobot uses "<channel>:<chat_id>[:<thread>...]". For ATTACHMENT
        # mode (text channels), chat_id is the second segment.
        chat_id, _, _ = rest.partition(":")
        return chat_id or None

    def _write_voice_file(self, channel: str, chat_id: str, opus_bytes: bytes) -> Path:
        ts = int(time.time() * 1000)
        name = f"{channel}_{chat_id}_{ts}_{uuid.uuid4().hex[:8]}.ogg"
        out = self._media_dir / name
        out.write_bytes(opus_bytes)
        return out
```

- [ ] **Step 4.5: Un-skip the concurrent-sessions test**

Now that `session_key` is on `TTSChunk` and the sink keys buffers by it, replace the `pytest.skip` in `test_two_concurrent_sessions_have_independent_buffers` with a real assertion:

```python
    @pytest.mark.asyncio
    async def test_two_concurrent_sessions_have_independent_buffers(
        self, sink: tuple[AttachmentTTSSink, MessageBus, _FakeEncoder]
    ) -> None:
        s, bus, encoder = sink
        await s.send_tts_chunk(TTSChunk(sequence=0, text="a", audio_base64=_wav(0), emotion=None, keyframes=[], session_key="telegram:1"))
        await s.send_tts_chunk(TTSChunk(sequence=0, text="b", audio_base64=_wav(0), emotion=None, keyframes=[], session_key="telegram:2"))
        await s.send_tts_chunk(TTSChunk(sequence=1, text="c", audio_base64=_wav(1), emotion=None, keyframes=[], session_key="telegram:1"))

        await s.on_session_end("telegram:1")
        # Only chat 1's buffer flushed
        assert len(encoder.calls) == 1
        assert len(encoder.calls[0]) == 2
        msg1 = bus.outbound.get_nowait()
        assert msg1.chat_id == "1"

        await s.on_session_end("telegram:2")
        assert len(encoder.calls) == 2
        assert len(encoder.calls[1]) == 1
        msg2 = bus.outbound.get_nowait()
        assert msg2.chat_id == "2"
```

Also update the existing `test_flush_concatenates_chunks_and_publishes_outbound` and `test_flush_drops_failed_synth_chunks` tests to pass `session_key="telegram:42"` on each `TTSChunk(...)` constructor call.

- [ ] **Step 4.6: Run all sink tests**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_attachment_sink.py -v`
Expected: ALL PASSED.

- [ ] **Step 4.7: Run hook tests for regression (TTSChunk shape change)**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/hooks/ tests/services/tts/ tests/services/channels/ -v`
Expected: ALL PASSED. If any test breaks because it constructed `TTSChunk(...)` without `session_key`, that's fine — the field defaults to `None` so callers don't need updating, but if a strict equality test fails, update the expected value.

- [ ] **Step 4.8: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add src/nanobot_runtime/services/hooks/tts.py src/nanobot_runtime/services/tts/attachment_sink.py tests/services/tts/test_attachment_sink.py
git commit -m "feat(tts): AttachmentTTSSink — buffer per-session, flush as OGG/Opus voice-note"
```

---

## Task 5: `MultiplexingTTSSink` — route per-chunk by mode

**Files:**
- Create: `src/nanobot_runtime/services/tts/multiplex_sink.py`
- Test: `tests/services/tts/test_multiplex_sink.py`

- [ ] **Step 5.1: Write the failing test**

Create `tests/services/tts/test_multiplex_sink.py`:

```python
import pytest

from nanobot_runtime.services.hooks.tts import TTSChunk, TTSSink
from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode
from nanobot_runtime.services.tts.multiplex_sink import MultiplexingTTSSink


class _RecordingSink(TTSSink):
    def __init__(self, name: str, enabled_for: set[str | None]) -> None:
        self.name = name
        self._enabled_for = enabled_for
        self.chunks: list[TTSChunk] = []
        self.session_ends: list[str | None] = []

    def is_enabled(self, session_key: str | None) -> bool:
        return session_key in self._enabled_for

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        self.chunks.append(chunk)

    async def on_session_end(self, session_key: str | None) -> None:
        self.session_ends.append(session_key)


@pytest.fixture
def sinks() -> tuple[_RecordingSink, _RecordingSink, MultiplexingTTSSink]:
    streaming = _RecordingSink("streaming", {"desktop_mate:abc"})
    attachment = _RecordingSink("attachment", {"telegram:42"})
    mode_map = ChannelModeMap(
        default=TTSMode.NONE,
        channels={"desktop_mate": TTSMode.STREAMING, "telegram": TTSMode.ATTACHMENT},
    )
    mux = MultiplexingTTSSink(
        mode_map=mode_map,
        streaming_sink=streaming,
        attachment_sink=attachment,
    )
    return streaming, attachment, mux


class TestMultiplexingTTSSink:
    def test_is_enabled_routes_streaming_to_streaming_sink(
        self, sinks: tuple[_RecordingSink, _RecordingSink, MultiplexingTTSSink]
    ) -> None:
        _, _, mux = sinks
        assert mux.is_enabled("desktop_mate:abc") is True

    def test_is_enabled_routes_attachment_to_attachment_sink(
        self, sinks: tuple[_RecordingSink, _RecordingSink, MultiplexingTTSSink]
    ) -> None:
        _, _, mux = sinks
        assert mux.is_enabled("telegram:42") is True

    def test_is_enabled_false_for_none_mode_channel(
        self, sinks: tuple[_RecordingSink, _RecordingSink, MultiplexingTTSSink]
    ) -> None:
        _, _, mux = sinks
        assert mux.is_enabled("slack:C123") is False

    @pytest.mark.asyncio
    async def test_send_tts_chunk_routes_by_chunk_session_key(
        self, sinks: tuple[_RecordingSink, _RecordingSink, MultiplexingTTSSink]
    ) -> None:
        streaming, attachment, mux = sinks
        await mux.send_tts_chunk(
            TTSChunk(sequence=0, text="dm", audio_base64="A", emotion=None, keyframes=[], session_key="desktop_mate:abc")
        )
        await mux.send_tts_chunk(
            TTSChunk(sequence=0, text="tg", audio_base64="A", emotion=None, keyframes=[], session_key="telegram:42")
        )
        assert [c.text for c in streaming.chunks] == ["dm"]
        assert [c.text for c in attachment.chunks] == ["tg"]

    @pytest.mark.asyncio
    async def test_on_session_end_fans_out_to_both(
        self, sinks: tuple[_RecordingSink, _RecordingSink, MultiplexingTTSSink]
    ) -> None:
        streaming, attachment, mux = sinks
        # Fan-out is the simple choice: each child decides whether it cares.
        # Streaming sink ignores; attachment sink flushes its buffer.
        await mux.on_session_end("telegram:42")
        assert streaming.session_ends == ["telegram:42"]
        assert attachment.session_ends == ["telegram:42"]
```

- [ ] **Step 5.2: Run test to verify it fails**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_multiplex_sink.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 5.3: Implement the multiplexer**

Create `src/nanobot_runtime/services/tts/multiplex_sink.py`:

```python
"""Mode-based router that fans TTSChunks to either streaming or attachment sink.

The multiplexer is the single sink injected into ``TTSHook``. Per call:

- ``is_enabled(session_key)``: returns the OR of the two child sinks' verdicts
  for that session, gated by the channel's mode in ``ChannelModeMap``.
- ``send_tts_chunk(chunk)``: routes by the chunk's own ``session_key`` field
  (populated by ``TTSHook``) — STREAMING channels go to the streaming sink,
  ATTACHMENT channels to the attachment sink.
- ``on_session_end(session_key)``: fans out to BOTH children. Each decides
  whether to act based on its own buffer state. The streaming sink's
  ``on_session_end`` is a no-op; the attachment sink uses it as the flush
  trigger. Fan-out is intentional — neither sink need know about the other,
  and per-channel routing is settled in the mode map alone.
"""
from loguru import logger

from nanobot_runtime.services.hooks.tts import TTSChunk, TTSSink
from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode


class MultiplexingTTSSink(TTSSink):
    """Fan TTSChunks to STREAMING or ATTACHMENT sink based on channel mode."""

    def __init__(
        self,
        *,
        mode_map: ChannelModeMap,
        streaming_sink: TTSSink,
        attachment_sink: TTSSink,
    ) -> None:
        self._mode_map: ChannelModeMap = mode_map
        self._streaming: TTSSink = streaming_sink
        self._attachment: TTSSink = attachment_sink

    def is_enabled(self, session_key: str | None) -> bool:
        mode = self._mode_map.lookup(self._channel_from_session_key(session_key))
        if mode is TTSMode.STREAMING:
            return self._streaming.is_enabled(session_key)
        if mode is TTSMode.ATTACHMENT:
            return self._attachment.is_enabled(session_key)
        return False

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        mode = self._mode_map.lookup(self._channel_from_session_key(chunk.session_key))
        if mode is TTSMode.STREAMING:
            await self._streaming.send_tts_chunk(chunk)
        elif mode is TTSMode.ATTACHMENT:
            await self._attachment.send_tts_chunk(chunk)
        else:
            logger.warning(
                "MultiplexingTTSSink: chunk seq={} has session_key={} with mode={}; dropping",
                chunk.sequence, chunk.session_key, mode,
            )

    async def on_session_end(self, session_key: str | None) -> None:
        # Fan-out: both children get the notification. Cheap (each is a
        # method call), and avoids asking the multiplexer to remember which
        # child handled which session.
        await self._streaming.on_session_end(session_key)
        await self._attachment.on_session_end(session_key)

    # ── Internals ───────────────────────────────────────────────────────

    @staticmethod
    def _channel_from_session_key(session_key: str | None) -> str | None:
        if not session_key:
            return None
        prefix, sep, _ = session_key.partition(":")
        return prefix if sep else None
```

- [ ] **Step 5.4: Run tests**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_multiplex_sink.py -v`
Expected: 5 PASSED.

- [ ] **Step 5.5: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add src/nanobot_runtime/services/tts/multiplex_sink.py tests/services/tts/test_multiplex_sink.py
git commit -m "feat(tts): MultiplexingTTSSink — route chunks by channel mode"
```

---

## Task 6: Wire the multiplexer into the launcher

**Files:**
- Modify: `src/nanobot_runtime/launcher.py:60-101` (the `_build_tts_hook` function)
- Modify: `tests/services/channels/test_tts_sink_wiring.py` (existing test must pass with the new shape)

- [ ] **Step 6.1: Read the existing wiring test to know what must keep passing**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && cat tests/services/channels/test_tts_sink_wiring.py`

Identify the assertions about sink type. If the test asserts the sink is a `LazyChannelTTSSink`, change to assert it's a `MultiplexingTTSSink` whose streaming child is a `LazyChannelTTSSink`. Make this update minimally — don't rewrite the test.

- [ ] **Step 6.2: Add a focused new wiring test for the multiplexer**

In `tests/services/channels/test_tts_sink_wiring.py` (or a new co-located test file `test_tts_attachment_wiring.py` if the existing file is overloaded), add:

```python
class TestAttachmentSinkWiring:
    def test_launcher_wires_multiplexer_with_attachment_sink_for_telegram(
        self, monkeypatch, tmp_path
    ) -> None:
        """The hook's sink must be a MultiplexingTTSSink whose attachment
        child reports is_enabled=True for telegram session keys."""
        from nanobot_runtime.launcher import _build_tts_hook
        from nanobot_runtime.services.tts.multiplex_sink import MultiplexingTTSSink

        # Set up minimal env + workspace dirs
        rules = tmp_path / "rules.yml"
        rules.write_text("emotions: {}\n")
        modes = tmp_path / "modes.yml"
        modes.write_text(
            "default: none\nchannels:\n  desktop_mate: streaming\n  telegram: attachment\n"
        )
        monkeypatch.setenv("TTS_RULES_PATH", str(rules))
        monkeypatch.setenv("TTS_MODES_PATH", str(modes))
        monkeypatch.setenv("TTS_URL", "http://stub")

        hook = _build_tts_hook()
        sink = hook._sink  # type: ignore[attr-defined] — test-only introspection
        assert isinstance(sink, MultiplexingTTSSink)
        assert sink.is_enabled("telegram:42") is True
        assert sink.is_enabled("desktop_mate:abc") is True
        assert sink.is_enabled("slack:C123") is False
```

- [ ] **Step 6.3: Run the test — expect it to fail because the launcher still wires only LazyChannelTTSSink**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/channels/test_tts_sink_wiring.py -v`
Expected: FAIL on the new test (existing tests may also fail if they introspected the sink type).

- [ ] **Step 6.4: Update `_build_tts_hook` to produce a MultiplexingTTSSink**

Edit `src/nanobot_runtime/launcher.py`. Add imports near the existing TTS imports:

```python
from nanobot.bus.queue import MessageBus
from nanobot.config.paths import get_media_dir

from nanobot_runtime.services.tts.attachment_sink import AttachmentTTSSink
from nanobot_runtime.services.tts.encoder import OpusEncoder
from nanobot_runtime.services.tts.multiplex_sink import MultiplexingTTSSink
```

Change the function signature and body. The current `_build_tts_hook()` takes no args and constructs a sink that doesn't need a bus. The attachment sink needs a bus, so the function now takes the bus as a parameter:

```python
def _build_tts_hook(bus: MessageBus) -> TTSHook:
    """Assemble the TTS pipeline.

    Composition:
      MultiplexingTTSSink
        ├── streaming → LazyChannelTTSSink (DesktopMate)
        └── attachment → AttachmentTTSSink (Telegram, ...)

    The attachment sink encodes per-turn voice-notes via :class:`OpusEncoder`
    and publishes them onto ``bus`` for the channel manager to dispatch.
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

    streaming_sink = LazyChannelTTSSink(mode_map=mode_map)
    attachment_sink = AttachmentTTSSink(
        mode_map=mode_map,
        bus=bus,
        encoder=OpusEncoder(),
        media_dir=get_media_dir("tts_attachments"),
    )
    sink = MultiplexingTTSSink(
        mode_map=mode_map,
        streaming_sink=streaming_sink,
        attachment_sink=attachment_sink,
    )
    return TTSHook(
        chunker_factory=SentenceChunker,
        preprocessor=Preprocessor(known_emojis=emotion_mapper.known_emojis),
        emotion_mapper=emotion_mapper,
        synthesizer=synthesizer,
        sink=sink,
        barrier_timeout_seconds=float(os.getenv("TTS_BARRIER_TIMEOUT", "30")),
    )
```

Update the caller of `_build_tts_hook` (search the file for it). It will need to pass the bus that the gateway constructs. If the bus is constructed inside `gateway.run()`, you'll need to thread it back — read `gateway.py` to find the right injection point. The simplest correct fix is to expose a builder hook on `gateway.run()` that takes a `bus -> AgentHook` factory.

If that's too invasive, an acceptable alternative is to construct the bus in the launcher first, pass it to both the TTS hook builder AND the gateway. Choose whichever is smaller in diff size.

- [ ] **Step 6.5: Update the new test fixture to pass the bus**

Edit the test from Step 6.2 to construct a bus and pass it:

```python
        from nanobot.bus.queue import MessageBus
        bus = MessageBus()
        hook = _build_tts_hook(bus)
```

- [ ] **Step 6.6: Run the tests**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/channels/test_tts_sink_wiring.py tests/test_launcher.py -v`
Expected: ALL PASSED.

- [ ] **Step 6.7: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add src/nanobot_runtime/launcher.py src/nanobot_runtime/gateway.py tests/services/channels/test_tts_sink_wiring.py
git commit -m "feat(tts): wire MultiplexingTTSSink + AttachmentTTSSink in launcher"
```

---

## Task 7: Remove the "ATTACHMENT pipeline TBD" warning

**Files:**
- Modify: `src/nanobot_runtime/services/tts/modes.py:122-137`
- Modify: `tests/services/tts/test_modes.py` (drop or update the test that asserts the warning fires)

- [ ] **Step 7.1: Find the existing warning test**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && grep -n "TBD\|attachment" tests/services/tts/test_modes.py`

Note the test name(s) that check the warning behavior.

- [ ] **Step 7.2: Update modes.py**

Edit `src/nanobot_runtime/services/tts/modes.py`. Replace the `attachment_channels` warning block (lines 122-137) with:

```python
    if attachment_channels := sorted(
        name for name, mode in result.channels.items() if mode is TTSMode.ATTACHMENT
    ):
        logger.info(
            "🎙️ TTS ATTACHMENT mode active for channels: {}", attachment_channels
        )
    return result
```

(The walrus is intentional: same logic, no warning, just an info-level lifecycle marker per the CODING_RULES emoji-marker convention.)

- [ ] **Step 7.3: Update the test**

Replace the warning-expectation test with one that asserts the info log fires. If the test was using `caplog`, just change the expected level and message. If it was a structural "ATTACHMENT silently NONE" test, delete it — the assumption is no longer true.

- [ ] **Step 7.4: Run modes tests**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_modes.py -v`
Expected: ALL PASSED.

- [ ] **Step 7.5: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add src/nanobot_runtime/services/tts/modes.py tests/services/tts/test_modes.py
git commit -m "feat(tts): drop ATTACHMENT-pipeline-TBD warning; replaced with lifecycle info log"
```

---

## Task 8: Integration test — TTSHook → Multiplexer → AttachmentTTSSink → bus

**Files:**
- Create: `tests/services/tts/test_attachment_integration.py`

- [ ] **Step 8.1: Write the integration test**

Create `tests/services/tts/test_attachment_integration.py`:

```python
import asyncio
import base64
import wave
from io import BytesIO
from pathlib import Path

import pytest
from nanobot.agent.hook import AgentHookContext
from nanobot.bus.queue import MessageBus

from nanobot_runtime.services.channels.desktop_mate import LazyChannelTTSSink
from nanobot_runtime.services.hooks.tts import TTSHook
from nanobot_runtime.services.tts.attachment_sink import AttachmentTTSSink
from nanobot_runtime.services.tts.encoder import OpusEncoder
from nanobot_runtime.services.tts.modes import ChannelModeMap, TTSMode
from nanobot_runtime.services.tts.multiplex_sink import MultiplexingTTSSink


def _wav_b64() -> str:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * 1600)  # 0.1s silence
    return base64.b64encode(buf.getvalue()).decode()


class _StubChunker:
    def feed(self, delta: str) -> list[str]:
        return [delta] if delta.endswith(".") else []

    def flush(self) -> str | None:
        return None


class _StubPreprocessor:
    def process(self, sentence: str) -> tuple[str, str | None]:
        return sentence, None


class _StubEmotionMapper:
    def map(self, emotion):
        return []


class _StubSynth:
    async def synthesize(self, text: str, *, reference_id=None) -> str:
        return _wav_b64()


@pytest.mark.asyncio
async def test_full_attachment_pipeline_emits_voice_outbound(tmp_path: Path) -> None:
    """End-to-end: hook receives deltas, dispatches to multiplexer, attachment
    sink buffers, on_stream_end barrier fires, encoder produces OGG, bus has
    one OutboundMessage with .ogg media for the telegram channel."""
    import shutil
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required for end-to-end encoding")

    bus = MessageBus()
    mode_map = ChannelModeMap(
        default=TTSMode.NONE,
        channels={"telegram": TTSMode.ATTACHMENT, "desktop_mate": TTSMode.STREAMING},
    )
    streaming = LazyChannelTTSSink(mode_map=mode_map)
    attachment = AttachmentTTSSink(
        mode_map=mode_map, bus=bus, encoder=OpusEncoder(), media_dir=tmp_path
    )
    mux = MultiplexingTTSSink(
        mode_map=mode_map, streaming_sink=streaming, attachment_sink=attachment
    )
    hook = TTSHook(
        chunker_factory=_StubChunker,
        preprocessor=_StubPreprocessor(),
        emotion_mapper=_StubEmotionMapper(),
        synthesizer=_StubSynth(),
        sink=mux,
        barrier_timeout_seconds=5.0,
    )

    ctx = AgentHookContext(session_key="telegram:42")
    await hook.on_stream(ctx, "hello.")
    await hook.on_stream(ctx, "there.")
    await hook.on_stream_end(ctx, resuming=False)

    assert bus.outbound.qsize() == 1
    msg = bus.outbound.get_nowait()
    assert msg.channel == "telegram"
    assert msg.chat_id == "42"
    assert len(msg.media) == 1
    out_path = Path(msg.media[0])
    assert out_path.exists()
    assert out_path.suffix == ".ogg"
    assert out_path.read_bytes().startswith(b"OggS")
```

- [ ] **Step 8.2: Run it**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/test_attachment_integration.py -v`
Expected: 1 PASSED.

- [ ] **Step 8.3: Run the full TTS-related test suite as a regression sweep**

Run: `cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime && uv run pytest tests/services/tts/ tests/services/hooks/ tests/services/channels/ -v`
Expected: ALL PASSED.

- [ ] **Step 8.4: Commit**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git add tests/services/tts/test_attachment_integration.py
git commit -m "test(tts): end-to-end attachment pipeline integration test"
```

---

## Task 9: Live e2e on the workspace

This is a manual verification step the engineer performs once code merges. It does NOT block the PR but does block declaring the work shipped.

**Pre-reqs (NOT done in this plan — user-action items):**
- BotFather token obtained.
- `TELEGRAM_BOT_TOKEN` env var set in the yuri runtime environment.
- `yuri/nanobot.json` has the `telegram` channel block enabled (Phase 1-3 from the conversation that produced this plan).
- `yuri/resources/tts_channel_modes.yml` already has `telegram: attachment` (already in place pre-plan).

- [ ] **Step 9.1: Restart yuri runtime**

Run whatever command starts yuri in the dev environment.

Tail logs and confirm no `FileNotFoundError`, no `TTS rules YAML not found`, and the new info log `🎙️ TTS ATTACHMENT mode active for channels: ['telegram']` is present.

- [ ] **Step 9.2: Send a text message to the bot from a Telegram client (PC or mobile, both should work)**

Expected behavior:
1. Bot streams text response (existing behavior).
2. Within ~1-2 seconds of stream end, a voice-note appears in the chat with waveform UI.
3. Voice-note is mono OGG/Opus, plays back the same content as the text.

- [ ] **Step 9.3: Send a message that triggers a tool call (e.g. a memory lookup if LTM is wired)**

Expected behavior: the tool call hop does NOT produce a partial voice-note. Only the final terminal stream_end produces one voice-note covering the whole turn. (This is the `resuming=False` gate from Task 1.)

- [ ] **Step 9.4: Verify DesktopMate streaming was not regressed**

Open the DesktopMate FE, send a message → audio chunks arrive in real time as before. No double audio.

- [ ] **Step 9.5: Open the PR**

```bash
cd /home/spow12/codes/2026_upper/agents/yuri/nanobot_runtime
git push -u origin <feat-branch-name>
gh pr create --base develop --title "feat(tts): ATTACHMENT pipeline for Telegram voice-notes" --body "<summary + test plan>"
```

---

## Self-Review Checklist (run before handing off)

**Spec coverage:**
- [x] `on_session_end` notification on TTSSink (Task 1)
- [x] WAV → OGG/Opus encoder (Task 3)
- [x] Per-session buffering + flush (Task 4)
- [x] Multiplex routing by mode (Task 5)
- [x] Launcher wiring (Task 6)
- [x] Drop the TBD warning (Task 7)
- [x] Integration test (Task 8)
- [x] Live e2e (Task 9)

**Type consistency:**
- `TTSChunk.session_key: str | None` (Task 4.3) — used by `AttachmentTTSSink.send_tts_chunk` (Task 4.4) and `MultiplexingTTSSink.send_tts_chunk` (Task 5.3). Consistent.
- `VoiceEncoder` ABC + `OpusEncoder` concrete (Task 3.3) — referenced as `VoiceEncoder` in `AttachmentTTSSink.__init__` (Task 4.4), in test fakes that inherit from `VoiceEncoder` (Task 4.1), and in integration test usage (Task 8.1). Consistent.
- `AttachmentTTSSink.__init__(*, mode_map, bus, encoder, media_dir)` (Task 4.4) — matches launcher construction (Task 6.4) and integration test fixture (Task 8.1). Consistent.

**ABC unification (per user request):**
- ✅ `TTSSink` ABC — `is_enabled`, `send_tts_chunk`, `on_session_end` are ALL `@abstractmethod`. Concrete subclasses (`LazyChannelTTSSink`, `AttachmentTTSSink`, `MultiplexingTTSSink`) inherit and explicitly implement each.
- ✅ `VoiceEncoder` ABC — `encode_wav_chunks` is `@abstractmethod`. Concrete (`OpusEncoder`) and test fakes (`_FakeEncoder`) both inherit.
- ⚠️ Existing `SentenceChunker` / `TextPreprocessor` / `EmotionMapper` / `TTSSynthesizer` remain `typing.Protocol` — out of scope for this PR. Conversion requires touching `IrodoriClient`, `Preprocessor`, `EmotionMapper`, `SentenceChunker` simultaneously plus all their tests. Tracked as a follow-up refactor.

**Placeholder scan:** None — all code blocks are complete; no "TODO" / "TBD" / "implement later" tokens in the plan body.

**Risks / open questions for the engineer:**
1. **Bus injection in launcher (Task 6.4)** — the smallest correct fix depends on how `gateway.run()` is structured. Read `gateway.py` first; if the bus is created inside `run()`, the cleanest path is to create it in the launcher and pass it down. Don't refactor more than needed.
2. **`get_media_dir` import path** — verify it's `nanobot.config.paths.get_media_dir`. It was referenced in `desktop_mate.py:151` so the path is known to exist.
3. **`AgentHookContext` constructor in tests** — verify the hook context is constructed with `session_key=` (kwarg). If nanobot's hook context is positional or uses a different field name, adjust the test stubs accordingly.
