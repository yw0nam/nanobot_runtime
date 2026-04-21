"""Tests for IrodoriClient — async HTTP client wrapping POST /synthesize.

Uses pytest_httpx's `httpx_mock` fixture to stub httpx.AsyncClient.
"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from nanobot_runtime.tts.irodori import IrodoriClient


@pytest.fixture
def base_url() -> str:
    return "http://localhost:9999"


async def test_successful_post_returns_base64(
    httpx_mock: HTTPXMock, base_url: str
) -> None:
    payload = b"RIFF....WAV_FAKE_BYTES"
    httpx_mock.add_response(
        method="POST",
        url=f"{base_url}/synthesize",
        content=payload,
        status_code=200,
    )
    client = IrodoriClient(base_url=base_url)
    result = await client.synthesize("Hello.")
    assert result == base64.b64encode(payload).decode("utf-8")


async def test_http_error_returns_none(httpx_mock: HTTPXMock, base_url: str) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{base_url}/synthesize",
        status_code=500,
        content=b"internal error",
    )
    client = IrodoriClient(base_url=base_url)
    result = await client.synthesize("Hello.")
    assert result is None


async def test_empty_text_short_circuits_without_http_call(
    httpx_mock: HTTPXMock, base_url: str
) -> None:
    # No mocked response registered — if client tried to POST, test would fail.
    client = IrodoriClient(base_url=base_url)
    assert await client.synthesize("") is None
    assert await client.synthesize("   ") is None
    # Confirm no HTTP was attempted.
    assert httpx_mock.get_requests() == []


async def test_request_error_returns_none(
    httpx_mock: HTTPXMock, base_url: str
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    client = IrodoriClient(base_url=base_url)
    assert await client.synthesize("Hello.") is None


async def test_unexpected_exception_returns_none(
    httpx_mock: HTTPXMock, base_url: str
) -> None:
    httpx_mock.add_exception(RuntimeError("boom"))
    client = IrodoriClient(base_url=base_url)
    assert await client.synthesize("Hello.") is None


async def test_trailing_slash_in_base_url_is_normalized(
    httpx_mock: HTTPXMock,
) -> None:
    payload = b"ok"
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:9999/synthesize",
        content=payload,
        status_code=200,
    )
    client = IrodoriClient(base_url="http://localhost:9999/")
    result = await client.synthesize("Hi.")
    assert result == base64.b64encode(payload).decode("utf-8")


async def test_reference_id_without_dir_returns_none(
    httpx_mock: HTTPXMock, base_url: str
) -> None:
    client = IrodoriClient(base_url=base_url, reference_id="alice")
    # No ref_audio_dir set → cannot resolve → None, no HTTP call.
    assert await client.synthesize("Hi.") is None
    assert httpx_mock.get_requests() == []


async def test_reference_id_missing_file_returns_none(
    httpx_mock: HTTPXMock, base_url: str, tmp_path: Path
) -> None:
    client = IrodoriClient(
        base_url=base_url, reference_id="alice", ref_audio_dir=tmp_path
    )
    # No tmp_path/alice/merged_audio.mp3 → None, no HTTP call.
    assert await client.synthesize("Hi.") is None
    assert httpx_mock.get_requests() == []


async def test_includes_expected_form_fields(
    httpx_mock: HTTPXMock, base_url: str
) -> None:
    payload = b"RIFF"
    httpx_mock.add_response(
        method="POST",
        url=f"{base_url}/synthesize",
        content=payload,
        status_code=200,
    )
    client = IrodoriClient(base_url=base_url)
    await client.synthesize("Hello world.")
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 1
    body = reqs[0].content
    # httpx form-encodes spaces as "+"; verify the encoded text + required
    # synthesis parameters are present.
    assert b"text=Hello+world." in body
    assert b"num_steps=40" in body
    assert b"cfg_scale_text=3.0" in body
    assert b"cfg_scale_speaker=5.0" in body
