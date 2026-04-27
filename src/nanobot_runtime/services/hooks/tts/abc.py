"""TTSSink — abstract base class for downstream TTS chunk consumers."""

from abc import ABC, abstractmethod

from nanobot_runtime.services.hooks.tts.models import TTSChunk


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
