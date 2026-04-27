"""Injected-dependency protocols for the TTS pipeline."""

from typing import Any, Callable, Protocol


class SentenceChunker(Protocol):
    """Protocol for streaming sentence boundary detectors."""

    def feed(self, delta: str) -> list[str]: ...
    def flush(self) -> str | None: ...


class TextPreprocessor(Protocol):
    """Protocol for text cleanup and emotion-tag extraction."""

    def process(self, sentence: str) -> tuple[str, str | None]:
        """Return (clean_text_for_display, emotion_tag_or_None)."""


class EmotionMapper(Protocol):
    """Protocol for mapping emotion tags to animation keyframes."""

    def map(self, emotion: str | None) -> list[dict[str, Any]]: ...


class TTSSynthesizer(Protocol):
    """Protocol for async TTS synthesis backends."""

    async def synthesize(
        self, text: str, *, reference_id: str | None = None
    ) -> str | None:
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
