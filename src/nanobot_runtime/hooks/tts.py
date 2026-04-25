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

Per-session state (since 2026-04-22)
------------------------------------
nanobot's AgentLoop runs up to ``NANOBOT_MAX_CONCURRENT_REQUESTS`` turns in
parallel and shares a single ``_extra_hooks`` list across all sessions/
channels, so one TTSHook instance sees deltas from every concurrent turn
interleaved. The hook therefore keeps a **per-session** chunker/sequence/
pending-tasks state bundle keyed on ``AgentHookContext.session_key``
(added upstream in the nanobot fork). Turns on non-desktop channels (e.g.
the idle-watcher firing through ``channel=cli``) still drive ``on_stream``,
but the sink's ``is_enabled()`` gate returns ``False`` for them, so their
sentences never consume a sequence number. When ``session_key`` is ``None``
(caller didn't supply one — stale nanobot, or a test) the hook falls back
to a shared ``_default`` state bucket and emits a warning once.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

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
# Per-session state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SessionState:
    chunker: SentenceChunker
    sequence: int = 0
    pending: list[asyncio.Task[None]] = field(default_factory=list)


_FALLBACK_SESSION_KEY = "__tts_hook_default__"


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


class TTSHook(AgentHook):
    def __init__(
        self,
        *,
        chunker_factory: Callable[[], SentenceChunker],
        preprocessor: TextPreprocessor,
        emotion_mapper: EmotionMapper,
        synthesizer: TTSSynthesizer,
        sink: TTSSink,
        barrier_timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__()
        self._chunker_factory = chunker_factory
        self._preprocessor = preprocessor
        self._emotion_mapper = emotion_mapper
        self._synthesizer = synthesizer
        self._sink = sink
        self._barrier_timeout = barrier_timeout_seconds
        self._states: dict[str, _SessionState] = {}
        self._fallback_warned = False

    def wants_streaming(self) -> bool:
        return True

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        if not delta:
            return
        state = self._state_for(context)
        for sentence in state.chunker.feed(delta):
            self._dispatch_sentence(state, sentence)

    async def on_stream_end(
        self, context: AgentHookContext, *, resuming: bool
    ) -> None:
        # Mid-turn break (tool call imminent) — do not flush. Partial buffer
        # must carry across the tool loop so a single logical response does
        # not get split into separate TTS groups.
        if resuming:
            return

        key = self._session_key(context)
        state = self._states.get(key)
        if state is None:
            # Turn produced no deltas for this session — nothing to drain.
            return

        remainder = state.chunker.flush()
        if remainder:
            self._dispatch_sentence(state, remainder)

        # TTS Barrier: block until every pending synth task settles, so the
        # caller (channel) can emit stream_end immediately after this returns.
        if state.pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*state.pending, return_exceptions=True),
                    timeout=self._barrier_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "TTS Barrier timeout ({}s) — {} tasks still pending; "
                    "cancelling. (session={})",
                    self._barrier_timeout,
                    sum(1 for t in state.pending if not t.done()),
                    key,
                )
                for t in state.pending:
                    if not t.done():
                        t.cancel()

        # Turn fully wrapped — drop the per-session bucket so the next turn
        # for the same session starts clean (sequence back to 0, fresh chunker).
        self._states.pop(key, None)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _session_key(self, context: AgentHookContext) -> str:
        key = getattr(context, "session_key", None)
        if key is None:
            if not self._fallback_warned:
                logger.warning(
                    "TTSHook: AgentHookContext.session_key is None — falling "
                    "back to a shared default bucket. Update the nanobot "
                    "dependency so concurrent turns don't share TTS state."
                )
                self._fallback_warned = True
            return _FALLBACK_SESSION_KEY
        return key

    def _state_for(self, context: AgentHookContext) -> _SessionState:
        key = self._session_key(context)
        state = self._states.get(key)
        if state is None:
            state = _SessionState(chunker=self._chunker_factory())
            self._states[key] = state
        return state

    def _sink_is_enabled(self) -> bool:
        fn = getattr(self._sink, "is_enabled", None)
        return not callable(fn) or fn()

    def _dispatch_sentence(self, state: _SessionState, sentence: str) -> None:
        text, emotion = self._preprocessor.process(sentence)
        if not text or not any(ch.isalnum() for ch in text):
            return
        # Sinks without is_enabled() are treated as always-enabled.
        # We drop silently — no task, no synthesis, no sequence bump.
        if not self._sink_is_enabled():
            return
        sequence = state.sequence
        state.sequence += 1
        task = asyncio.create_task(self._synth_and_emit(text, emotion, sequence))
        state.pending.append(task)

    async def _synth_and_emit(
        self, text: str, emotion: str | None, sequence: int
    ) -> None:
        # Second-chance check: sink's enabled state can change between task
        # scheduled and task running (e.g. channel registers the stream as
        # off after dispatch). Re-checking avoids a wasted synthesize() call.
        if not self._sink_is_enabled():
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
