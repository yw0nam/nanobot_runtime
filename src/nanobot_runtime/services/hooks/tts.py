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
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from loguru import logger
from nanobot.agent.hook import AgentHook, AgentHookContext
from pydantic import BaseModel, ConfigDict, Field


# ── Data shape emitted to the sink (matches DMP's tts_chunk frame payload)


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


# ── Injected dependencies (Protocols) ────────────────────────────────────


class SentenceChunker(Protocol):
    def feed(self, delta: str) -> list[str]: ...
    def flush(self) -> str | None: ...


class TextPreprocessor(Protocol):
    def process(self, sentence: str) -> tuple[str, str | None]:
        """Return (clean_text_for_display, emotion_tag_or_None)."""


class EmotionMapper(Protocol):
    def map(self, emotion: str | None) -> list[dict[str, Any]]: ...


class TTSSynthesizer(Protocol):
    async def synthesize(self, text: str, *, reference_id: str | None = None) -> str | None:
        """Return base64-encoded audio (wav) or None on failure.

        ``reference_id`` is an optional per-call voice override. ``None``
        means "use the synthesizer's default"; an empty string forces no
        reference even when the synthesizer has a baked-in default.
        """


# Resolves a session_key (``"desktop_mate:<chat_id>"``-style) to the voice
# to use for that session, or ``None`` to fall back to the synthesizer's
# default. The hook calls this *once per sentence dispatch*, so the resolver
# may consult mutable channel state without stale-cache concerns.
ReferenceIdResolver = Callable[[str | None], str | None]


class TTSSink(ABC):
    """Contract for downstream consumers of synthesized TTS chunks.

    Promoted from a `Protocol` with an optional `is_enabled` to an ABC so
    every sink — production (`LazyChannelTTSSink`), regression harness
    (`DirectSink`), and test fakes — implements the same contract. The
    hook can then call `self._sink.is_enabled(session_key)` directly,
    with no `getattr` introspection and no implicit "always enabled"
    fallback. A sink that forgets either method fails at construction
    time with a clear `TypeError`, not at the dispatch hot path with an
    `AttributeError`.
    """

    @abstractmethod
    async def send_tts_chunk(self, chunk: TTSChunk) -> None: ...

    @abstractmethod
    def is_enabled(self, session_key: str | None) -> bool:
        """Return True iff this sink is willing to deliver audio for the
        given session. The hook calls this once at dispatch time and once
        again inside the synth task (second-chance check), both with the
        same ``state.session_key``. Sinks that don't care about the
        channel implement this trivially (return True or check internal
        state). ``session_key`` is required positionally — there is no
        default; pass ``None`` explicitly when no key is available.
        """


# ── Per-session state ─────────────────────────────────────────────────────


@dataclass(slots=True)
class _SessionState:
    chunker: SentenceChunker
    sequence: int = 0
    pending: list[asyncio.Task[None]] = field(default_factory=list)
    # Cached so ``_dispatch_sentence`` can call the resolver without
    # threading the AgentHookContext through every helper.
    session_key: str | None = None


_FALLBACK_SESSION_KEY = "__tts_hook_default__"


# ── Hook ─────────────────────────────────────────────────────────────────


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
        reference_id_resolver: ReferenceIdResolver | None = None,
    ) -> None:
        super().__init__()
        self._chunker_factory = chunker_factory
        self._preprocessor = preprocessor
        self._emotion_mapper = emotion_mapper
        self._synthesizer = synthesizer
        self._sink = sink
        self._barrier_timeout = barrier_timeout_seconds
        self._reference_id_resolver = reference_id_resolver
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

    # ── Internals ─────────────────────────────────────────────────────

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
            state = _SessionState(chunker=self._chunker_factory(), session_key=key)
            self._states[key] = state
        return state

    def _resolve_reference_id(self, session_key: str | None) -> str | None:
        if self._reference_id_resolver is None:
            return None
        try:
            return self._reference_id_resolver(session_key)
        except Exception:
            # Resolver failure must not block synthesis — fall back to the
            # synthesizer's constructor default.
            logger.exception("TTSHook reference_id resolver raised (session={})", session_key)
            return None

    def _dispatch_sentence(self, state: _SessionState, sentence: str) -> None:
        text, emotion = self._preprocessor.process(sentence)
        if not text or not any(ch.isalnum() for ch in text):
            return
        if not self._sink.is_enabled(state.session_key):
            return
        # Resolve voice up-front so a per-tick state change in the channel
        # (e.g. user re-sends with a different reference_id mid-stream) does
        # not split the same sentence across two voices.
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
        if not self._sink.is_enabled(session_key):
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
