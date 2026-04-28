# FAQ — 작업 패턴 권장사항

> 새 변경 시작/디버깅/upstream 욕구 등 작업 흐름 가이드.
> 이 문서는 [`CLAUDE.md`](../../CLAUDE.md) 의 entrypoint에서 참조됨.

---

## 1. 새 변경 시작 전 체크리스트

1. [`README.md`](../../README.md) "왜 이렇게 되어 있는가" 정독 — "왜?"의 답이 거의 다 있음
2. [`.claude/rules/CODING_RULES.md`](../../.claude/rules/CODING_RULES.md) 훑기 — `from __future__`, `Optional`, `print()` 같은 거 한 번에 거름
3. `docs/superpowers/plans/` 에 진행 중 plan 있는지 확인 — 새 plan 짜기 전에 기존 거 본다
4. `tests/` 에서 비슷한 영역의 기존 테스트 찾기 (TDD 시작점 + 패턴 학습)
5. 환경변수 신규 추가가 필요하면 [env-vars.md](./env-vars.md) prefix 컨벤션 확인

---

## 2. 큰 변경 (channel/sink/hook 추가)

### 2.1 plan 문서 먼저
위치: `docs/superpowers/plans/<YYYY-MM-DD>-<slug>.md`

플랜에 들어가야 할 것:
- Goal + Constraints (사용자 제약)
- File structure (new + modified)
- Task 분해 (각 task 안에 RED → GREEN → REFACTOR)
- 각 task 끝 atomic commit 메시지
- Self-review checklist

참고 예시: `docs/superpowers/plans/2026-04-26-tts-attachment-pipeline-telegram.md` (1560 lines, 9 task)

### 2.2 plan을 critic에 한번 돌리기
- Momus 같은 plan critic agent에 review 받기
- 명확성 / 검증 가능성 / 완전성 부분 보강

### 2.3 task 단위 RED → GREEN → REFACTOR
- Test 먼저 작성 (반드시 fail 확인)
- 최소 구현 (test pass)
- Refactor (test 유지)
- Commit (atomic, 한 task 한 commit)

### 2.4 Verification (CODING_RULES §10)
- E2E test 통과 확인 (`bash scripts/e2e.sh`)
- LSP diagnostics clean
- 변경된 파일에서 `lsp_diagnostics`

---

## 3. 디버깅 패턴

### 3.1 절대 silent fallback 금지
[`pitfalls.md`](./pitfalls.md) §11 참조. 모든 except는 `logger.exception` 로 traceback 보존.

### 3.2 TTS 디버깅
- `TTS_BARRIER_TIMEOUT` 늘려서 타임아웃 함정 배제
- Loguru filter: `loguru.add(..., filter=lambda r: "tts" in r["name"].lower())`
- IrodoriClient HTTP 에러는 자체 logging 있음 (`logger.exception`)
- 합성 실패가 turn 전체에 leak되는지 확인 — None 처리 코드 경로 추적

### 3.3 Hook 디버깅
- `wants_streaming()` 가 True인지 확인 (안 그러면 `on_stream` 안 호출)
- `AgentHookContext.session_key` 값 확인
- Hook 안 raise → `try/except + logger.exception` 로 격리

### 3.4 Channel 디버깅
- nanobot config substitution 작동하는지 — `.env` 의 `${VAR}` 가 nanobot.json에서 치환됐는지
- `ChannelManager._init_channels` 로그 (`{display_name} channel enabled`) 부팅시 확인

---

## 4. Upstream nanobot 수정하고 싶을 때

### 4.1 일단 멈춰라
- nanobot은 `0.1.5.x` 로 pin. version drift 감지 코드까지 있음 (`gateway.py` 헤더)
- 99%의 경우 runtime layer에서 해결 가능

### 4.2 우회 우선순위
1. **Hook으로 가능?** — `AgentHook` lifecycle 메서드로 해결되면 가장 깨끗함. → [hooks.md](./hooks.md)
2. **monkey-patch?** — 기존 패턴 따라가기:
   - `gateway.py::_install_monkey_patch` (AgentLoop)
   - `gateway.py::_install_run_patch` (AgentLoop.run)
   - `gateway.py::_install_channel_manager_patch` (ChannelManager._init_channels)
3. **새 channel/sink 추가?** — runtime layer에서 가능. → [channels.md](./channels.md)
4. **정말 upstream 변경 필요?** — nanobot 자체에 PR 올려라

### 4.3 monkey-patch 작성 시 주의
- deferred import 패턴 (nanobot CLI 모듈을 함수 안에서 import)
- patched 메서드 내부에서 `_orig_xxx` 도 호출해야 정상 동작 유지
- 버전 가드: `nanobot.__version__` 검사로 drift 감지

---

## 5. 워크스페이스 동기화 워크플로우

```
nanobot_runtime (개발 원본, git repo)
        ↓ scripts/sync-to-yuri.sh
yuri/ (git submodule 아님, throwaway)
        ↓ workspace 별로 운영
실 환경
```

`scripts/sync-to-yuri.sh` 가 gate keeper:
- 정식 모드: clean + pushed 검사 후 ff-only
- 긴급 모드: rsync + reminder

자세히는 [`docs/operations.md`](../operations.md) §1 "런타임 업데이트 워크플로우".

---

## 6. 자주 받는 도와줘 요청

**"E2E가 깨졌어요"**:
- `tests/e2e/README.md` 시나리오 매뉴얼 따라가기
- TTS 관련이면 `tts_chunk.sequence` segment reset 패턴 확인 (nanobot pin 버그)
- 채널 모드 YAML 빠졌나 확인

**"새 채널을 추가하고 싶어요"**:
- → [`docs/operations.md` §4.5](../operations.md#45-새-채널--tts-모드-추가)
- + [channels.md](./channels.md)

**"hook이 안 호출돼요"**:
- `wants_streaming()` 반환값 확인
- `_hooks_factory` 에서 빠뜨리지 않았는지
- `TTS_ENABLED=0` 같은 env 가드 켜져있나

**"Telegram에 voice가 안 나가요"**:
- 채널 모드 YAML에 `telegram: attachment` 인지
- `OutboundMessage.media` 에 `.ogg` 확장자 파일 경로 들어갔나
- ATTACHMENT 파이프라인 plan 진행 중인지 확인 (현재 미구현)
- → [tts-pipeline.md](./tts-pipeline.md), [pitfalls.md](./pitfalls.md)

---

## 7. Slow path / 속도 팁

- `pytest -n auto` 로 병렬 (단, fixture에 race 있는 테스트는 제외)
- `pytest --lf` (last-failed) 로 디버그 cycle 단축
- LSP diagnostics를 build 전에 돌려서 type 에러 미리 잡기
- 큰 변경은 작은 PR로 쪼개서 review 부담 분산

---

## 8. PR 작성

- branch: `feat/<slug>` 또는 `fix/<slug>`, base는 `develop`
- title: conventional commits 스타일 (`feat(tts): ...`, `fix(channel): ...`)
- description:
  - Summary (1-3 bullet)
  - Test plan (어떤 테스트 / 어떤 환경에서 검증)
  - 관련 plan/issue 링크
- self-review 한 번 돌리고 push
