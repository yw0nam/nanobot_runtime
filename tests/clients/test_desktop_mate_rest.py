"""Tests for DesktopMateChannel's REST surface (session list / messages / delete).

Routes mirror nanobot's WebSocketChannel but with a ``desktop_mate:`` prefix
filter and the channel's static ``?token=`` auth (instead of the TTL pool).
These unit tests exercise ``_dispatch_http`` directly with fake requests
and a fake SessionManager — no real socket binding.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


from nanobot_runtime.services.channels.desktop_mate import (
    DesktopMateChannel,
    DesktopMateConfig,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeHeaders(dict):
    """Case-insensitive enough for our bearer_token() helper."""


class FakeRequest:
    def __init__(self, path: str, headers: dict[str, str] | None = None):
        self.path = path
        self.headers = FakeHeaders(headers or {})


class FakeBus:
    async def publish_inbound(self, msg: Any) -> None:  # pragma: no cover - unused
        pass

    async def publish_outbound(self, msg: Any) -> None:  # pragma: no cover - unused
        pass


class FakeSessionManager:
    def __init__(
        self,
        *,
        sessions: list[dict[str, Any]] | None = None,
        files: dict[str, dict[str, Any]] | None = None,
        deletable: set[str] | None = None,
    ):
        self._sessions = sessions or []
        self._files = files or {}
        self._deletable = deletable or set()
        self.deleted: list[str] = []

    def list_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions)

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        return self._files.get(key)

    def delete_session(self, key: str) -> bool:
        self.deleted.append(key)
        return key in self._deletable


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_channel(
    *,
    token: str = "secret",
    session_manager: Any | None = None,
) -> DesktopMateChannel:
    return DesktopMateChannel(
        config=DesktopMateConfig(token=token, host="127.0.0.1", port=0, path="/ws"),
        bus=FakeBus(),
        emotion_emojis=set(),
        session_manager=session_manager,
    )


def _body_json(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def _run(coro):
    return (
        asyncio.get_event_loop().run_until_complete(coro)
        if False
        else asyncio.run(coro)
    )


# ── Dispatch routing ─────────────────────────────────────────────────────────


def test_dispatch_unknown_path_returns_404():
    ch = _make_channel(session_manager=FakeSessionManager())
    req = FakeRequest("/nope")
    resp = asyncio.run(ch._dispatch_http(connection=None, request=req))
    assert resp.status_code == 404


def test_dispatch_ws_upgrade_falls_through_to_handshake():
    ch = _make_channel(session_manager=FakeSessionManager())
    req = FakeRequest(
        "/ws?token=secret",
        headers={"Upgrade": "websocket", "Connection": "Upgrade"},
    )
    resp = asyncio.run(ch._dispatch_http(connection=None, request=req))
    assert resp is None  # None → websockets lib continues the handshake


def test_dispatch_non_upgrade_to_ws_path_is_404():
    ch = _make_channel(session_manager=FakeSessionManager())
    # Plain GET to /ws without Upgrade header should not be treated as an
    # unauth'd handshake — just a miss.
    req = FakeRequest("/ws")
    resp = asyncio.run(ch._dispatch_http(connection=None, request=req))
    assert resp.status_code == 404


# ── /api/sessions (list) ─────────────────────────────────────────────────────


def test_sessions_list_filters_prefix_and_strips_path():
    sm = FakeSessionManager(
        sessions=[
            {
                "key": "desktop_mate:abc",
                "created_at": "t1",
                "updated_at": "t2",
                "path": "/x/abc.jsonl",
            },
            {
                "key": "desktop_mate:def",
                "created_at": "t3",
                "updated_at": "t4",
                "path": "/x/def.jsonl",
            },
            {
                "key": "websocket:foo",
                "created_at": "t5",
                "updated_at": "t6",
                "path": "/x/foo.jsonl",
            },
            {
                "key": "slack:C1:1.0",
                "created_at": "t7",
                "updated_at": "t8",
                "path": "/x/s.jsonl",
            },
        ],
    )
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(None, FakeRequest("/api/sessions?token=secret"))
    )
    assert resp.status_code == 200
    data = _body_json(resp)
    keys = [s["key"] for s in data["sessions"]]
    assert keys == ["desktop_mate:abc", "desktop_mate:def"]
    for s in data["sessions"]:
        assert "path" not in s


def test_sessions_list_401_when_token_missing():
    ch = _make_channel(session_manager=FakeSessionManager())
    resp = asyncio.run(ch._dispatch_http(None, FakeRequest("/api/sessions")))
    assert resp.status_code == 401


def test_sessions_list_401_when_token_wrong():
    ch = _make_channel(session_manager=FakeSessionManager())
    resp = asyncio.run(
        ch._dispatch_http(None, FakeRequest("/api/sessions?token=bogus"))
    )
    assert resp.status_code == 401


def test_sessions_list_accepts_bearer_header():
    sm = FakeSessionManager(sessions=[])
    ch = _make_channel(session_manager=sm)
    req = FakeRequest("/api/sessions", headers={"Authorization": "Bearer secret"})
    resp = asyncio.run(ch._dispatch_http(None, req))
    assert resp.status_code == 200


def test_sessions_list_503_when_manager_missing():
    ch = _make_channel(session_manager=None)
    resp = asyncio.run(
        ch._dispatch_http(None, FakeRequest("/api/sessions?token=secret"))
    )
    assert resp.status_code == 503


def test_sessions_list_allows_all_when_no_token_configured():
    sm = FakeSessionManager(sessions=[])
    ch = DesktopMateChannel(
        config=DesktopMateConfig(token="", host="127.0.0.1", port=0, path="/ws"),
        bus=FakeBus(),
        emotion_emojis=set(),
        session_manager=sm,
    )
    resp = asyncio.run(ch._dispatch_http(None, FakeRequest("/api/sessions")))
    assert resp.status_code == 200


# ── /api/sessions/{key}/messages (read) ──────────────────────────────────────


def test_session_messages_happy_path():
    sm = FakeSessionManager(
        files={
            "desktop_mate:abc": {
                "key": "desktop_mate:abc",
                "created_at": "t",
                "updated_at": "t",
                "metadata": {},
                "messages": [{"role": "user", "content": "hi"}],
            }
        }
    )
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(
            None, FakeRequest("/api/sessions/desktop_mate:abc/messages?token=secret")
        )
    )
    assert resp.status_code == 200
    data = _body_json(resp)
    assert data["key"] == "desktop_mate:abc"
    assert data["messages"][0]["content"] == "hi"


def test_session_messages_400_invalid_key_chars():
    sm = FakeSessionManager()
    ch = _make_channel(session_manager=sm)
    # Space is not in the allowed charset.
    resp = asyncio.run(
        ch._dispatch_http(
            None, FakeRequest("/api/sessions/bad%20key/messages?token=secret")
        )
    )
    assert resp.status_code == 400


def test_session_messages_404_wrong_prefix():
    sm = FakeSessionManager(
        files={"websocket:foo": {"key": "websocket:foo", "messages": []}}
    )
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(
            None, FakeRequest("/api/sessions/websocket:foo/messages?token=secret")
        )
    )
    assert resp.status_code == 404


def test_session_messages_404_when_not_found():
    sm = FakeSessionManager(files={})
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(
            None,
            FakeRequest("/api/sessions/desktop_mate:missing/messages?token=secret"),
        )
    )
    assert resp.status_code == 404


def test_session_messages_401_no_token():
    ch = _make_channel(session_manager=FakeSessionManager())
    resp = asyncio.run(
        ch._dispatch_http(None, FakeRequest("/api/sessions/desktop_mate:x/messages"))
    )
    assert resp.status_code == 401


# ── /api/sessions/{key}/delete ───────────────────────────────────────────────


def test_session_delete_true():
    sm = FakeSessionManager(deletable={"desktop_mate:abc"})
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(
            None, FakeRequest("/api/sessions/desktop_mate:abc/delete?token=secret")
        )
    )
    assert resp.status_code == 200
    assert _body_json(resp) == {"deleted": True}
    assert sm.deleted == ["desktop_mate:abc"]


def test_session_delete_false_when_missing():
    sm = FakeSessionManager(deletable=set())
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(
            None, FakeRequest("/api/sessions/desktop_mate:abc/delete?token=secret")
        )
    )
    assert resp.status_code == 200
    assert _body_json(resp) == {"deleted": False}


def test_session_delete_404_wrong_prefix():
    sm = FakeSessionManager(deletable={"websocket:foo"})
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(
            None, FakeRequest("/api/sessions/websocket:foo/delete?token=secret")
        )
    )
    assert resp.status_code == 404
    # Cross-channel delete must be blocked even before reaching the manager.
    assert sm.deleted == []


def test_session_delete_400_invalid_key():
    sm = FakeSessionManager()
    ch = _make_channel(session_manager=sm)
    resp = asyncio.run(
        ch._dispatch_http(
            None, FakeRequest("/api/sessions/ohno%21bang/delete?token=secret")
        )
    )
    assert resp.status_code == 400
