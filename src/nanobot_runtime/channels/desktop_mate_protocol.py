"""Wire-format schemas for :mod:`nanobot_runtime.channels.desktop_mate`.

The DMP frontend speaks a small, stable set of JSON frames. Keeping the
schema in one place (rather than scattered dict literals inside the
channel) makes the contract auditable and prevents silent drift:

* Outbound (server→FE): :class:`StreamStartFrame` / :class:`DeltaFrame`
  / :class:`StreamEndFrame` / :class:`TTSChunkFrame` /
  :class:`ImageRejectedFrame`
* Inbound (FE→server): :class:`NewChatFrame` / :class:`MessageFrame`
  as a discriminated union — parse with :func:`parse_inbound`.

All outbound frames are serialised via ``model_dump_json(exclude_none=True)``
so optional fields such as ``proactive`` or ``stream_id`` are omitted
when unset. ``TTSChunkFrame`` intentionally keeps ``audio_base64`` / ``emotion``
serialised as explicit ``null`` because the frontend treats ``null`` as
"TTS unavailable, play silence" — dropping the key would change semantics.

Image intake (issue #8): inbound frames may carry an ``images`` field
(a list of ``data:<mime>;base64,<payload>`` URLs, max
:data:`_MAX_IMAGES_PER_MESSAGE` entries). Per-image byte/MIME validation
happens at the channel layer, which surfaces :class:`ImageRejectedFrame`
to the client on failure.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)


# Per-frame image cap. Mirrors upstream nanobot's built-in WS channel
# (``nanobot.channels.websocket._MAX_IMAGES_PER_MESSAGE``) — keeping the
# cap in lock-step avoids asymmetric surprises across ingress paths.
_MAX_IMAGES_PER_MESSAGE = 4


# ---------------------------------------------------------------------------
# Outbound frames
# ---------------------------------------------------------------------------


class _OutboundBase(BaseModel):
    """Common base for outbound frames.

    ``model_config`` forbids extras to catch typos at construction time and
    freezes instances — frames are value objects, never mutated after build.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chat_id: str
    proactive: bool | None = None


class ReadyFrame(BaseModel):
    """Sent once per connection after a successful handshake.

    This fills the role of the legacy DMP ``authorize_success`` frame:
    it tells the frontend that the connection is live and carries a
    ``connection_id`` that trace logs can correlate against. Unlike the
    other outbound frames it has no ``chat_id`` — the connection is not
    yet bound to a chat session.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["ready"] = "ready"
    connection_id: str
    client_id: str
    server_time: float


class StreamStartFrame(_OutboundBase):
    event: Literal["stream_start"] = "stream_start"


class DeltaFrame(_OutboundBase):
    event: Literal["delta"] = "delta"
    text: str
    stream_id: str | None = None


class StreamEndFrame(_OutboundBase):
    event: Literal["stream_end"] = "stream_end"
    content: str


class ImageRejectedFrame(BaseModel):
    """Sent when the channel refuses to accept a turn's ``images`` payload.

    Carries a short, stable ``reason`` token (``"malformed"``,
    ``"too_large"``, ``"unsupported_mime"``, ``"too_many"``) so the FE can
    render a localized message without parsing free-form prose. The whole
    turn is rejected — the agent loop is never entered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["image_rejected"] = "image_rejected"
    chat_id: str | None = None
    reason: str


class TTSChunkFrame(_OutboundBase):
    event: Literal["tts_chunk"] = "tts_chunk"
    sequence: int
    text: str
    # These three are "explicit-null-significant": FE treats null as
    # "synthesis failed, play silence". model_dump_json(exclude_none=True)
    # would drop them, so we override serialisation per-instance below.
    audio_base64: str | None
    emotion: str | None
    keyframes: list[dict[str, Any]] = Field(default_factory=list)

    def model_dump_json(self, **kwargs: Any) -> str:  # type: ignore[override]
        # Force retention of audio_base64/emotion even when null — see class
        # docstring. We deliberately *do* still honour exclude_none for the
        # inherited ``proactive`` field.
        kwargs.pop("exclude_none", None)
        raw = super().model_dump_json(exclude_none=False, **kwargs)
        # Drop proactive when None. Cheap: avoid a second pydantic pass.
        if self.proactive is None:
            import json as _json

            data = _json.loads(raw)
            data.pop("proactive", None)
            return _json.dumps(data, ensure_ascii=False)
        return raw


# ---------------------------------------------------------------------------
# Inbound frames
# ---------------------------------------------------------------------------


class _InboundBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str
    tts_enabled: bool = True
    reference_id: str | None = None
    # Inbound image attachments as ``data:<mime>;base64,<payload>`` URLs.
    # Per-item byte/MIME validation happens at the channel layer — see
    # ``DesktopMateChannel._decode_inbound_images``. Only the per-frame
    # count cap is enforced here so malformed frames fail fast.
    images: list[str] | None = Field(
        default=None,
        max_length=_MAX_IMAGES_PER_MESSAGE,
    )

    @model_validator(mode="after")
    def _content_or_images_required(self) -> "_InboundBase":
        # Image-only turns are permitted (the LLM can caption them), so a
        # blank ``content`` is only rejected when there are no images to
        # carry the turn. Matches upstream websocket.py:1043.
        has_text = bool(self.content and self.content.strip())
        has_images = bool(self.images)
        if not has_text and not has_images:
            raise ValueError("content must be non-empty")
        return self


class NewChatFrame(_InboundBase):
    type: Literal["new_chat"]


class MessageFrame(_InboundBase):
    type: Literal["message"]
    chat_id: str = Field(min_length=1)


InboundFrame = Annotated[
    Union[NewChatFrame, MessageFrame],
    Field(discriminator="type"),
]

InboundEnvelope = TypeAdapter(InboundFrame)


def parse_inbound(raw: str | bytes) -> NewChatFrame | MessageFrame:
    """Parse an inbound JSON envelope into the matching frame model.

    Raises :class:`pydantic.ValidationError` for schema violations and
    :class:`ValueError` for malformed JSON.
    """
    return InboundEnvelope.validate_json(raw)
