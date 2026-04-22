"""Tests for TTSHook — buffers streaming deltas, detects sentence
boundaries, synthesizes audio per boundary, emits tts_chunk events via an
injected sink, and on stream_end blocks until all in-flight syntheses
complete (TTS Barrier semantic)."""
from __future__ import annotations

import asyncio
from typing import Any

from nanobot.agent.hook import AgentHookContext

from nanobot_runtime.hooks.tts import TTSChunk, TTSHook


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeChunker:
    """Simple sentence splitter on '.', '!', '?', '\\n' for deterministic tests."""

    _ENDERS = set(".!?\n")

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        cursor = 0
        for i, ch in enumerate(self._buf):
            if ch in self._ENDERS:
                segment = self._buf[cursor : i + 1].strip()
                if segment:
                    out.append(segment)
                cursor = i + 1
        self._buf = self._buf[cursor:]
        return out

    def flush(self) -> str | None:
        remainder = self._buf.strip()
        self._buf = ""
        return remainder or None


class _FakePreprocessor:
    """Strips leading '(emotion) ' tag; returns (clean_text, emotion | None)."""

    def process(self, sentence: str) -> tuple[str, str | None]:
        text = sentence.strip()
        if text.startswith("(") and ")" in text:
            end = text.index(")")
            emotion = text[1:end].strip() or None
            clean = text[end + 1 :].strip()
            return clean, emotion
        return text, None


class _FakeEmotionMapper:
    """Returns a stub keyframe list keyed by emotion for assertion."""

    def map(self, emotion: str | None) -> list[dict[str, Any]]:
        if emotion is None:
            return [{"duration": 0.3, "targets": {"neutral": 1.0}}]
        return [{"duration": 0.3, "targets": {emotion: 1.0}}]


class _FakeSynthesizer:
    """Records calls; optionally sleeps to simulate synth latency."""

    def __init__(self, latency: float = 0.0, fail_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._latency = latency
        self._fail_on = fail_on or set()

    async def synthesize(self, text: str) -> str | None:
        if self._latency:
            await asyncio.sleep(self._latency)
        self.calls.append(text)
        if text in self._fail_on:
            raise RuntimeError(f"TTS synth failed for: {text}")
        return f"b64::{text}"


class _FakeSink:
    """Captures emitted TTSChunks in order."""

    def __init__(self) -> None:
        self.chunks: list[TTSChunk] = []

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        self.chunks.append(chunk)


def _make_hook(
    *,
    latency: float = 0.0,
    fail_on: set[str] | None = None,
) -> tuple[TTSHook, _FakeSink, _FakeSynthesizer]:
    sink = _FakeSink()
    synth = _FakeSynthesizer(latency=latency, fail_on=fail_on)
    hook = TTSHook(
        chunker_factory=_FakeChunker,
        preprocessor=_FakePreprocessor(),
        emotion_mapper=_FakeEmotionMapper(),
        synthesizer=synth,
        sink=sink,
    )
    return hook, sink, synth


def _ctx(iteration: int = 0, session_key: str = "test-session") -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration, messages=[], session_key=session_key
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


async def test_wants_streaming_is_true() -> None:
    hook, _, _ = _make_hook()
    assert hook.wants_streaming() is True


async def test_delta_without_boundary_does_not_synthesize() -> None:
    hook, sink, synth = _make_hook()
    ctx = _ctx()
    await hook.on_stream(ctx, "Hello there, I am")
    assert synth.calls == []
    assert sink.chunks == []


async def test_sentence_boundary_triggers_synthesis_and_emission() -> None:
    hook, sink, synth = _make_hook()
    ctx = _ctx()
    await hook.on_stream(ctx, "Hello there.")
    # Hook dispatches synth as a background task; drain pending tasks.
    await hook.on_stream_end(ctx, resuming=False)

    assert synth.calls == ["Hello there."]
    assert len(sink.chunks) == 1
    chunk = sink.chunks[0]
    assert chunk.sequence == 0
    assert chunk.text == "Hello there."
    assert chunk.audio_base64 == "b64::Hello there."
    assert chunk.emotion is None
    assert chunk.keyframes == [{"duration": 0.3, "targets": {"neutral": 1.0}}]


async def test_multiple_sentences_get_sequenced() -> None:
    hook, sink, _ = _make_hook()
    ctx = _ctx()
    await hook.on_stream(ctx, "First sentence. Second one! Third?")
    await hook.on_stream_end(ctx, resuming=False)

    assert [c.text for c in sink.chunks] == [
        "First sentence.",
        "Second one!",
        "Third?",
    ]
    assert [c.sequence for c in sink.chunks] == [0, 1, 2]


async def test_emotion_tag_is_extracted_and_mapped() -> None:
    hook, sink, synth = _make_hook()
    ctx = _ctx()
    await hook.on_stream(ctx, "(joyful) I am so happy.")
    await hook.on_stream_end(ctx, resuming=False)

    assert len(sink.chunks) == 1
    c = sink.chunks[0]
    assert c.text == "I am so happy."
    assert c.emotion == "joyful"
    assert c.keyframes == [{"duration": 0.3, "targets": {"joyful": 1.0}}]
    # Synthesizer receives the cleaned text (emoji/tag already extracted)
    assert synth.calls == ["I am so happy."]


async def test_empty_or_whitespace_sentences_are_dropped() -> None:
    hook, sink, synth = _make_hook()
    ctx = _ctx()
    # Consecutive enders with nothing between → nothing to synthesize
    await hook.on_stream(ctx, "...!?")
    await hook.on_stream_end(ctx, resuming=False)

    assert synth.calls == []
    assert sink.chunks == []


async def test_flush_on_stream_end_synthesizes_buffered_remainder() -> None:
    """Trailing text without a terminator must still reach TTS on stream_end."""
    hook, sink, synth = _make_hook()
    ctx = _ctx()
    await hook.on_stream(ctx, "First sentence. Trailing text without period")
    await hook.on_stream_end(ctx, resuming=False)

    assert synth.calls == ["First sentence.", "Trailing text without period"]
    assert [c.text for c in sink.chunks] == [
        "First sentence.",
        "Trailing text without period",
    ]


async def test_stream_end_waits_for_synthesis_to_complete_before_returning() -> None:
    """TTS Barrier: on_stream_end(resuming=False) must block until all
    pending syntheses are done, so DesktopMateChannel can safely emit
    stream_end immediately after the hook returns."""
    hook, sink, synth = _make_hook(latency=0.05)
    ctx = _ctx()
    await hook.on_stream(ctx, "One. Two. Three.")
    # sink may still be empty here — tasks are in flight.
    await hook.on_stream_end(ctx, resuming=False)
    # By the time on_stream_end returns, every synth must have emitted.
    assert len(sink.chunks) == 3


async def test_stream_end_resuming_does_not_block_or_flush() -> None:
    """resuming=True signals tool-call follow-up; don't flush mid-turn
    or we'd split a single logical response into multiple TTS groups."""
    hook, sink, synth = _make_hook()
    ctx = _ctx()
    await hook.on_stream(ctx, "Half sentence without end")
    await hook.on_stream_end(ctx, resuming=True)

    assert synth.calls == []  # no flush mid-turn
    assert sink.chunks == []


async def test_stream_end_resuming_preserves_buffer_across_tool_loop() -> None:
    hook, sink, synth = _make_hook()
    ctx = _ctx()
    await hook.on_stream(ctx, "Pre-tool partial ")
    await hook.on_stream_end(ctx, resuming=True)

    # Tool call happens, then stream resumes with completion
    await hook.on_stream(ctx, "and continuation. Done!")
    await hook.on_stream_end(ctx, resuming=False)

    # The partial from before the tool call merges with the continuation
    assert "Pre-tool partial and continuation." in synth.calls
    assert "Done!" in synth.calls


async def test_synth_failure_does_not_block_other_chunks() -> None:
    """One synth failure must not stop subsequent sentences from reaching FE."""
    hook, sink, synth = _make_hook(fail_on={"Second."})
    ctx = _ctx()
    await hook.on_stream(ctx, "First. Second. Third.")
    await hook.on_stream_end(ctx, resuming=False)

    # First and Third should emit; Second's failure is swallowed (audio=None)
    texts = [c.text for c in sink.chunks]
    assert "First." in texts
    assert "Third." in texts
    # Failed chunk still emits with audio_base64=None (FE can fall back to silence)
    second = next((c for c in sink.chunks if c.text == "Second."), None)
    assert second is not None
    assert second.audio_base64 is None


async def test_sequence_resets_per_hook_instance_across_turns() -> None:
    """A new turn (next user message) should restart sequence at 0.

    Nanobot drives the hook per iteration. We reset on the first on_stream
    of a new turn — detected when iteration is 0 and buffer is empty.
    """
    hook, sink, _ = _make_hook()
    ctx1 = _ctx(iteration=0)
    await hook.on_stream(ctx1, "Turn one. Done.")
    await hook.on_stream_end(ctx1, resuming=False)

    ctx2 = _ctx(iteration=0)
    await hook.on_stream(ctx2, "Turn two. Done.")
    await hook.on_stream_end(ctx2, resuming=False)

    first_turn = [c for c in sink.chunks if "Turn one" in c.text or c.text == "Done."][:2]
    second_turn_chunks = sink.chunks[len(first_turn) :]

    # Second turn's sequence starts at 0 again
    assert second_turn_chunks[0].sequence == 0


# --------------------------------------------------------------------------
# TTSSink.is_enabled — synthesis-skip path
# --------------------------------------------------------------------------


class _FakeSinkWithEnableFlag:
    """Sink that also advertises an enable state, mimicking the real channel."""

    def __init__(self, enabled: bool = True) -> None:
        self.chunks: list[TTSChunk] = []
        self._enabled = enabled
        self.is_enabled_calls = 0

    def is_enabled(self) -> bool:
        self.is_enabled_calls += 1
        return self._enabled

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        self.chunks.append(chunk)


async def test_is_enabled_false_skips_synthesis_entirely() -> None:
    """When sink reports TTS disabled, the hook must not call the
    synthesizer. This saves real GPU/network work for clients that
    cannot play audio anyway.
    """
    sink = _FakeSinkWithEnableFlag(enabled=False)
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

    assert synth.calls == [], f"synthesizer must not be called when disabled; got {synth.calls}"
    assert sink.chunks == [], f"no chunk should have been emitted; got {sink.chunks}"


async def test_is_enabled_true_keeps_default_behaviour() -> None:
    sink = _FakeSinkWithEnableFlag(enabled=True)
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


async def test_sink_without_is_enabled_defaults_to_enabled() -> None:
    """Backward compat: sinks that don't expose is_enabled must still work
    (the original TTSSink protocol had no such method)."""
    hook, sink, synth = _make_hook()
    assert not hasattr(sink, "is_enabled")

    ctx = _ctx()
    await hook.on_stream(ctx, "Hello there.")
    await hook.on_stream_end(ctx, resuming=False)

    assert synth.calls == ["Hello there."]
    assert len(sink.chunks) == 1
