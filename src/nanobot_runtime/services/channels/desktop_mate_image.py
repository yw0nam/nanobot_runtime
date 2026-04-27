"""Image decode and validation logic for DesktopMateChannel (issue #8).

Validates count, MIME type, size, and base64 integrity; persists decoded
images to the media directory. Every failure surfaces to the caller as an
``ImageRejectReason`` token so the FE can handle it without parsing prose.
"""

import binascii
import re
from pathlib import Path

from loguru import logger

from nanobot.utils.media_decode import FileSizeExceeded, save_base64_data_url

from nanobot_runtime.models.desktop_mate import (
    ImageRejectReason,
    _MAX_IMAGES_PER_MESSAGE,
)


# 10 MB per image — mirrors upstream nanobot's built-in WS channel intent.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# SVG is excluded to avoid embedded-script XSS surface.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }
)

_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _decode_images(
    images: list[str] | None,
    *,
    sender_id: str,
    media_dir: Path,
    max_image_bytes: int,
) -> tuple[list[str], ImageRejectReason | None]:
    """Decode a frame's ``images`` entries to disk.

    Returns ``(paths, None)`` on success or ``([], reason)`` on the first
    failure. Whole turn is rejected on any failure — no partial ingress.
    Any images already written before a later failure are unlinked.
    """
    if not images:
        return [], None

    if len(images) > _MAX_IMAGES_PER_MESSAGE:
        logger.warning(
            "desktop_mate: rejecting images from {}: count={} exceeds cap={}",
            sender_id,
            len(images),
            _MAX_IMAGES_PER_MESSAGE,
        )
        return [], "too_many"

    saved_paths: list[str] = []

    def _abort(
        reason: ImageRejectReason,
        *,
        mime: str | None = None,
        size_hint: int | None = None,
    ) -> tuple[list[str], ImageRejectReason]:
        level = "error" if reason == "io_error" else "warning"
        getattr(logger, level)(
            "desktop_mate: rejecting image from {}: reason={} mime={} size_hint={}",
            sender_id,
            reason,
            mime,
            size_hint,
        )
        for p in saved_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                logger.opt(exception=True).warning(
                    "desktop_mate: failed to unlink partial media {}", p
                )
        return [], reason

    for entry in images:
        if not isinstance(entry, str) or not entry:
            return _abort("malformed")
        mime = _extract_data_url_mime(entry)
        if mime is None:
            return _abort("malformed")
        if mime not in _IMAGE_MIME_ALLOWED:
            return _abort("unsupported_mime", mime=mime)
        try:
            saved = save_base64_data_url(entry, media_dir, max_bytes=max_image_bytes)
        except FileSizeExceeded:
            return _abort("too_large", mime=mime, size_hint=len(entry))
        except (binascii.Error, ValueError):
            logger.opt(exception=True).warning(
                "desktop_mate: decode failed (caller-fixable)"
            )
            return _abort("malformed", mime=mime, size_hint=len(entry))
        except OSError:
            logger.opt(exception=True).error("desktop_mate: image persist failed")
            return _abort("io_error", mime=mime, size_hint=len(entry))
        if saved is None:
            return _abort("malformed", mime=mime, size_hint=len(entry))
        saved_paths.append(saved)
    return saved_paths, None
