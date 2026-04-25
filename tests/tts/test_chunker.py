"""Tests for SentenceChunker — wraps fast_bunkai to split streaming deltas
into sentence-terminated chunks, filters <think> reasoning blocks, and
yields a remainder on flush().
"""
from __future__ import annotations

from nanobot_runtime.tts.chunker import SentenceChunker


def test_single_sentence_delta_yields_sentence() -> None:
    c = SentenceChunker(min_chunk_length=0)
    out = c.feed("Hello world.")
    assert out == ["Hello world."]


def test_multi_sentence_in_one_delta_yields_all_in_order() -> None:
    c = SentenceChunker(min_chunk_length=0)
    out = c.feed("First sentence. Second sentence! Third?")
    # All three should come out (last may arrive on subsequent feed or flush)
    # fast_bunkai may hold the last one until the next feed — check at least
    # the first two emerge in order.
    assert out[0] == "First sentence."
    assert out[1] == "Second sentence!"


def test_split_delta_across_two_feeds_yields_complete_sentence() -> None:
    c = SentenceChunker(min_chunk_length=0)
    first = c.feed("Hello ")
    second = c.feed("world.")
    assert first == []
    assert second == ["Hello world."]


def test_flush_returns_remainder_without_terminator() -> None:
    c = SentenceChunker(min_chunk_length=0)
    out = c.feed("No terminator here")
    assert out == []
    remainder = c.flush()
    assert remainder == "No terminator here"


def test_flush_returns_none_when_empty() -> None:
    c = SentenceChunker(min_chunk_length=0)
    assert c.flush() is None


def test_empty_and_whitespace_deltas_yield_nothing() -> None:
    c = SentenceChunker(min_chunk_length=0)
    assert c.feed("") == []
    assert c.feed("   ") == []


def test_think_block_is_dropped() -> None:
    c = SentenceChunker(min_chunk_length=0)
    out = c.feed("<think>internal reasoning.</think>Visible answer.")
    assert out == ["Visible answer."]


def test_think_block_split_across_deltas_is_dropped() -> None:
    c = SentenceChunker(min_chunk_length=0)
    a = c.feed("<think>hidden ")
    b = c.feed("thought.</think>Real answer.")
    assert a == []
    assert b == ["Real answer."]


def test_min_chunk_length_respected() -> None:
    # Short sentences below min_chunk_length are buffered until combined
    # length exceeds threshold.
    c = SentenceChunker(min_chunk_length=30)
    out = c.feed("Hi.")
    assert out == []
    # Force flush to verify the short sentence wasn't lost.
    remainder = c.flush()
    assert remainder is not None and "Hi." in remainder


def test_default_min_chunk_length_is_50() -> None:
    c = SentenceChunker()
    # A 10-char sentence should NOT emit at default min_chunk_length=50.
    assert c.feed("Hello.") == []


def test_while_true_loop_breaks_when_no_real_position_found() -> None:
    # Regression: the while True loop must break and not spin when find_eos
    # returns positions that all fail the _SENTENCE_ENDERS filter. A buffer
    # ending with whitespace after a letter is a natural case where the EOS
    # detector may report a position but the stripped prefix does not end with
    # a sentence-ender character, so real_positions ends up empty.
    c = SentenceChunker(min_chunk_length=0)
    # Feed text with no sentence-ending character — loop must exit without
    # hanging and leave the text in the buffer for flush().
    result = c.feed("No terminator, just a trailing space ")
    assert result == []
    remainder = c.flush()
    assert remainder is not None and "No terminator" in remainder
