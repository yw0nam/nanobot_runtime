# FAQ — 환경변수 컨벤션

> 모든 환경변수는 **generic prefix** 를 따른다. 워크스페이스명 prefix (e.g. `YURI_*`) 금지.
> 이 문서는 [`CLAUDE.md`](../../CLAUDE.md) 의 entrypoint에서 참조됨.

---

## 1. Prefix 분류

| Prefix | 영역 |
|--------|------|
| `TTS_*` | TTS 파이프라인 |
| `LTM_*` | Long-term memory (mem0) |
| `IDLE_*` | Proactive idle watcher |
| `NANOBOT_*` | Launcher → gateway 진입 |
| 채널별 | nanobot.json `${...}` 치환용 (e.g. `TELEGRAM_BOT_TOKEN`) |

---

## 2. TTS_*

| 변수 | 기본값 | 역할 |
|------|--------|------|
| `TTS_ENABLED` | `"1"` | `"0"` 이면 TTS hook 자체가 등록 안 됨 |
| `TTS_URL` | `http://192.168.0.41:8091` | Irodori TTS 서버 base URL |
| `TTS_REF_AUDIO` | (unset) | 기본 voice의 reference_id (workspace별 voice 클론) |
| `TTS_RULES_PATH` | `<cwd>/resources/tts_rules.yml` | 감정 emoji → keyframes YAML |
| `TTS_MODES_PATH` | `<cwd>/resources/tts_channel_modes.yml` | 채널 → TTS 모드 YAML |
| `TTS_BARRIER_TIMEOUT` | `"30"` | stream_end에서 합성 task 대기 timeout (초) |

→ YAML 누락 시 fail loud (`FileNotFoundError`). silent fallback 없음.

---

## 3. LTM_*

| 변수 | 기본값 | 역할 |
|------|--------|------|
| `LTM_URL` | `http://127.0.0.1:7777/mcp/` | mem0 MCP 서버 URL |
| `LTM_USER_ID` | `"sangjun"` | 사용자 식별자 |
| `LTM_AGENT_ID` | `"yuri"` | 에이전트 식별자 |
| `LTM_TOP_K` | `"5"` | 검색 시 retrieve 개수 |

---

## 4. IDLE_*

| 변수 | 기본값 | 역할 |
|------|--------|------|
| `IDLE_ENABLED` | `"1"` | `"0"` 이면 idle watcher 등록 안 됨 |
| `IDLE_TIMEOUT_S` | `"300"` | idle 임계 (초) |
| `IDLE_COOLDOWN_S` | `"900"` | idle trigger 후 cooldown |
| `IDLE_SCAN_INTERVAL_S` | `"30"` | scan tick |
| `IDLE_STARTUP_GRACE_S` | `"120"` | 부팅 직후 grace period |
| `IDLE_TIMEZONE` | `"Asia/Tokyo"` | quiet hours 기준 |
| `IDLE_QUIET_START` | `"02:00"` | quiet 시작 시각 |
| `IDLE_QUIET_END` | `"07:00"` | quiet 종료 시각 (둘 다 비우면 quiet 비활성) |
| `IDLE_CHANNELS` | `"desktop_mate"` | comma-separated 적용 채널 |

→ `IDLE_ENABLED=1` 인데 `loop.cron_service is None` 이면 부팅 거부 (operator misconfig 잡으려고 fail loud).

---

## 5. NANOBOT_*

| 변수 | 기본값 | 역할 |
|------|--------|------|
| `NANOBOT_CONFIG` | `"./nanobot.json"` | 설정 파일 경로 |
| `NANOBOT_WORKSPACE` | `"."` | 워크스페이스 루트 |

→ launcher가 cwd 기준으로 resolve. `__file__` 기준 X (패키지가 워크스페이스 밖에 있어도 워크스페이스 resources를 가리키게 하기 위함).

---

## 6. 채널별 secrets (nanobot.json `${...}` 치환)

```bash
# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_ALLOW_USER_ID=8281248569

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# (필요한 채널별로)
```

→ launcher가 `dotenv.load_dotenv(Path.cwd() / ".env")` 로 import time에 로드. nanobot config loader가 `${NAME}` 패턴을 환경변수로 치환.

---

## 7. 자주 질문 받는 것

**Q: 워크스페이스명 prefix 못 쓰는 이유?**
A: 두번째 워크스페이스를 추가하려면 그 workspace에서도 같은 prefix가 통해야 함. `YURI_TTS_URL` 이면 새 workspace `aki` 가 어떻게 부르냐? Generic prefix가 multi-tenancy의 ground rule. README "Env var prefix" 섹션 참조.

**Q: 옛날 plan/spec에 `YURI_*` 가 있는데?**
A: PR 분리 historical record. 현 코드에서는 모두 generic으로 마이그레이션 완료 (`feat/tts-channel-gating` PR이 시작점).

**Q: 새 변수 추가 시 어떤 prefix?**
A: 도메인 따라:
- TTS 관련 → `TTS_*`
- 새 hook이면 그 hook 도메인 prefix (예: `MEMORY_*`, `RAG_*`)
- 진짜 cross-cutting이면 `NANOBOT_*` 신중히 (지금은 config/workspace path 만)

**Q: env 변경 후 재시작 필요?**
A: 거의 모든 변수가 import-time / 부팅 시 1회 읽힘. 핫리로드 X. 변경 시 프로세스 재시작.
