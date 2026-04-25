"""Test harness for Phase 3-D regression scenarios.

Composes :class:`DesktopMateChannel` with a real :class:`TTSHook` built
on the actual Preprocessor / SentenceChunker / EmotionMapper and a
recording fake synthesizer. Simulates nanobot's bus dispatch by having
the test drive ``channel.send_delta`` / ``channel.send`` directly with
the same ``_stream_delta`` / ``_stream_end`` metadata nanobot would emit.

This gives deterministic, in-process regression coverage of the wire
contract without depending on a running gateway or live LLM/TTS
backends. See :mod:`tests.regression.test_scenarios` for the scenarios.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot_runtime.services.channels.desktop_mate import (
    DesktopMateChannel,
    DesktopMateConfig,
    _reset_registry_for_tests,
)
from nanobot_runtime.services.hooks.tts import TTSChunk, TTSHook
from nanobot_runtime.services.tts.chunker import SentenceChunker
from nanobot_runtime.services.tts.emotion_mapper import EmotionMapper
from nanobot_runtime.services.tts.preprocessor import Preprocessor


class FakeConnection:
    """In-memory stand-in for a websockets ServerConnection."""

    def __init__(self, inbox: list[str] | None = None):
        self.sent: list[str] = []
        self.inbox: list[str] = list(inbox or [])
        self.remote_address = ("127.0.0.1", 12345)
        self.closed = False
        self.close_code: int | None = None

    async def send(self, raw: str) -> None:
        if self.closed:
            raise RuntimeError("connection closed")
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code

    def __aiter__(self):
        async def _gen():
            for item in list(self.inbox):
                yield item

        return _gen()


class FakeBus:
    def __init__(self):
        self.inbound: list[InboundMessage] = []
        self.outbound: list[OutboundMessage] = []

    async def publish_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.outbound.append(msg)


class RecordingSynthesizer:
    """Fake TTSSynthesizer that records every call — lets a test assert
    exact synth counts (key signal for "TTS disabled → no work done")."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ref_calls: list[str | None] = []

    async def synthesize(self, text: str, *, reference_id: str | None = None) -> str | None:
        self.calls.append(text)
        self.ref_calls.append(reference_id)
        # Short, fixed base64 so tests can assert the FULL tts_chunk
        # without caring about actual audio.
        return "QUJDRA=="  # "ABCD"


class DirectSink:
    """Sink that forwards to a specific channel (not via module registry).

    Regression tests instantiate one channel per scenario and want to
    avoid cross-test state leakage from the module-level _LATEST_CHANNEL.
    The channel is also an ``is_enabled``-aware TTSSink so TTSHook's skip
    path is exercised.
    """

    def __init__(self, channel: DesktopMateChannel):
        self._channel = channel

    def is_enabled(self) -> bool:
        return self._channel.is_tts_enabled_for_current_stream()

    async def send_tts_chunk(self, chunk: TTSChunk) -> None:
        await self._channel.send_tts_chunk(chunk)


@dataclass
class Harness:
    """Full-stack channel+hook harness for a single scenario.

    Fields:
        channel: the DesktopMateChannel under test
        bus: FakeBus capturing InboundMessages
        hook: real TTSHook wired to channel via DirectSink
        synth: RecordingSynthesizer (lets tests assert call counts)
        emotion_emojis: the set installed on the channel
    """

    channel: DesktopMateChannel
    bus: FakeBus
    hook: TTSHook
    synth: RecordingSynthesizer
    emotion_emojis: set[str] = field(default_factory=set)

    # -- Inbound helpers --------------------------------------------------

    async def connect(self, conn: FakeConnection) -> None:
        """Simulate a successful handshake (auth + allow_from pass)."""
        # Mirror DesktopMateChannel.handler() for the fake:
        # _handshake + _send_ready would be the real thing; we just
        # attach the connection as "accepted".
        pass  # No server loop; scenarios just feed inbox via drive_inbound.

    async def drive_inbound(self, conn: FakeConnection) -> None:
        """Run the channel's inbound loop over the connection's inbox."""
        await self.channel._connection_loop(conn, sender_id="regression")

    def tts_override_tts_zero(self, conn: FakeConnection) -> None:
        """Set the URL-level override as if ?tts=0 was in the handshake."""
        self.channel._apply_connection_tts_override(conn, {"tts": ["0"]})

    # -- Agent-side simulation --------------------------------------------

    async def simulate_agent_turn(
        self,
        chat_id: str,
        deltas: list[str],
        *,
        stream_id: str | None = None,
        final_content: str | None = None,
    ) -> None:
        """Simulate nanobot dispatching one streamed turn to chat_id.

        This reproduces the exact sequence the agent loop+bus dispatcher
        produces: deltas with ``_stream_delta`` metadata, followed by a
        final message carrying ``_stream_end`` on the send() path.

        The TTSHook is driven separately (on_stream per delta +
        on_stream_end at the end) so synthesis and the TTS Barrier are
        exercised exactly like in production.
        """
        sid = stream_id or f"test-stream:{uuid.uuid4().hex[:8]}"

        # Reset hook sequence at a turn boundary (iteration==0 + no pending).
        from nanobot.agent.hook import AgentHookContext

        ctx = AgentHookContext(
            iteration=0,
            messages=[],
            session_key=f"desktop_mate:{chat_id}",
        )

        for delta in deltas:
            # Channel dispatches the delta first so stream routing state is
            # registered BEFORE the hook asks ``sink.is_enabled()``. This
            # matches the practical nanobot order where bus.publish yields
            # to the dispatcher task before the loop moves on to the hook.
            await self.channel.send_delta(
                chat_id,
                delta,
                {"_stream_delta": True, "_stream_id": sid},
            )
            # Hook sees the delta. At this point the channel already knows
            # the stream → is_enabled() reflects per-chat / URL override.
            await self.hook.on_stream(ctx, delta)

        # Barrier + final stream_end — matches how nanobot's agent loop
        # closes a turn.
        await self.hook.on_stream_end(ctx, resuming=False)
        await self.channel.send(
            OutboundMessage(
                channel="desktop_mate",
                chat_id=chat_id,
                content=final_content if final_content is not None else "".join(deltas),
                metadata={"_stream_end": True, "_stream_id": sid},
            )
        )

    # -- Frame decoding ---------------------------------------------------

    @staticmethod
    def frames(conn: FakeConnection) -> list[dict[str, Any]]:
        return [json.loads(raw) for raw in conn.sent]

    @staticmethod
    def events(conn: FakeConnection) -> list[str]:
        return [f.get("event") for f in Harness.frames(conn)]


def build_harness(
    *,
    emotion_emojis: set[str] | None = None,
    min_chunk_length: int = 1,
) -> Harness:
    """Construct a channel+hook stack for one scenario.

    ``min_chunk_length=1`` so chunker doesn't coalesce short sentences —
    regression tests want to see every sentence boundary.
    """
    _reset_registry_for_tests()

    channel = DesktopMateChannel(
        config=DesktopMateConfig(token="", allow_from=["*"], host="127.0.0.1", port=0),
        bus=FakeBus(),
        emotion_emojis=emotion_emojis or set(),
    )
    emojis = emotion_emojis or set()
    emotion_map = {
        emoji: {"keyframes": [{"duration": 0.3, "targets": {"happy": 1.0}}]}
        for emoji in emojis
    }
    synth = RecordingSynthesizer()
    hook = TTSHook(
        chunker_factory=lambda: SentenceChunker(min_chunk_length=min_chunk_length),
        preprocessor=Preprocessor(known_emojis=frozenset(emojis)),
        emotion_mapper=EmotionMapper(emotion_map),
        synthesizer=synth,
        sink=DirectSink(channel),
    )
    return Harness(
        channel=channel,
        bus=channel.bus,  # type: ignore[arg-type]
        hook=hook,
        synth=synth,
        emotion_emojis=emojis,
    )
