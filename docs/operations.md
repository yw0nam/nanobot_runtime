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
.venv/bin/python run_gateway.py
```

WS 클라이언트로 메시지 1개 보내고 15초 대기 — 자발 `stream_start →
delta → stream_end` 가 들어오면 OK. (`YURI_...` 대신 워크스페이스 prefix
에 맞춰 해석.)

---

## 3. 로그 / 상태 파일

| 무엇 | 어디 |
|---|---|
| Gateway stdout/stderr (프로덕션) | `run_gateway.py` 띄운 터미널 |
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

nanobot 내장 `websocket` 채널과 `DesktopMateChannel` 모두 동일한 3개 라우트를
노출한다 (DesktopMate 쪽은 `src/nanobot_runtime/channels/desktop_mate_rest.py`
에서 mirror 구현, 전체 키의 `desktop_mate:` prefix 로 자기 세션만 필터링).
인증은 두 채널 모두 `?token=<>` 쿼리 파라미터 또는 `Authorization: Bearer <>`
헤더를 재사용한다.

---

## 4. 확장

### 4.1 새 hook 추가

1. `src/nanobot_runtime/hooks/my_hook.py` 작성 — `AgentHook` 상속.
2. `src/nanobot_runtime/hooks/__init__.py` 에서 re-export.
3. TDD: `tests/test_my_hook.py` 먼저 RED 로 작성.
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

`TTSHook` 을 `_hooks_factory` 에서 빼거나, 프로젝트 차원에서 TTS 를 쓰지
않는다면 `nanobot_runtime/tts/` 의존성 (`fast-bunkai` 등)도 가볍게
쳐낼 수 있다. 다만 `pyproject.toml` 은 현재 tts 모듈을 포함한다.

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

- URL 파라미터 `?tts=0` 또는 inbound `tts_enabled:false` 가 걸려있는지 먼저.
- 게이트웨이 로그에 `IrodoriClient.synthesize: <preview>` 가 찍히면 호출은
  성공. 그 뒤 실패는 swallow 되므로 프레임은 나가되 `audio_base64=null`.
- sink 가 `is_enabled()` False 를 돌려주면 synth task 자체가 생성되지
  않는다 — URL/per-message 오버라이드 확인.

### Dream 이 skill 파일을 예기치 않게 덮어쓴다

- nanobot Dream Phase 2 는 `skills/<kebab-case-name>/` 가 존재하면 skip
  한다. 사용자 수작성 skill 이름이 LLM 이 생성하는 kebab-case 와 같으면
  스킵됨.
- GitStore 가 변경 시 자동 커밋하므로 `git log` 로 복구 가능.

### LTM 저장이 이중으로 들어간다

- `LTMSavingConsolidator` 는 `loop.consolidator.archive` 바인딩만 재지정
  한다. 인스턴스 자체를 재래핑하면 AutoCompact 의 stale reference 때문에
  archive 경로가 두 번 흐를 수 있다 — README invariant 참조.

---

## 6. 관련 문서

- [`setup.md`](./setup.md) — 처음 세팅
- [`../README.md`](../README.md) — 패키지 개요 + 설계 invariants + 새 hook 추가 패턴
- [`../tests/e2e/README.md`](../tests/e2e/README.md) — Live E2E 시나리오 표 + Idle 수동 smoke recipe
