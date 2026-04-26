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

LTM / TTS 를 안 쓸 거면 `nanobot_runtime/launcher.py` 의 `_hooks_factory`
에서 해당 블록을 빼거나, 환경변수 (`TTS_ENABLED=0`, `YURI_IDLE_ENABLED=0`,
`YURI_LTM_URL=`)로 끄면 된다. 각 서비스도 기동할 필요 없음.

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

채널별로 TTS 디스패치 모드(`streaming` / `attachment` / `none`)를 선언
해야 한다 — `<workspace>/resources/tts_channel_modes.yml` 파일에서 관리.
파일이 없으면 `TTS_ENABLED=1` 일 때 launcher 가 fail-loud 한다. 새 채널
추가 절차는 [`operations.md` §4.5](./operations.md#45-새-채널--tts-모드-추가).

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

### 4.4 Launcher

워크스페이스가 자체 entrypoint 파일을 둘 필요 없음. `nanobot-runtime` 이
`nanobot-launcher` console script (`nanobot_runtime.launcher:main`) 를
제공하므로 `uv sync` 후 바로 실행 가능. LTM + TTS + Idle hook 조립과 env
var 매핑은 `nanobot_runtime/launcher.py` 안에 들어 있음.

Env var 은 두 그룹:

- **TTS (워크스페이스 중립)**: `TTS_ENABLED`, `TTS_URL`, `TTS_REF_AUDIO`,
  `TTS_BARRIER_TIMEOUT`, `TTS_RULES_PATH`, `TTS_MODES_PATH`.
- **워크스페이스-identity**: `YURI_LTM_URL`, `YURI_LTM_USER_ID`,
  `YURI_IDLE_ENABLED`, `YURI_IDLE_TIMEOUT_S` 등. 두 번째 워크스페이스가
  생기면 generic prefix 로 마이그레이션 가능 (TTS 가 그 패턴의 선례).

워크스페이스가 hook 구성을 바꾸려면 두 가지 길:

- **간단 케이스 (on/off, URL/타임아웃 변경)**: `.env` 환경변수만 수정.
- **채널별 TTS 모드 변경**: `<workspace>/resources/tts_channel_modes.yml`
  편집 + launcher 재시작 — 자세한 절차는
  [`operations.md` §4.5](./operations.md#45-새-채널--tts-모드-추가).
- **구조 변경 (다른 hook set)**: `nanobot_runtime/launcher.py` 를 fork 해서
  자기 패키지로 복사 + 자체 console script 등록. 이 경우 위쪽 §4 의
  patterns (`build_ltm_hooks` 사용법, `install_idle_system_job` 호출 규약)
  는 그대로 적용.

### 4.5 자동 scaffold

`nanobot-launcher` 최초 기동 시 nanobot 이 워크스페이스에 다음을 자동 생성:

- `SOUL.md` `USER.md` `AGENTS.md` `HEARTBEAT.md` `TOOLS.md` (템플릿)
- `memory/` `sessions/` `skills/` `cron/` (빈 디렉토리)
- `.git` (Dream GitStore)

이후 `SOUL.md` / `USER.md` 를 페르소나에 맞게 편집하면 Dream cron 이
주기적으로 갱신한다.

---

## 5. 설치 + 기동

```bash
# 워크스페이스 루트에서
uv sync                              # nanobot-runtime editable + nanobot-launcher 등록
set -a && source .env && set +a      # nanobot.json 의 ${VAR} 참조용 (Slack 토큰 등)
uv run nanobot-launcher
```

> `.env` 를 export 안 한 채로 기동하면 nanobot 이
> `Environment variable 'SLACK_BOT_TOKEN' referenced in config is not set`
> 같은 메시지로 즉시 종료한다. `nanobot.json` 안에 `${VAR}` 참조가 있는
> 채널/프로바이더는 모두 같은 규칙.

> Editable 설치이므로 `nanobot_runtime/` 안의 코드 수정은 venv 재생성 없이
> 바로 반영된다. `pyproject.toml` 의 `[project.scripts]` 가 바뀐 경우만
> `uv sync` 재실행 필요.

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
