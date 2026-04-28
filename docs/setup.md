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
| `wiki` MCP (LLM-free graph + FTS5 search over markdown KB) | wiki MCP 서버 + KB 디렉토리 마운트 | 선택 |
| `TTSHook` (스트리밍 음성 합성) | 사용자가 주입하는 `TTSSynthesizer` 구현체 (Irodori 등) | 선택 |
| `install_idle_system_job` (무대화 N분 후 자발 nudge) | 없음 — nanobot 내장 `CronService` 사용 | 선택 |

LTM / TTS 를 안 쓸 거면 `nanobot_runtime/launcher.py` 의 `_hooks_factory`
에서 해당 블록을 빼거나, 환경변수 (`TTS_ENABLED=0`, `IDLE_ENABLED=0`,
`LTM_URL=`)로 끄면 된다. 각 서비스도 기동할 필요 없음.

---

## 3. 외부 서비스 기동 (선택 기능이 쓰는 것)

### 3.1 LLM provider

`nanobot.json` 의 `providers` 블록이 가리키는 엔드포인트. vLLM 예시:

```bash
# 별도 머신에서 운영 중이라는 전제. 헬스체크:
curl http://<vllm-host>:<port>/v1/models
```

### 3.2 LTM 스택 (optional)

별도 프로젝트로 운영되는 MCP 서버 (Qdrant + Neo4j 백엔드). 셋업·통합
절차는 [`mcp_servers/ltm/docs/workspace-integration.md`](../../mcp_servers/ltm/docs/workspace-integration.md).
요약: `cd mcp_servers/ltm && docker compose up -d && uv run ltm-mcp` →
`http://127.0.0.1:7777/mcp/` (405 응답도 alive).

### 3.3 TTS 서버 (optional)

외부 HTTP 서비스 (`IrodoriClient` 가 기본 구현체). `TTS_ENABLED=1` 일 때
launcher 가 `<workspace>/resources/tts_channel_modes.yml` 부재를 fail-loud
검증한다. 채널-mode 설정·새 채널 추가 절차는
[`operations.md` §4.5](./operations.md#45-새-채널--tts-모드-추가).

### 3.4 Wiki MCP (optional)

별도 프로젝트로 운영되는 MCP 서버 — markdown KB 위 LLM-free graph + FTS5
검색을 12 개 도구로 노출. 셋업·통합 절차는
[`mcp_servers/wiki/docs/workspace-integration.md`](../../mcp_servers/wiki/docs/workspace-integration.md).
요약: `cd mcp_servers/wiki && docker compose up -d` →
`http://127.0.0.1:7778/mcp/`. nanobot 은 등록만으로 도구를 자동 발견하므로
hook 작성 불필요.

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
    "nanobot-ai>=0.1.5.post1",     # path source 금지 (git/PyPI OK)
    "nanobot-runtime",
]

[tool.uv.sources]
nanobot-runtime = { path = "./nanobot_runtime", editable = true }
```

> **Invariant**: `nanobot-ai` 에 `[tool.uv.sources]` 로 **path 의존**을
> 걸지 않는다 — 상대 경로가 consumer 기준으로 꼬여 클론마다 resolve 가
> 깨진다. PyPI 또는 git source (`{ git = "...", branch = "..." }`) 는 OK
> — 실제로 `nanobot_runtime/pyproject.toml` 자체가 `nanobot-ai` 를 git
> source 로 추적해 `develop` 브랜치를 받는다. 상세는
> [`../README.md`](../README.md#왜-이렇게-되어-있는가-non-obvious-한-부분) 참조.

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

### 4.4 Launcher + 환경변수

워크스페이스가 자체 entrypoint 파일을 둘 필요 없음. `nanobot-runtime` 이
`nanobot-launcher` console script (`nanobot_runtime.launcher:main`) 를
제공하므로 `uv sync` 후 바로 실행 가능. LTM + TTS + Idle hook 조립과 env
var 매핑은 `nanobot_runtime/launcher.py` 안에 들어 있음. Launcher 는
import 시점에 `<cwd>/.env` 를 자동 로드한다.

#### 환경변수 (launcher.py 가 실제 읽는 것)

| Group | Var | Default | 용도 |
|---|---|---|---|
| **TTS** | `TTS_ENABLED` | `1` | `0` 으로 끔 |
| | `TTS_URL` | `http://192.168.0.41:8091` | Irodori 서버 |
| | `TTS_REF_AUDIO` | (없음) | 화자 reference id |
| | `TTS_BARRIER_TIMEOUT` | `30` | TTS barrier 초 |
| | `TTS_RULES_PATH` | `<cwd>/resources/tts_rules.yml` | EmotionMapper YAML |
| | `TTS_MODES_PATH` | `<cwd>/resources/tts_channel_modes.yml` | 채널-mode YAML |
| **LTM** | `LTM_URL` | `http://127.0.0.1:7777/mcp/` | MCP endpoint. 빈 값 = 비활성 |
| | `LTM_USER_ID` | `sangjun` | mem0 namespace 키 (영구 고정) |
| | `LTM_AGENT_ID` | `yuri` | mem0 agent scope |
| | `LTM_TOP_K` | `5` | 검색 시 반환 개수 |
| **Idle** | `IDLE_ENABLED` | `1` | `0` 으로 끔 |
| | `IDLE_TIMEOUT_S` | `300` | 무대화 판정 임계 (초) |
| | `IDLE_COOLDOWN_S` | `900` | 같은 세션 nudge 쿨다운 |
| | `IDLE_SCAN_INTERVAL_S` | `30` | 스캐너 tick |
| | `IDLE_STARTUP_GRACE_S` | `120` | 부팅 직후 grace |
| | `IDLE_TIMEZONE` | `Asia/Tokyo` | quiet hours TZ |
| | `IDLE_QUIET_START` | `02:00` | 빈 값으로 quiet 비활성 |
| | `IDLE_QUIET_END` | `07:00` | 빈 값으로 quiet 비활성 |
| | `IDLE_CHANNELS` | `desktop_mate` | 콤마 구분, idle 대상 채널 |
| **Nanobot** | `NANOBOT_CONFIG` | `./nanobot.json` | config path |
| | `NANOBOT_WORKSPACE` | `.` | workspace root |

#### 최소 `.env` 템플릿

```bash
# Channel tokens (nanobot.json 의 ${VAR} 치환용)
TELEGRAM_BOT_TOKEN=...
SLACK_BOT_TOKEN=...
SLACK_APP_TOKEN=...

# LTM (선택 — 비활성하려면 LTM_URL= 으로 비워둠)
LTM_USER_ID=alice               # 한번 정하면 영구
LTM_AGENT_ID=alice
LTM_TOP_K=5

# Idle (선택)
IDLE_ENABLED=0                  # 사용 시 1
IDLE_TIMEZONE=Asia/Tokyo

# Nanobot
NANOBOT_CONFIG=./nanobot.json
NANOBOT_WORKSPACE=.
```

#### Hook 구성 변경

- **on/off, URL/타임아웃**: `.env` 만 수정 → launcher 재시작.
- **채널별 TTS 모드**: `<workspace>/resources/tts_channel_modes.yml` 편집
  → launcher 재시작. 자세한 절차는
  [`operations.md` §4.5`](./operations.md#45-새-채널--tts-모드-추가).
- **다른 hook set 으로 구조 변경**: `nanobot_runtime/launcher.py` 를 fork
  해서 자기 패키지로 복사 + 자체 console script 등록 (`build_ltm_hooks`
  사용법과 `install_idle_system_job` 호출 규약은 그대로).

### 4.5 자동 scaffold

`nanobot-launcher` 최초 기동 시 nanobot 이 워크스페이스에 다음을 자동 생성:

- 템플릿 markdown: `SOUL.md`, `USER.md`, `AGENTS.md`, `HEARTBEAT.md`, `TOOLS.md`
- 작업 디렉토리: `memory/`, `skills/`
- `.git` (Dream GitStore)

`sessions/`, `cron/` 은 nanobot 이 첫 메시지·첫 cron job 시점에 on-demand
로 생성한다. 이후 `SOUL.md` / `USER.md` 를 페르소나에 맞게 편집하면 Dream
cron 이 주기적으로 갱신한다.

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

순서는 달라도 다음 라인들이 찍히면 OK. `nanobot_runtime:` prefix 가 없는
라인은 nanobot 본체에서 출력한다.

```
# nanobot_runtime 출력 (LTM 활성 + IDLE 활성 시)
nanobot_runtime: LTM-saving consolidator installed (user_id=..., agent_id=alice)
nanobot_runtime: injected N hook(s): [...]
Idle watcher installed (timeout=300s cooldown=900s scan=30s channels=['desktop_mate'])

# nanobot 본체 출력
Cron service started with 2 jobs       # dream + idle-watcher
✓ Channels enabled: websocket
```

`injected N hook(s)` 의 N: LTM 활성 시 기본 2 (`LTMInjectionHook` +
`LTMArgumentsHook`), `TTS_ENABLED=1` 이면 +1 = 3. Idle watcher 는 hook 이
아니라 cron job 으로 등록되므로 hook count 에는 포함되지 않는다.

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
