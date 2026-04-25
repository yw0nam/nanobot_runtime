"""Preprocessor — clean sentence text and extract emotion-emoji tag.

Ported from DMP's ``TTSTextProcessor`` (src/services/agent_service/utils/
text_processor.py). Implements the ``TextPreprocessor`` Protocol declared
in ``nanobot_runtime.hooks.tts``.

Given a sentence, returns ``(clean_text, emotion_tag_or_None)``:
    * emotion_tag: first known emoji found in the sentence (by position),
      or None when no known emoji is present.
    * clean_text: with ``*action*`` and ``[meta]`` patterns removed and
      whitespace collapsed. The emoji is left in the text — DMP's Irodori
      TTS server consumes it for emotion control.
"""

import re

from nanobot_runtime.hooks.tts import TextPreprocessor as _TextPreprocessorBase

_DEFAULT_EMOJI_SET: frozenset[str] = frozenset(
    [
        "😊",
        "😭",
        "😠",
        "😮",
        "😪",
        "🤭",
        "😰",
        "😆",
        "😱",
        "😟",
        "😌",
        "🤔",
        "😲",
        "😖",
        "🥺",
        "😏",
        "🫶",
        "😒",
        "🥵",
    ]
)


class Preprocessor(_TextPreprocessorBase):
    """Clean TTS-bound sentence text and extract first known emotion emoji."""

    def __init__(self, known_emojis: frozenset[str] | None = None) -> None:
        self.known_emojis = known_emojis if known_emojis is not None else _DEFAULT_EMOJI_SET
        self._cleanup_patterns: list[re.Pattern[str]] = [
            re.compile(r"\*[^*]*\*"),
            re.compile(r"\[[^\]]*\]"),
        ]

    def process(self, sentence: str) -> tuple[str, str | None]:
        if not sentence or not sentence.strip():
            return ("", None)

        emotion_tag: str | None = None
        if self.known_emojis:
            first_pos = len(sentence)
            for emoji in self.known_emojis:
                pos = sentence.find(emoji)
                if pos != -1 and pos < first_pos:
                    first_pos = pos
                    emotion_tag = emoji

        cleaned = sentence
        for pattern in self._cleanup_patterns:
            cleaned = pattern.sub("", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return (cleaned, emotion_tag)
