# Setup guide

nanobot 기반 워크스페이스를 처음부터 세팅하는 실행형 체크리스트.
AI 에이전트가 이 문서만 읽고 명령을 실행해가며 완주할 수 있도록
작성돼 있다. 운영·업데이트는 [`operations.md`](./operations.md), 패키지 설계
불변식은 [`../README.md`](../README.md) 참조.

---

## 1. Prerequisites

- Python 3.12 이상
- [`uv`](https://docs.astral.sh/uv/) (권장 — `pyproject.toml` + `uv.lock`
  기반 재현)
- `git`
- `docker compose` — LTM 기능을 쓸 때만
- `curl` — 헬스체크용

---

## 2. 기능 선택 — 어떤 외부 서비스가 필요한가

nanobot-runtime 이 제공하는 선택적 hook 들은 각자 외부 의존이 다르다.
세팅 전에 "이 워크스페이스가 뭘 쓸지" 결정한다.

| Hook / 기능 | 외부 의존 | 필수? |
|---|---|---|
| AgentLoop (대화 루프) | LLM provider (vLLM / OpenAI-compat / Anthropic) | **필수** |
| `build_ltm_hooks` (LTM 읽기·저장·wire-level id 교정) | LTM MCP 서버 + Qdrant + Neo4j | 선택 |
| `TTSHook` (스트리밍 음성 합성) | 사용자가 주입하는 `TTSSynthesizer` 구현체 (Irodori 등) | 선택 |
| `install_idle_system_job` (무대화 N분 후 자발 nudge) | 없음 — nanobot 내장 `CronService` 사용 | 선택 |

LTM / TTS 를 안 쓸 거면 `run_gateway.py` 의 `_hooks_factory` 에서 해당
블록을 빼면 된다. 각 서비스도 기동할 필요 없음.

---

## 3. 외부 서비스 기동 (선택 기능이 쓰는 것)

### 3.1 LLM provider

`nanobot.json` 의 `providers` 블록이 가리키는 엔드포인트. vLLM 예시:

```bash
# 별도 머신에서 운영 중이라는 전제. 헬스체크:
curl http://<vllm-host>:<port>/v1/models
```

### 3.2 LTM 스택 (optional)

LTM MCP 서버는 이 저장소와 별개 프로젝트다. 대표 구현체는
[`yw0nam/mcp_servers/ltm/`](https://github.com/yw0nam) 패턴으로 운영되며,
Qdrant + Neo4j 두 백엔드를 docker compose 로 띄우고 FastMCP 서버가 두
백엔드 앞에 선다.

```bash
# MCP 서버 디렉토리에서
docker compose up -d                  # Qdrant (10002) + Neo4j (10001 bolt)
uv sync --extra dev
cp .env.example .env                  # embedding/LLM 엔드포인트 채움
uv run ltm-mcp                        # http://127.0.0.1:7777/mcp/
```

헬스체크:

```bash
curl http://127.0.0.1:10002/healthz   # Qdrant
curl http://127.0.0.1:7777/mcp/       # 405 응답도 alive 로 본다
```

### 3.3 TTS 서버 (optional)

외부 HTTP 서비스. 프로젝트가 `TTSSynthesizer` Protocol 구현체를 제공한다
(예: 본 레포의 `nanobot_runtime.tts.irodori.IrodoriClient`). 서버가 내려가
있어도 `synthesize()` 는 `None` 을 반환하므로 워크스페이스 자체는 기동
되지만 음성이 끊긴다 — 로그에 실패 라인이 찍힌다.

---

## 4. Workspace 스켈레톤

여기서는 `alice` 라는 워크스페이스를 새로 만든다고 가정한다. 기존
워크스페이스 (`yuri/`) 를 재세팅한다면 이 섹션을 건너뛰고 5 로.

### 4.1 디렉토리 + 런타임 clone

```bash
mkdir alice && cd alice
git clone https://github.com/yw0nam/nanobot_runtime.git
```

### 4.2 `pyproject.toml`

```toml
[project]
name = "alice-workspace"
version = "0.1.0"
description = "Alice nanobot workspace."
requires-python = ">=3.12"

dependencies = [
    "nanobot-ai>=0.1.5.post1",     # PyPI — path 의존 걸지 말 것
    "nanobot-runtime",
]

[tool.uv.sources]
nanobot-runtime = { path = "./nanobot_runtime", editable = true }
```

> **Invariant**: `nanobot-ai` 는 PyPI 에서만 받는다. `[tool.uv.sources]`
> 로 로컬 경로 의존을 걸면 transitive resolve 가 consumer 기준으로 꼬인다.
> 상세는 [`../README.md`](../README.md#왜-이렇게-되어-있는가-non-obvious-한-부분) 참조.

### 4.3 `nanobot.json`

최소 구성 + 내장 WebSocket 채널 + LTM MCP 등록:

```json
{
  "providers": {
    "vllm": {
      "apiKey": "token-abc123",
      "apiBase": "http://192.168.0.41:5535/v1"
    }
  },
  "agents": {
    "defaults": {
      "provider": "vllm",
      "model": "chat_model",
      "unifiedSession": false,
      "idleCompactAfterMinutes": 15,
      "timezone": "Asia/Tokyo"
    }
  },
  "channels": {
    "websocket": {
      "enabled": true,
      "host": "127.0.0.1",
      "port": 8766,
      "allowFrom": ["*"],
      "streaming": true
    }
  },
  "tools": {
    "restrictToWorkspace": true,
    "mcpServers": {
      "ltm": { "url": "http://127.0.0.1:7777/mcp/", "toolTimeout": 60 }
    }
  }
}
```

주의사항:

- **Port 충돌**: 워크스페이스마다 `channels.websocket.port` 를 다르게
  준다 (`yuri` 레퍼런스 배포는 8765).
- **`allowFrom: []` = deny-all**: nanobot `ChannelManager` 가 빈 배열을
  "아무도 허용 안 함" 으로 해석해 SystemExit 한다. 반드시 `["*"]` 또는
  구체 client_id 리스트.
- **`${VAR}` 치환**: nanobot 은 `Nanobot.__init__` 경로에서 환경변수를
  치환한다. `nanobot_runtime.gateway.run()` 이 이 경로를 타므로 별다른
  세팅 없이 `${VLLM_API_KEY}` 같은 패턴을 써도 된다.
- **LTM 비활성 시**: `tools.mcpServers.ltm` 블록을 통째로 지우고
  `_hooks_factory` 에서 `build_ltm_hooks` 호출을 제거.

### 4.4 `run_gateway.py`

LTM + Idle 을 켜는 최소 템플릿:

```python
"""Entrypoint: launch the alice nanobot gateway with LTM + Idle hooks."""
from __future__ import annotations

import os
import sys

from loguru import logger
from nanobot.agent.hook import AgentHook
from nanobot.agent.loop import AgentLoop
from nanobot_runtime.gateway import run
from nanobot_runtime.hooks import build_ltm_hooks
from nanobot_runtime.proactive import IdleConfig, QuietHours, install_idle_system_job


def _build_idle_config() -> IdleConfig:
    start = os.getenv("ALICE_IDLE_QUIET_START", "02:00")
    end = os.getenv("ALICE_IDLE_QUIET_END", "07:00")
    quiet = QuietHours(start=start, end=end) if start and end else None
    channels = tuple(
        c.strip()
        for c in os.getenv("ALICE_IDLE_CHANNELS", "websocket").split(",")
        if c.strip()
    )
    return IdleConfig(
        enabled=os.getenv("ALICE_IDLE_ENABLED", "1") != "0",
        idle_timeout_s=int(os.getenv("ALICE_IDLE_TIMEOUT_S", "300")),
        cooldown_s=int(os.getenv("ALICE_IDLE_COOLDOWN_S", "900")),
        scan_interval_s=int(os.getenv("ALICE_IDLE_SCAN_INTERVAL_S", "30")),
        quiet_hours=quiet,
        timezone=os.getenv("ALICE_IDLE_TIMEZONE", "Asia/Tokyo"),
        channels=channels,
    )


def _hooks_factory(loop: AgentLoop) -> list[AgentHook]:
    hooks: list[AgentHook] = list(
        build_ltm_hooks(
            loop,
            user_id=os.getenv("ALICE_LTM_USER_ID", "default_user"),
            agent_id=os.getenv("ALICE_LTM_AGENT_ID", "alice"),
            ltm_url=os.getenv("ALICE_LTM_URL", "http://127.0.0.1:7777/mcp/"),
            top_k=int(os.getenv("ALICE_LTM_TOP_K", "5")),
        )
    )

    idle = _build_idle_config()
    if idle.enabled:
        if loop.cron_service is None:
            logger.warning("Idle watcher requested but AgentLoop has no cron_service — skipping")
        else:
            install_idle_system_job(
                agent=loop, sessions=loop.sessions, cron=loop.cron_service, config=idle,
            )

    return hooks


def main() -> None:
    run(
        hooks_factory=_hooks_factory,
        config_path=os.getenv("ALICE_NANOBOT_CONFIG", "./nanobot.json"),
        workspace=os.getenv("ALICE_WORKSPACE", "."),
    )


if __name__ == "__main__":
    sys.exit(main())
```

환경변수 prefix (`ALICE_...`) 를 워크스페이스 이름 기준으로 충돌 방지.

**TTS 를 추가하려면**: `_hooks_factory` 안에서 `TTSHook` 을 조립해
`hooks.append(...)` 한다. 조립 패턴은 본 레포의 `tests/` 에 있는 TTSHook
관련 예제 + `DesktopMateChannel` 기반 배포 (yuri) 를 참고.

### 4.5 자동 scaffold

`run_gateway.py` 최초 기동 시 nanobot 이 워크스페이스에 다음을 자동 생성:

- `SOUL.md` `USER.md` `AGENTS.md` `HEARTBEAT.md` `TOOLS.md` (템플릿)
- `memory/` `sessions/` `skills/` `cron/` (빈 디렉토리)
- `.git` (Dream GitStore)

이후 `SOUL.md` / `USER.md` 를 페르소나에 맞게 편집하면 Dream cron 이
주기적으로 갱신한다.

---

## 5. 설치 + 기동

```bash
uv sync
uv run python run_gateway.py
```

### 5.1 정상 기동 로그

순서는 달라도 다음 라인들이 찍히면 OK:

```
nanobot_runtime: LTM-saving consolidator installed (user_id=..., agent_id=alice)
nanobot_runtime: injected 2 hook(s): ['LTMInjectionHook', 'LTMArgumentsHook']
Idle watcher installed (timeout=300s cooldown=900s scan=30s channels=['websocket'])
Cron service started with 2 jobs       # dream + idle-watcher
✓ Channels enabled: websocket
```

FE 가 붙지 않아도 기동은 되며 WS 포트가 열린다.

### 5.2 수동 WS 프로브

```bash
# nanobot 내장 WS 채널 프로토콜 기준 (기본 설정 시)
python - <<'PY'
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:8766/?client_id=probe") as ws:
        await ws.send(json.dumps({"type": "new_chat", "content": "hello"}))
        async for msg in ws:
            print(json.loads(msg))
asyncio.run(main())
PY
```

`ready → message`/`delta` 류 이벤트가 들어오면 pipeline 이 전부 돈다.

---

## 6. 다음 단계

- [`operations.md`](./operations.md) — 런타임 업데이트 (`sync-to-yuri.sh`),
  테스트, 로그 위치, 흔한 trouble-shooting.
- 본 레포 [`../README.md`](../README.md#새-hook-추가-가이드) — 새 hook 을 레포에 추가하는 패턴.
- 자기 워크스페이스의 `cron/README.md` — Schedule/Cron 배포 레시피
  (워크스페이스별로 관리).
