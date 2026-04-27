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

import email.utils
import http
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger

from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response


SESSION_KEY_PREFIX = "desktop_mate:"

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
    """Extract bearer token, accepting both ``Authorization`` and ``authorization`` headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def is_websocket_upgrade(request: WsRequest) -> bool:
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


def decode_api_key(raw_key: str) -> str | None:
    """URL-decode and validate a session key from the path."""
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


# ── Session REST Handlers ─────────────────────────────────────────────────


def _is_dm_session(key: str) -> bool:
    return key.startswith(SESSION_KEY_PREFIX)


def _token_valid(token_cfg: str, request: WsRequest) -> bool:
    """Check REST auth: Bearer header or ``?token=`` query param."""
    expected = (token_cfg or "").strip()
    if not expected:
        return True
    _, query = parse_request_path(request.path)
    supplied = bearer_token(request.headers) or query_first(query, "token")
    return supplied is not None and supplied == expected


def handle_sessions_list(
    token: str, session_manager: Any, request: WsRequest
) -> Response:
    if not _token_valid(token, request):
        return http_error(401, "Unauthorized")
    if session_manager is None:
        return http_error(503, "session manager unavailable")
    try:
        sessions = session_manager.list_sessions()
    except Exception:
        logger.opt(exception=True).error("desktop_mate: list_sessions raised")
        return http_error(500, "internal error")
    cleaned = [
        {k: v for k, v in s.items() if k != "path"}
        for s in sessions
        if isinstance(s.get("key"), str) and _is_dm_session(s["key"])
    ]
    return http_json_response({"sessions": cleaned})


def handle_session_messages(
    token: str, session_manager: Any, request: WsRequest, key: str
) -> Response:
    if not _token_valid(token, request):
        return http_error(401, "Unauthorized")
    if session_manager is None:
        return http_error(503, "session manager unavailable")
    decoded = decode_api_key(key)
    if decoded is None:
        return http_error(400, "invalid session key")
    if not _is_dm_session(decoded):
        return http_error(404, "session not found")
    try:
        data = session_manager.read_session_file(decoded)
    except Exception:
        logger.opt(exception=True).error("desktop_mate: read_session_file raised")
        return http_error(500, "internal error")
    if data is None:
        return http_error(404, "session not found")
    return http_json_response(data)


def handle_session_delete(
    token: str, session_manager: Any, request: WsRequest, key: str
) -> Response:
    if not _token_valid(token, request):
        return http_error(401, "Unauthorized")
    if session_manager is None:
        return http_error(503, "session manager unavailable")
    decoded = decode_api_key(key)
    if decoded is None:
        return http_error(400, "invalid session key")
    if not _is_dm_session(decoded):
        return http_error(404, "session not found")
    try:
        deleted = session_manager.delete_session(decoded)
    except Exception:
        logger.opt(exception=True).error("desktop_mate: delete_session raised")
        return http_error(500, "internal error")
    return http_json_response({"deleted": bool(deleted)})


def dispatch_http(
    token: str, session_manager: Any, ws_path: str, request: WsRequest
) -> Response | None:
    """Route inbound HTTP to a REST handler; return None to proceed with WS upgrade."""
    got, _ = parse_request_path(request.path)
    if got == "/api/sessions":
        return handle_sessions_list(token, session_manager, request)
    m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
    if m:
        return handle_session_messages(token, session_manager, request, m.group(1))
    m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
    if m:
        return handle_session_delete(token, session_manager, request, m.group(1))
    expected_ws = ws_path.rstrip("/") or "/"
    if got == expected_ws and is_websocket_upgrade(request):
        return None
    return http_error(404, "Not Found")
