"""SentenceChunker — streaming delta → sentence-boundary splitter.

Ported from DMP's ``TextChunkProcessor`` (src/services/agent_service/utils/
text_chunker.py) combined with the websocket_service wrapper that
returns ``flush()`` as a single joined remainder string.

Uses fast_bunkai's ``FastBunkai.find_eos`` to detect real sentence
terminators, filters ``<think>...</think>`` reasoning blocks out of the
stream, and supports a ``min_chunk_length`` to coalesce very short
sentences.
"""

from __future__ import annotations

import re

from fast_bunkai import FastBunkai
from loguru import logger


class SentenceChunker:
    """Buffer streaming deltas and yield sentence-terminated chunks.

    Methods:
        feed(delta): consume a streaming delta; return zero or more
            complete sentence strings.
        flush(): return any remaining buffered text (no terminator) as a
            single string, or None if the buffer is empty. Resets state.
    """

    _SENTENCE_ENDERS = frozenset("。！？.!?\n")

    def __init__(
        self,
        *,
        reasoning_start_tag: str = "<think>",
        reasoning_end_tag: str = "</think>",
        min_chunk_length: int = 50,
    ) -> None:
        self._buffer = ""
        self._inside_reasoning = False
        self._min_chunk_length = min_chunk_length
        self._fb = FastBunkai()

        self._reasoning_pattern = re.compile(
            f"({re.escape(reasoning_start_tag)}|{re.escape(reasoning_end_tag)})",
            re.IGNORECASE,
        )
        self._tool_call_pattern = re.compile(
            r"\{\s*\'type\'\s*:\s*\'tool_call\'[\s\S]*?\}\}"
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []

        filtered = self._filter_reasoning_stream(delta)
        if not filtered:
            return []

        self._buffer += filtered
        self._buffer = self._tool_call_pattern.sub("", self._buffer)

        result: list[str] = []
        while any(c in self._SENTENCE_ENDERS for c in self._buffer):
            positions = self._fb.find_eos(self._buffer)
            real_positions = [
                p
                for p in positions
                if p > 0
                and (s := self._buffer[:p].rstrip())
                and s[-1] in self._SENTENCE_ENDERS
            ]
            if not real_positions:
                break

            emitted = False
            for pos in real_positions:
                segment = self._buffer[:pos].strip()
                if len(segment) >= self._min_chunk_length:
                    result.append(segment)
                    self._buffer = self._buffer[pos:]
                    emitted = True
                    break
            if not emitted:
                break

        if result:
            logger.debug("SentenceChunker emitted {} chunks", len(result))
        return result

    def flush(self) -> str | None:
        self._buffer = self._tool_call_pattern.sub("", self._buffer)
        remaining = self._buffer.strip()
        self._reset()
        if not remaining:
            return None
        return remaining

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _filter_reasoning_stream(self, chunk: str) -> str:
        parts = self._reasoning_pattern.split(chunk)
        filtered = ""
        for part in parts:
            if not part:
                continue
            lowered = part.lower()
            if lowered == "<think>":
                self._inside_reasoning = True
            elif lowered == "</think>":
                self._inside_reasoning = False
            elif not self._inside_reasoning:
                filtered += part
        return filtered

    def _reset(self) -> None:
        self._buffer = ""
        self._inside_reasoning = False
