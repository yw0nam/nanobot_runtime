"""TTSChunk — data shape emitted to the TTS sink per completed sentence."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TTSChunk(BaseModel):
    """Data emitted to the TTS sink per completed sentence."""

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(
        description="Zero-based index of this chunk within the current stream."
    )
    text: str = Field(description="Cleaned sentence text sent to the TTS engine.")
    audio_base64: str | None = Field(
        description="Base64-encoded WAV audio, or None on failure."
    )
    emotion: str | None = Field(description="Detected emotion emoji/tag, or None.")
    keyframes: list[dict[str, Any]] = Field(
        default_factory=list, description="Animation keyframe dicts for the emotion."
    )
