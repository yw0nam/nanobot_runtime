"""TTS hook package — re-exports for backward-compatible import paths."""

from nanobot_runtime.services.hooks.tts.abc import TTSSink
from nanobot_runtime.services.hooks.tts.hook import TTSHook, _FALLBACK_SESSION_KEY
from nanobot_runtime.services.hooks.tts.models import TTSChunk
from nanobot_runtime.services.hooks.tts.protocols import (
    EmotionMapper,
    ReferenceIdResolver,
    SentenceChunker,
    TextPreprocessor,
    TTSSynthesizer,
)

__all__ = [
    "TTSChunk",
    "TTSSink",
    "TTSHook",
    "_FALLBACK_SESSION_KEY",
    "SentenceChunker",
    "TextPreprocessor",
    "EmotionMapper",
    "TTSSynthesizer",
    "ReferenceIdResolver",
]
