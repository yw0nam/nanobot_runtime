"""Tests for Preprocessor — strip stage directions / meta brackets,
normalize whitespace, and extract first known emotion emoji as tag.
"""
from __future__ import annotations

from nanobot_runtime.tts.preprocessor import Preprocessor


def test_plain_text_no_emoji_returns_text_and_none() -> None:
    p = Preprocessor()
    text, emotion = p.process("Hello there.")
    assert text == "Hello there."
    assert emotion is None


def test_sentence_with_known_emoji_extracts_first_match() -> None:
    p = Preprocessor()
    text, emotion = p.process("😊 I am happy to see you.")
    assert emotion == "😊"
    # Emoji stays in the text (DMP behavior — Irodori uses it), whitespace normalized.
    assert "I am happy to see you." in text


def test_action_stars_stripped() -> None:
    p = Preprocessor()
    text, _ = p.process("Hello *waves hand* friend.")
    assert "*waves hand*" not in text
    assert "Hello" in text and "friend." in text


def test_meta_brackets_stripped() -> None:
    p = Preprocessor()
    text, _ = p.process("Hi [note: whisper] there.")
    assert "[note: whisper]" not in text
    assert "Hi" in text and "there." in text


def test_whitespace_normalized() -> None:
    p = Preprocessor()
    text, _ = p.process("Hello     world.\n\nHow    are you?")
    # Multiple spaces collapsed to single.
    assert "  " not in text


def test_empty_input_returns_empty_and_none() -> None:
    p = Preprocessor()
    text, emotion = p.process("")
    assert text == ""
    assert emotion is None


def test_first_emoji_in_text_wins_over_later() -> None:
    p = Preprocessor()
    # 😊 appears before 😭 — first one wins.
    text, emotion = p.process("😊 happy then 😭 sad")
    assert emotion == "😊"


def test_unknown_emoji_not_treated_as_tag() -> None:
    p = Preprocessor()
    text, emotion = p.process("Hello 🦄 unicorn.")
    # Unknown emoji → no tag.
    assert emotion is None
