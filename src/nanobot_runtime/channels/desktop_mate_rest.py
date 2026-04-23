"""HTTP helpers for :class:`DesktopMateChannel`'s REST surface.

Mirrors nanobot's ``WebSocketChannel`` session routes so the FE's existing
STM sidebar (list / history / delete) keeps working against our custom
channel. We intentionally copy the helpers rather than importing them
from ``nanobot.channels.websocket`` — those symbols are private and the
upstream module is large; pinning to them would fragile-couple us to
nanobot internals beyond what ``gateway.py`` already does.

Auth model is intentionally simpler than nanobot's TTL-bound token pool:
we reuse the static ``?token=`` used for the WebSocket handshake.
Rotating tokens can layer on later without changing the REST shape.
"""
from __future__ import annotations

import email.utils
import http
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response


# Session keys look like ``desktop_mate:<chat_id>``. Keep permissive enough
# for hyphenated UUIDs and colon-separated namespacing, but tight enough
# to rule out path traversal / quote injection.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def parse_request_path(path_with_query: str) -> tuple[str, dict[str, list[str]]]:
    """Split a raw request path into ``(normalized_path, query_dict)``."""
    parsed = urlparse("ws://x" + path_with_query)
    path = _strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query)


def query_first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def bearer_token(headers: Any) -> str | None:
    """Extract a ``Authorization: Bearer <token>`` header value."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def is_websocket_upgrade(request: WsRequest) -> bool:
    """Return True iff the request is an actual WS upgrade handshake."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


def decode_api_key(raw_key: str) -> str | None:
    """URL-decode a session key from the path and validate it."""
    key = unquote(raw_key)
    if _API_KEY_RE.match(key) is None:
        return None
    return key


def http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return http_response(
        body, status=status, content_type="application/json; charset=utf-8"
    )


def http_error(status: int, message: str | None = None) -> Response:
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return http_response(body, status=status)
