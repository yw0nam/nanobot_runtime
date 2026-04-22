"""IrodoriClient — async TTS synthesizer for Aratako/Irodori-TTS-500M-v2.

Ported from DMP's ``IrodoriTTSService`` (src/services/tts_service/
irodori_tts.py) but reduced to the essentials needed by the nanobot
``TTSSynthesizer`` Protocol:

    async def synthesize(self, text: str) -> str | None

Differences from DMP:
    * Uses ``httpx.AsyncClient`` instead of sync ``httpx.Client``.
    * Returns base64-encoded WAV string (not configurable — always base64).
    * ``reference_id`` is a single fixed voice (not discovered via scan).
    * Swallows every error path back to ``None`` and logs, matching DMP's
      graceful-degradation behavior.

Request shape matches DMP's ``_post_synthesize``::

    POST {base_url}/synthesize
    form fields: text, seconds, num_steps, cfg_scale_text, cfg_scale_speaker
    optional multipart file: reference_audio = {ref_audio_dir}/{reference_id}/merged_audio.mp3
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


class IrodoriClient:
    """Async HTTP client for Irodori TTS ``POST /synthesize`` endpoint."""

    def __init__(
        self,
        base_url: str,
        reference_id: str | None = None,
        ref_audio_dir: Path | None = None,
        *,
        timeout: float = 30.0,
        seconds: float = 30.0,
        num_steps: int = 40,
        cfg_scale_text: float = 3.0,
        cfg_scale_speaker: float = 5.0,
        seed: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.reference_id = reference_id
        self.ref_audio_dir = Path(ref_audio_dir) if ref_audio_dir is not None else None
        self.timeout = timeout
        self.seconds = seconds
        self.num_steps = num_steps
        self.cfg_scale_text = cfg_scale_text
        self.cfg_scale_speaker = cfg_scale_speaker
        self.seed = seed

    # ------------------------------------------------------------------
    # TTSSynthesizer protocol
    # ------------------------------------------------------------------

    async def synthesize(self, text: str) -> str | None:
        tts_text = text.strip() if text else ""
        if not tts_text:
            return None

        # Entry log: lets regression E2E verify "synthesize() was NOT
        # called" for TTS-off cases by grepping the gateway log. Truncate
        # long text to keep the line bounded.
        preview = tts_text if len(tts_text) <= 60 else tts_text[:57] + "..."
        logger.info("IrodoriClient.synthesize: {!r}", preview)

        reference_audio_path = self._resolve_reference_audio()
        if self.reference_id is not None and reference_audio_path is None:
            # Reference requested but couldn't be resolved — fail closed.
            return None

        audio_bytes = await self._post_synthesize(tts_text, reference_audio_path)
        if not audio_bytes:
            return None
        return base64.b64encode(audio_bytes).decode("utf-8")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_reference_audio(self) -> Path | None:
        if self.reference_id is None:
            return None
        if self.ref_audio_dir is None:
            logger.error(
                "IrodoriClient: reference_id '{}' given but ref_audio_dir is not set",
                self.reference_id,
            )
            return None
        candidate = self.ref_audio_dir / self.reference_id / "merged_audio.mp3"
        if not candidate.exists():
            logger.error("IrodoriClient: reference audio not found: {}", candidate)
            return None
        return candidate

    async def _post_synthesize(
        self, text: str, reference_audio_path: Path | None
    ) -> bytes | None:
        url = f"{self.base_url}/synthesize"
        data: dict[str, Any] = {
            "text": text,
            "seconds": self.seconds,
            "num_steps": self.num_steps,
            "cfg_scale_text": self.cfg_scale_text,
            "cfg_scale_speaker": self.cfg_scale_speaker,
        }
        if self.seed is not None:
            data["seed"] = self.seed

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                if reference_audio_path is not None:
                    with reference_audio_path.open("rb") as ref_handle:
                        files = {
                            "reference_audio": (
                                reference_audio_path.name,
                                ref_handle,
                                "audio/wav",
                            )
                        }
                        response = await client.post(url, data=data, files=files)
                else:
                    response = await client.post(url, data=data)
                response.raise_for_status()
                return bytes(response.content)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "IrodoriClient HTTP error {} from {}",
                exc.response.status_code,
                url,
            )
            return None
        except httpx.RequestError:
            logger.opt(exception=True).warning("IrodoriClient request failed")
            return None
        except Exception:
            logger.opt(exception=True).warning("IrodoriClient unexpected error")
            return None
