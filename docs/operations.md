# Operations guide

세팅 완료 후 일상 운영 — 런타임 업데이트, 테스트, 로그 읽기, 흔한
트러블슈팅. 초기 세팅은 [`setup.md`](./setup.md), 설계 invariants 는
[`../README.md`](../README.md).

---

## 1. 런타임 업데이트 워크플로우

개발 원본은 **본 레포** (`nanobot_runtime/` 원본). 각 워크스페이스는
자기 디렉토리에 `git clone` 한 별개의 사본을 쓴다. 원본을 고치면 클론
쪽에도 반영해야 한다.

### 정식 경로

```bash
# 1) 원본에서 개발 + 테스트 + 커밋
cd <parent>/nanobot_runtime
uv run pytest                    # unit + integration (e2e 는 marker deselect)
git add -A && git commit -m "..."
git push

# 2) 워크스페이스 클론을 fast-forward
./scripts/sync-to-yuri.sh        # parent 가 clean + pushed 인지 검사 후 merge ff
# 다른 클론 경로를 지정하려면
WORKSPACE_CLONE=<path>/nanobot_runtime ./scripts/sync-to-yuri.sh
```

`sync-to-yuri.sh` 의 기본 모드는 parent 에 커밋 안 된 변경이나 push 안
된 커밋이 있으면 거부한다. 이 게이트가 "로컬만 수정하고 잊어버리기"
패턴을 막는다.

### 긴급 경로 (rsync)

원본을 아직 커밋 못 하는 상태에서 스모크 검증을 돌려야 할 때:

```bash
./scripts/sync-to-yuri.sh --quick
```

`src/` + `tests/` 를 rsync 한 뒤 parent git status 를 출력해 **commit +
push 를 상기시킨다**. 이 단계를 누락하면 다음 번 `git pull` 이 "로컬
수정 vs incoming 같은 내용" 충돌을 일으킨다 — rsync 된 untracked 파일
과 pull 받는 파일이 충돌.

복구 순서 (충돌이 이미 발생했다면):

```bash
cd <workspace>/nanobot_runtime
# 1. rsync 된 untracked 파일들이 parent 와 동일한지 확인
for f in <충돌 파일들>; do diff -q <parent>/$f ./$f; done
# 2. 동일하면 삭제 + modified 는 git checkout --
rm <untracked 파일들>
git checkout -- <modified 파일들>
# 3. 다시 pull
git pull --ff-only
```

이 레시피는 한 번 실전 검증됨 (Phase 5 Idle Watcher 최초 sync 시).

---

## 2. 테스트

```bash
cd <parent>/nanobot_runtime

uv run pytest                              # 기본 (unit + 몇몇 fast 통합)
uv run pytest tests/proactive              # Phase 5-A Idle 17 cases
uv run pytest -m integration               # live LTM MCP 필요
uv run pytest -m e2e tests/e2e/            # live-infra E2E (세 백엔드 필요)
```

E2E marker 는 `pyproject.toml` 의 `addopts = "-m 'not integration and not e2e'"`
로 기본 deselect — 의도적. live 서비스 의존성이 없을 때는 자동 skip
하도록 fixture 가 백엔드 probe 를 먼저 한다.

### Idle watcher 수동 smoke

`tests/e2e/README.md` 의 **"Manual smoke: Phase 5 Idle Watcher"** 섹션
참조. 요약:

```bash
cd <workspace>

# 짧은 idle 환경변수로 게이트웨이 재기동
YURI_IDLE_TIMEOUT_S=10 \
YURI_IDLE_SCAN_INTERVAL_S=3 \
YURI_IDLE_COOLDOWN_S=60 \
YURI_IDLE_QUIET_START= YURI_IDLE_QUIET_END= \
.venv/bin/nanobot-launcher
```

WS 클라이언트로 메시지 1개 보내고 15초 대기 — 자발 `stream_start →
delta → stream_end` 가 들어오면 OK. (`YURI_...` 대신 워크스페이스 prefix
에 맞춰 해석.)

---

## 3. 로그 / 상태 파일

| 무엇 | 어디 |
|---|---|
| Gateway stdout/stderr (프로덕션) | `nanobot-launcher` 띄운 터미널 |
| E2E 서브프로세스 로그 | `/tmp/yuri_e2e_gateway.log` |
| 대화 세션 히스토리 | `<workspace>/sessions/<safe_key>.jsonl` (첫 줄 metadata, 이후 message 단위) |
| LTM persistent | Qdrant 볼륨 (`ltm_qdrant` docker volume) + Neo4j 컨테이너 내부 |
| Dream 결과물 | `<workspace>/SOUL.md` `USER.md` `memory/MEMORY.md` `skills/*/` |
| Cron state | `<workspace>/cron/jobs.json` + `cron/action.jsonl` |

### 세션 REST API

기본 gateway HTTP 포트에서:

```bash
curl http://127.0.0.1:<gateway-port>/api/sessions

# 특정 세션 히스토리
curl http://127.0.0.1:<gateway-port>/api/sessions/<channel>%3A<chat_id>/messages

# 세션 삭제
curl -X POST http://127.0.0.1:<gateway-port>/api/sessions/<channel>%3A<chat_id>/delete
```

세션 키 포맷: `channel:chat_id` (URL 인코딩 시 `%3A`).

### 이미지 첨부 (inbound `images`)

`new_chat` / `message` 프레임에 `images: list[str]` 필드를 실어 멀티모달
입력을 보낼 수 있다. 각 항목은 반드시 `data:<mime>;base64,<payload>`
형식의 data URL (원시 base64 문자열 불가). 캡: 프레임당 최대 4장,
장당 디코드 후 10MB. 허용 MIME 은 `image/png`, `image/jpeg`,
`image/webp`, `image/gif` — SVG 는 XSS 리스크로 차단된다. 검증 실패 시
채널은 turn 전체를 거부하고 `{"event": "image_rejected", "reason":
<reason>, "reference_id": <echo>}` 프레임을 돌려준다. `reason` 은 다음
다섯 토큰 중 하나로 고정된다:

- `malformed` — data URL 파싱 실패 / base64 복호 실패 (caller-fixable)
- `too_large` — 디코드 후 장당 10MB 초과
- `unsupported_mime` — allow-list 외 MIME (SVG 포함)
- `too_many` — 한 프레임에 4장 초과 동봉
- `io_error` — 서버 측 저장 실패 (디스크 풀, 권한). 사용자 입력 문제가
  아니므로 FE 는 일시적 오류로 취급해 재시도를 권장한다.

`reference_id` 는 인바운드 envelope 의 필드를 그대로 에코하므로, FE 는
`new_chat` 거부 (아직 `chat_id` 미할당) 경우에도 어떤 in-flight send 에
대한 거부인지 매칭할 수 있다. 디코드된 파일은 nanobot 의
`get_media_dir("desktop_mate")` 아래에 저장되고, 로컬 경로가
`BaseChannel._handle_message(..., media=...)` 로 전달되어 이후 멀티모달
변환은 nanobot 측이 자동 처리한다. 다운스트림 전달 도중 예외가 나면
채널이 디코드된 미디어를 unlink 하므로 rejected turn 의 파일이 디스크에
잔류하지 않는다.
nanobot 내장 `websocket` 채널과 `DesktopMateChannel` 모두 동일한 3개 라우트를
노출한다 (DesktopMate 쪽은 `src/nanobot_runtime/clients/desktop_mate_rest.py`
에서 mirror 구현, 전체 키의 `desktop_mate:` prefix 로 자기 세션만 필터링).
인증은 두 채널 모두 `?token=<>` 쿼리 파라미터 또는 `Authorization: Bearer <>`
헤더를 재사용한다.

---

## 4. 확장

### 4.1 새 hook 추가

1. `src/nanobot_runtime/services/hooks/my_hook.py` 작성 — `AgentHook` 상속.
   복수 파일로 나눠야 하면 LTM 처럼 `services/hooks/my_hook/` 서브패키지로.
2. `src/nanobot_runtime/services/hooks/__init__.py` 에서 re-export.
3. TDD: `tests/services/hooks/test_my_hook.py` 먼저 RED 로 작성 (test 트리는
   `src/` 와 디렉토리 구조를 미러링).
4. 공통 조립이 필요하면 `build_ltm_hooks` 옆에 형제 factory 추가.

### 4.2 Schedule / Cron 추가

nanobot 내장 `CronService` 로 충분 — 이 레포에 별도 코드 없다. 워크
스페이스 쪽 `cron/README.md` 를 해당 배포의 레시피 문서로 쓴다
(`deliver=true` 로 채널 푸시, `deliver=false` 로 silent 백그라운드).

### 4.3 Idle 제외·활성화

`<PREFIX>_IDLE_ENABLED=0` 로 완전 끄거나, `install_idle_system_job` 호출
자체를 `_hooks_factory` 에서 제거한다. cron_service 는 여전히 살아 있어
다른 schedule 이 등록 가능.

### 4.4 TTS 제외

`TTS_ENABLED=0` 으로 끄거나 `TTSHook` 을 `_hooks_factory` 에서 빼거나,
프로젝트 차원에서 TTS 를 쓰지 않는다면 `nanobot_runtime/services/tts/`
의존성 (`fast-bunkai` 등)도 가볍게 쳐낼 수 있다. 다만 `pyproject.toml`
은 현재 tts 모듈을 포함한다. 채널별로 끄는 것 (Slack 만 텍스트로) 은
§4.5 모드 게이트로 해결한다.

### 4.5 새 채널 / TTS 모드 추가

새 메신저 채널(예: WhatsApp, Discord)을 추가하는 작업은 **runtime쪽 게이팅**
과 **nanobot upstream 의 채널 구현** 두 단계로 나뉜다. 단계 별로 비용이
크게 다르므로 분리해서 본다.

#### (1) 채널이 nanobot 에 이미 있다면 — YAML 한 줄

`<workspace>/resources/tts_channel_modes.yml` 에 추가하고 launcher 재시작:

```yaml
default: none
channels:
  desktop_mate: streaming
  telegram: attachment
  slack: none
  discord: none         # 신규: 텍스트만 응답
  whatsapp: attachment  # 신규: 음성노트 모드 선언 (실제 동작은 §3 참고)
```

YAML 의 channel key 는 nanobot 이 만드는 `session_key` 의 prefix 와 일치
해야 한다 — `_channel_from_session_key` 가 첫 콜론 앞부분을 잘라쓰기
때문 (`slack:C123:T456` → `"slack"`).

빠뜨린 모드 typo 는 boot 시 `ValueError` 로 죽고, 미선언 채널은
`default:` 로 폴백한다. ATTACHMENT 모드를 선언하면 boot 시 WARNING 이
뜬다 (다음 항목 참고).

#### (2) nanobot 에 채널이 없다면 — upstream 작업이 선행

이 레포는 **runtime 쪽 게이팅**만 다룬다. 새 메신저 자체는 upstream
(`<parent>/nanobot/nanobot/channels/`) 에서 구현해야 한다. 최소 요구사항:

- `BaseChannel` 서브클래스 (`slack.py`, `telegram.py` 참고)
- 인바운드 메시지 발행 시 `session_key=f"<channel>:<chat_id>:..."` 형태로
  prefix 컨벤션 준수
- `nanobot.json` 의 `channels` 섹션에 등록

이 작업 없이 `tts_channel_modes.yml` 에만 등록하면 그 채널은 메시지를
받지조차 못한다. 게이트는 **존재하지 않는 채널을 차단할 뿐** 만들어
주지 않는다.

#### (3) 모드별 실제 동작

| 모드 | 현재 상태 | 추가 작업 |
|---|---|---|
| `none` | 즉시 동작. Slack / Discord 같은 텍스트-only 채널. | 없음 |
| `streaming` | DesktopMate 한정 동작. `LazyChannelTTSSink` 가 desktop_mate 싱글톤에 결합 (spec §5.3). | DesktopMate 가 아닌 다른 streaming 타깃(Unity overlay 등)이 필요하면 새 sink 클래스 + launcher 분기 필요 |
| `attachment` | **데이터 모델만 있고 파이프라인 미구현.** boot 시 WARNING + 실제는 silent NONE. | `AttachmentTTSHook` + 채널-side voice note uploader 필요 (별도 PR). |

#### (4) WhatsApp 구체 시나리오

- WhatsApp Business API 의 `audio` 메시지 타입을 쓸 거면 → `attachment`.
  단, 파이프라인이 아직 없으니 등록만 하면 텍스트만 응답 + boot warning.
  attachment 파이프라인 PR 이 별도로 필요.
- 텍스트만 보낼 거면 → `none`. 즉시 동작.
- WhatsApp Business API 자체가 nanobot 에 없으면 (2) 가 선행.

설계 디테일은 `docs/superpowers/specs/2026-04-26-tts-channel-gating-design.md`.

---

## 5. Troubleshooting

### Gateway 가 버전 assertion 에서 죽는다

```
RuntimeError: nanobot_runtime.gateway: unsupported nanobot version 0.2.0; validated against ('0.1.5',)
```

- `nanobot_runtime/gateway.py` 의 `_SUPPORTED_PREFIXES` 재검증이 먼저.
- private API 접촉 지점 세 군데 — `AgentLoop.__init__` 서명, `_extra_hooks`
  리스트, `_session_locks` 딕셔너리 — 가 upstream 에서 유지되는지 확인한
  뒤 pin 을 갱신.

### WS 포트가 이미 점유됨

```bash
lsof -iTCP:<port> -sTCP:LISTEN
```

- 이전 게이트웨이 프로세스 잔존. kill 후 재기동.
- E2E 는 `YURI_E2E_WS_PORT` 로 override 가능.

### Idle nudge 가 안 터진다

확인 순서:
1. `<PREFIX>_IDLE_ENABLED=1` 인가.
2. 현재 시각이 `quiet_hours` 밖인가.
3. 같은 세션에 cooldown (기본 900s) 이 걸려있나 — 로그에서
   `Idle nudge delivered: session=... idle=Xm` 이 최근에 한 번
   찍혔는지 확인.
4. `<PREFIX>_IDLE_TIMEOUT_S` 가 충분히 짧은가 — 테스트 중이면 10–30s 로.
5. 세션이 `<PREFIX>_IDLE_CHANNELS` allowlist 에 있는 채널인가.
6. 해당 세션의 per-session `asyncio.Lock` 이 잡혀 있는가 (다른 처리 중).

### LTM 검색 결과가 비어있다

- `<PREFIX>_LTM_USER_ID` 가 과거 저장 때와 다르면 mem0 네임스페이스
  격리로 0건이 나온다. `LTMArgumentsHook` 이 wire level 에서 user_id 를
  override 하므로 **고정 값을 쓰는 게 중요**.
- Qdrant 볼륨이 초기화됐는지 확인: `docker volume ls | grep ltm`,
  `docker volume inspect <볼륨명>`.
- LTM MCP 서버 자체가 살아있는지: `curl http://127.0.0.1:7777/mcp/`.

### TTS 청크가 안 온다

- 채널이 `tts_channel_modes.yml` 에서 `streaming` 으로 선언돼 있는지 먼저.
  텍스트-only 채널(slack/discord/...) 은 mode 게이트가 dispatch 단계에서
  short-circuit 한다 — synth 호출 자체가 안 일어나므로 로그에 아무것도
  안 찍힌다. 신규 채널을 추가했는데 음성이 안 나오면 §4.5 참고.
- URL 파라미터 `?tts=0` 또는 inbound `tts_enabled:false` 가 걸려있는지.
- 게이트웨이 로그에 `IrodoriClient.synthesize: <preview>` 가 찍히면 호출은
  성공. 그 뒤 실패는 swallow 되므로 프레임은 나가되 `audio_base64=null`.
- "DesktopMateChannel not registered yet" WARNING 이 한 번 떴다면 첫
  메시지 디스패치 시점에 채널이 아직 등록되지 않은 것. 보통 두 번째
  메시지부터 정상화되며 일회성. 매번 뜬다면 채널 기동 자체가 실패하는
  것이므로 별도 진단 필요.

### Dream 이 skill 파일을 예기치 않게 덮어쓴다

- nanobot Dream Phase 2 는 `skills/<kebab-case-name>/` 가 존재하면 skip
  한다. 사용자 수작성 skill 이름이 LLM 이 생성하는 kebab-case 와 같으면
  스킵됨.
- GitStore 가 변경 시 자동 커밋하므로 `git log` 로 복구 가능.

### LTM 저장이 이중으로 들어간다

- `LTMSavingConsolidator` 는 `loop.consolidator.archive` 바인딩만 재지정
  한다. 인스턴스 자체를 재래핑하면 AutoCompact 의 stale reference 때문에
  archive 경로가 두 번 흐를 수 있다 — README invariant 참조.

### E2E 가 startup 직후 죽는다 (`Environment variable 'X' is not set`)

- E2E fixture 는 `os.environ.copy()` 만 자식 프로세스에 넘긴다 — `.env`
  를 자동 로드하지 않는다. 워크스페이스 루트에서
  `set -a && source .env && set +a` 후 pytest 를 다시 실행한다. 누락 변수는
  보통 `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` (DesktopMate-only 워크스페이스라
  쓰지 않더라도 `nanobot.json` 의 `channels.slack.enabled=true` 에서 참조).

### E2E 가 "no yuri workspace found" 로 전부 skip 된다

- `tests/e2e/conftest.py` 의 default workspace 탐색은 `<repo>/../yuri` 를
  찾는다. `nanobot_runtime/` 가 워크스페이스 *안*에 nested 되어 있으면
  (로컬 dev 의 일반적인 구조) 이 default 가 안 맞는다. 워크스페이스
  루트에서 `YURI_WORKSPACE=$PWD` 명시 후 실행.

### gateway 로그에 `send_delta: no connection for chat_id=…`

- 채널이 outbound delta 를 보내려는데 그 chat_id 에 active WS 연결이
  없다는 뜻. 실제 운영 중에는 FE 가 reconnect 하기 전에 agent 가
  proactive nudge 를 보냈거나, FE 가 read-only 로 닫고 떠난 경우.
- E2E 에서 자주 만나는 패턴: 테스트가 너무 일찍 close 했거나
  intermediate `stream_end` 에 의존해서 turn 종료를 잘못 판단한 경우.
  README invariant ("intermediate empty stream_end 는 wire 에 안 나간다")
  와 합쳐서 본다 — 채널이 내려보내지 않으면 FE 도 그걸 보고 close
  하지 않을 것.

### TTS chunk sequence 가 `[0, 1, 2, 0]` 처럼 0 으로 다시 시작한다

- 버그 아님 (현재 nanobot pin 한정). `TTSHook._SessionState` 는 코드상
  `on_stream_end(resuming=False)` 에서만 sequence 를 0 으로 리셋해야 하지만,
  현 nanobot 버전이 multi-iteration turn 의 매 iteration 끝에서
  `resuming=False` 를 emit 하고 있어 segment 단위 reset 이 wire 에 그대로
  관찰된다. FE / 테스트는 0 를 새 segment 의 시작으로 간주하고 segment
  단위 monotonicity 만 검증한다 (테스트 C 의 assertion 패턴 참조).
  nanobot 의 `resuming` semantics 가 의도대로 (true=중간, false=종료)
  돌아오면 single-segment 로 회귀할 수 있다 — 그때 테스트는 자동으로
  통과 (segment 1개여도 monotonic 만족).

---

## 6. 관련 문서

- [`setup.md`](./setup.md) — 처음 세팅
- [`../README.md`](../README.md) — 패키지 개요 + 설계 invariants + 새 hook 추가 패턴
- [`../tests/e2e/README.md`](../tests/e2e/README.md) — Live E2E 시나리오 표 + Idle 수동 smoke recipe
