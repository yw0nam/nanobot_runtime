"""Fixtures for live-infrastructure E2E tests.

These tests spawn a real nanobot gateway and talk to it over a real
WebSocket. They require an external yuri-style workspace and three
reachable backends (vLLM, Irodori TTS, LTM MCP). When any are
unavailable the fixtures skip so a clean ``pytest`` run stays green.

Environment variables (all optional — sensible defaults apply):

* ``YURI_WORKSPACE`` — absolute path to the workspace to launch. Default:
  ``../yuri`` relative to the nanobot_runtime repo root.
* ``YURI_PYTHON``   — Python interpreter with the workspace venv
  active. Default: ``{workspace}/.venv/bin/python``.
* ``YURI_E2E_WS_PORT`` — port the gateway's WS channel binds to.
  Default: 8765 (matches the workspace config).
* ``YURI_E2E_TIMEOUT`` — seconds to wait for gateway to be ready.
  Default: 20.

Run with:

    pytest tests/e2e/ -m e2e

or individually (tests are not collected by default — see ``pyproject.toml``).
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest


# ── Configuration discovery ──────────────────────────────────────────────────


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_workspace() -> Path | None:
    env = os.getenv("YURI_WORKSPACE")
    if env:
        p = Path(env).resolve()
        return p if p.exists() else None
    # Fallback: sibling yuri/ workspace.
    candidate = _repo_root().parent / "yuri"
    return candidate if candidate.exists() else None


def _resolve_python(workspace: Path) -> Path | None:
    env = os.getenv("YURI_PYTHON")
    if env:
        p = Path(env)
        return p if p.exists() else None
    candidate = workspace / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_reachable(url: str, timeout: float = 2.0) -> bool:
    """Any HTTP response (including 4xx) counts as reachable. Only
    connection failures count as unreachable."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    try:
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # service answered, just not 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def workspace() -> Path:
    ws = _resolve_workspace()
    if ws is None:
        pytest.skip(
            "No yuri workspace found; set YURI_WORKSPACE or create a sibling "
            "`yuri/` directory with nanobot.json + .venv"
        )
    return ws


@pytest.fixture(scope="session")
def workspace_python(workspace: Path) -> Path:
    py = _resolve_python(workspace)
    if py is None:
        pytest.skip(
            f"No Python interpreter at {workspace}/.venv/bin/python — "
            "set YURI_PYTHON or run `uv sync` in the workspace"
        )
    return py


@pytest.fixture(scope="session")
def ws_port() -> int:
    return int(os.getenv("YURI_E2E_WS_PORT", "8765"))


@pytest.fixture(scope="session")
def backends_up(workspace: Path) -> None:
    """Probe the three services the gateway needs. Skip suite if any down."""
    # Parse nanobot.json to find backend URLs.
    import json

    cfg = json.loads((workspace / "nanobot.json").read_text())
    checks: list[tuple[str, bool]] = []

    # vLLM
    api_base = cfg.get("providers", {}).get("vllm", {}).get("apiBase", "")
    if api_base:
        checks.append(("vLLM", _http_reachable(api_base.rstrip("/") + "/models")))

    # Irodori — default from nanobot_runtime.launcher
    tts_url = os.getenv("TTS_URL", "http://192.168.0.41:8091")
    checks.append(("Irodori TTS", _http_reachable(tts_url.rstrip("/") + "/")))

    # LTM MCP
    ltm_url = cfg.get("tools", {}).get("mcpServers", {}).get("ltm", {}).get("url", "")
    if ltm_url:
        checks.append(("LTM MCP", _http_reachable(ltm_url)))

    down = [name for name, ok in checks if not ok]
    if down:
        pytest.skip(f"Backend(s) unreachable, skipping E2E: {', '.join(down)}")


@pytest.fixture(scope="session")
def gateway(
    workspace: Path,
    workspace_python: Path,
    ws_port: int,
    backends_up: None,
) -> "GatewayProcess":
    """Start the gateway once for the whole session, tear down at end."""
    if _tcp_reachable("127.0.0.1", ws_port, timeout=0.5):
        pytest.skip(
            f"Port {ws_port} already bound — another gateway is running. "
            "Stop it before running E2E or set YURI_E2E_WS_PORT."
        )

    log_path = Path("/tmp/yuri_e2e_gateway.log")
    log_path.write_text("")  # truncate

    env = os.environ.copy()
    # Unset VIRTUAL_ENV so uv doesn't complain. Workspace python sets it.
    env.pop("VIRTUAL_ENV", None)

    proc = subprocess.Popen(
        [str(workspace_python), "-m", "nanobot_runtime.launcher"],
        cwd=str(workspace),
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,  # detach so signals don't kill the test process
    )

    timeout = float(os.getenv("YURI_E2E_TIMEOUT", "20"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_reachable("127.0.0.1", ws_port, timeout=0.25):
            break
        if proc.poll() is not None:
            # Gateway exited before binding — dump log and fail.
            pytest.fail(
                f"Gateway died during startup (exit={proc.returncode}). "
                f"Log: {log_path}\n\n{log_path.read_text()[-2000:]}"
            )
        time.sleep(0.25)
    else:
        proc.terminate()
        pytest.fail(
            f"Gateway did not bind port {ws_port} within {timeout}s. "
            f"Log: {log_path}\n\n{log_path.read_text()[-2000:]}"
        )

    gw = GatewayProcess(proc=proc, log_path=log_path, ws_port=ws_port)

    yield gw

    # Teardown: SIGTERM the whole process group to catch Typer's child
    # processes as well. Fall back to SIGKILL if it doesn't exit.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


# ── GatewayProcess helper ────────────────────────────────────────────────────


class GatewayProcess:
    """Thin handle over the running gateway subprocess."""

    def __init__(self, proc: subprocess.Popen, log_path: Path, ws_port: int) -> None:
        self.proc = proc
        self.log_path = log_path
        self.ws_port = ws_port

    def log_snapshot(self) -> str:
        return self.log_path.read_text()

    def log_since(self, marker: float) -> str:
        """Return log content appended after wall-clock ``marker`` (monotonic sec).

        Simple implementation: re-read full file, slice by line timestamps
        when possible. Tests should grab a marker via ``time.monotonic()``
        before kicking off a scenario and pass it back here.
        """
        return self.log_path.read_text()  # Full text — tests slice themselves.

    def count_log_lines(self, substring: str) -> int:
        return sum(1 for ln in self.log_snapshot().splitlines() if substring in ln)


# ── WebSocket helpers ────────────────────────────────────────────────────────


class LiveClient:
    """Minimal async WS client that records every inbound frame."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self.frames: list[dict[str, Any]] = []
        self._reader_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(
        cls,
        port: int,
        *,
        client_id: str,
        tts: str | None = None,
        token: str = "",
        path: str = "/ws",
        max_size: int = 8 * 1024 * 1024,
        open_timeout: float = 5.0,
    ) -> "LiveClient":
        import websockets

        query: list[str] = [f"client_id={client_id}"]
        if tts is not None:
            query.append(f"tts={tts}")
        if token:
            query.append(f"token={token}")
        url = f"ws://127.0.0.1:{port}{path}?" + "&".join(query)

        ws = await websockets.connect(url, open_timeout=open_timeout, max_size=max_size)
        client = cls(ws)
        client._reader_task = asyncio.create_task(client._reader())
        return client

    async def _reader(self) -> None:
        import json

        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    frame = {"_raw": raw[:100]}
                self.frames.append(frame)
        except Exception:
            pass

    async def send_json(self, payload: dict[str, Any]) -> None:
        import json

        await self._ws.send(json.dumps(payload))

    async def wait_for_event(self, event: str, timeout: float = 30.0) -> dict[str, Any]:
        """Block until a frame with ``event == event`` arrives. Raises on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for f in self.frames:
                if f.get("event") == event:
                    return f
            await asyncio.sleep(0.1)
        raise TimeoutError(
            f"Timed out waiting for event={event!r} after {timeout}s. "
            f"Received events: {[f.get('event') for f in self.frames]}"
        )

    async def drain(self, seconds: float) -> None:
        """Keep reading for the given duration — useful to catch post-stream_end
        tts_chunks that arrive via the TTS Barrier race."""
        await asyncio.sleep(seconds)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        await self._ws.close()

    def events(self) -> list[str]:
        return [f.get("event") for f in self.frames if "event" in f]


@pytest.fixture
async def live_client(gateway: GatewayProcess):
    """Per-test WS client. Test must call ``.close()`` explicitly if it wants
    a deterministic teardown, otherwise the fixture cleans up."""
    clients: list[LiveClient] = []

    async def factory(**kwargs: Any) -> LiveClient:
        cli = await LiveClient.connect(gateway.ws_port, **kwargs)
        clients.append(cli)
        return cli

    yield factory

    for cli in clients:
        try:
            await cli.close()
        except Exception:
            pass
