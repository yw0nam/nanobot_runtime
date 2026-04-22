"""TTSHook — streams LLM deltas into a sentence-boundary TTS pipeline.

The hook is the nanobot analogue of DMP's `event_handlers._process_token_event`.
It buffers streaming deltas, detects sentence boundaries via an injected
chunker, extracts an emotion tag, runs TTS synthesis asynchronously, and
emits one `TTSChunk` per completed sentence through a caller-provided sink.

On `on_stream_end(resuming=False)` it (a) flushes any trailing text that
never hit a terminator and (b) awaits all in-flight synthesis tasks — this
is the **TTS Barrier** that lets the channel emit `stream_end` knowing no
further `tts_chunk` frames will arrive. `resuming=True` (tool-call break
inside a turn) does NOT flush — partial buffers continue across the tool
call, matching DMP's single-turn semantics.

Dependencies are injected as protocols so the hook is unit-testable without
real TTS infra. Real implementations (fast_bunkai chunker, IrodoriTTS
synth, emotion-motion YAML map) are provided by separate modules.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger
from nanobot.agent.hook import AgentHook, AgentHookContext


# ---------------------------------------------------------------------------
# Data shape emitted to the sink (matches DMP's tts_chunk frame payload)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TTSChunk:
    sequence: int
    text: str
    audio_base64: str | None
    emotion: str | None
    keyframes: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Injected dependencies (Protocols)
# ---------------------------------------------------------------------------


class SentenceChunker(Protocol):
    def feed(self, delta: str) -> list[str]: ...
    def flush(self) -> str | None: ...


class TextPreprocessor(Protocol):
    def process(self, sentence: str) -> tuple[str, str | None]:
        """Return (clean_text_for_display, emotion_tag_or_None)."""


class EmotionMapper(Protocol):
    def map(self, emotion: str | None) -> list[dict[str, Any]]: ...


class TTSSynthesizer(Protocol):
    async def synthesize(self, text: str) -> str | None:
        """Return base64-encoded audio (wav) or None on failure."""


class TTSSink(Protocol):
    async def send_tts_chunk(self, chunk: TTSChunk) -> None: ...

    # Optional. When present and returning False, the hook skips synthesis
    # entirely for this sentence — saving GPU / network traffic for clients
    # that can't play audio. Sinks that don't implement this method are
    # treated as "always enabled" for backward compatibility.
    # def is_enabled(self) -> bool: ...


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


class TTSHook(AgentHook):
    def __init__(
        self,
        *,
        chunker: SentenceChunker,
        preprocessor: TextPreprocessor,
        emotion_mapper: EmotionMapper,
        synthesizer: TTSSynthesizer,
        sink: TTSSink,
        barrier_timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__()
        self._chunker = chunker
        self._preprocessor = preprocessor
        self._emotion_mapper = emotion_mapper
        self._synthesizer = synthesizer
        self._sink = sink
        self._barrier_timeout = barrier_timeout_seconds
        self._sequence = 0
        self._pending: list[asyncio.Task[None]] = []

    def wants_streaming(self) -> bool:
        return True

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        if not delta:
            return
        # First delta of a new turn: reset sequence counter.
        if context.iteration == 0 and self._sequence and not self._pending:
            self._sequence = 0
        for sentence in self._chunker.feed(delta):
            self._dispatch_sentence(sentence)

    async def on_stream_end(
        self, context: AgentHookContext, *, resuming: bool
    ) -> None:
        # Mid-turn break (tool call imminent) — do not flush. Partial buffer
        # must carry across the tool loop so a single logical response does
        # not get split into separate TTS groups.
        if resuming:
            return

        remainder = self._chunker.flush()
        if remainder:
            self._dispatch_sentence(remainder)

        # TTS Barrier: block until every pending synth task settles, so the
        # caller (channel) can emit stream_end immediately after this returns.
        if self._pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._pending, return_exceptions=True),
                    timeout=self._barrier_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "TTS Barrier timeout ({}s) — {} tasks still pending; cancelling.",
                    self._barrier_timeout,
                    sum(1 for t in self._pending if not t.done()),
                )
                for t in self._pending:
                    if not t.done():
                        t.cancel()
            self._pending.clear()

        self._sequence = 0

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _dispatch_sentence(self, sentence: str) -> None:
        text, emotion = self._preprocessor.process(sentence)
        if not text or not any(ch.isalnum() for ch in text):
            return
        # Honour the sink's opt-in TTS-off signal. Sinks without this method
        # (e.g. older test fakes) are treated as always-enabled. We drop the
        # sentence silently — no task created, no synthesis, no sequence bump.
        is_enabled = getattr(self._sink, "is_enabled", None)
        if callable(is_enabled) and not is_enabled():
            return
        sequence = self._sequence
        self._sequence += 1
        task = asyncio.create_task(self._synth_and_emit(text, emotion, sequence))
        self._pending.append(task)

    async def _synth_and_emit(
        self, text: str, emotion: str | None, sequence: int
    ) -> None:
        # Second-chance check: _dispatch_sentence already gated, but the
        # sink's enabled state can change between "task scheduled" and
        # "task running" — for instance when the channel hasn't yet
        # registered the stream at dispatch time, then learns it's off.
        # Re-checking here ensures we never call synthesize() for a
        # stream that's known-disabled by the time we'd do the work.
        is_enabled = getattr(self._sink, "is_enabled", None)
        if callable(is_enabled) and not is_enabled():
            return
        try:
            audio_b64 = await self._synthesizer.synthesize(text)
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
