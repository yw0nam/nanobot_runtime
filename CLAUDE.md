# CLAUDE.md — Entrypoint

> 본 문서는 **slim entrypoint** 다. LLM agent가 이 레포에 진입할 때 처음 읽어야 할 곳이며,
> 상세 내용은 `docs/faq/*` 와 README, CODING_RULES, operations로 위임한다.

---

## 1. 3-Layer 아키텍처

```
nanobot (upstream PyPI 0.1.5.x)        ← 절대 수정 금지
  AgentLoop, ChannelManager, MessageBus,
  BaseChannel (TelegramChannel, SlackChannel, ...),
  AgentHook(ABC), AgentHookContext

         ▲ monkey-patch + hook 주입

nanobot_runtime (이 레포)               ← 모든 변경은 여기서
  gateway.run() — AgentLoop.__init__ 패치 → hooks 주입
  launcher.main() — .env 로드 + hooks_factory + gateway.run()
  services/{hooks,channels,tts,proactive}/ — 도메인 로직
  clients/ — 외부 HTTP/MCP 클라이언트

         ▲ git clone

workspace (yuri/, ...)                  ← throwaway 인스턴스
  nanobot.json + .env + resources/ + memory/ + sessions/
```

**핵심 분리 원칙**:
- nanobot은 **upstream**. `__version__`이 `0.1.5.x` 아니면 gateway가 부팅 거부 (`gateway.py` 헤더 참조).
- runtime은 **monkey-patch + hooks**로 nanobot을 확장. 새 hook/sink 모두 여기에.
- workspace는 config + state만. 코드 X.

---

## 2. Non-negotiable Invariants

이 다섯 개를 깨면 시스템이 안 돈다:

1. **`nanobot.__version__ == 0.1.5.x`** — drift 감지 가드 있음 (`gateway.py`)
2. **Hooks injected via `AgentLoop.__init__` monkey-patch** — `gateway._install_monkey_patch` 가 `loop._extra_hooks`에 주입
3. **`session_key` format: `<channel>:<chat_id>[:<thread>...]`** — 모든 hook/sink가 prefix로 채널 라우팅
4. **Default paths resolve from `cwd`, not `__file__`** — launcher가 패키지 안에 살아도 workspace의 `resources/`를 가리키게 하기 위함
5. **Generic env var prefix만 사용** — `TTS_*`, `LTM_*`, `IDLE_*`, `NANOBOT_*`. 워크스페이스명 prefix (e.g. `YURI_*`) 금지

---

## 3. Q&A Routing Table

| 질문 | 답이 있는 곳 |
|------|-------------|
| TTS 파이프라인, data flow, sink 추가 | [`docs/faq/tts-pipeline.md`](./docs/faq/tts-pipeline.md) |
| 채널 시스템, 새 채널 등록 메커니즘 | [`docs/faq/channels.md`](./docs/faq/channels.md) |
| Hook lifecycle, 새 hook 작성 | [`docs/faq/hooks.md`](./docs/faq/hooks.md) |
| 자주 틀리는 함정들 (조사 결과) | [`docs/faq/pitfalls.md`](./docs/faq/pitfalls.md) |
| 환경변수 prefix 컨벤션 | [`docs/faq/env-vars.md`](./docs/faq/env-vars.md) |
| 작업 패턴, 디버깅, upstream 수정 욕구 | [`docs/faq/working-patterns.md`](./docs/faq/working-patterns.md) |
| 새 TTS 모드 추가 절차 | [`docs/operations.md`](./docs/operations.md) §4.5 |
| 새 워크스페이스 세팅 | [`docs/setup.md`](./docs/setup.md) |
| 왜 monkey-patch? 왜 path 의존 X? | [`README.md`](./README.md) "왜 이렇게 되어 있는가" |
| 코딩 스타일 (imports, types, logging) | [`.claude/rules/CODING_RULES.md`](./.claude/rules/CODING_RULES.md) |
| 진행 중 큰 변경 plan | [`docs/superpowers/plans/`](./docs/superpowers/plans/) |
| Live E2E 시나리오 | [`tests/e2e/README.md`](./tests/e2e/README.md) |

---

## 4. Reading Order (처음 진입 시)

1. **이 파일** — 아키텍처 + 라우팅
2. **[`README.md`](./README.md)** — 설계 의도 (왜 이렇게?) + invariants
3. **[`.claude/rules/CODING_RULES.md`](./.claude/rules/CODING_RULES.md)** — 스타일 ground rules
4. **`docs/faq/*`** — 작업 영역 관련만 골라 읽기
5. **`docs/superpowers/plans/*`** — 진행 중인 큰 변경 있는지 확인

---

## 5. Top-level 디렉토리 (high-level)

```
src/nanobot_runtime/
├── gateway.py              — monkey-patch 진입 (수정 X 권장)
├── launcher.py             — main() entry
├── services/
│   ├── hooks/{tts,ltm}/   — AgentHook 구현
│   ├── channels/           — DesktopMate WS 채널 + sink
│   ├── tts/                — 채널-비의존 building blocks
│   └── proactive/          — Idle scanner
├── clients/                — HTTP/MCP 클라이언트
├── config/                 — Pydantic 설정 모델
├── models/                 — wire schema
└── core/                   — logger, error_classifier
```

상세 디렉토리 + 파일별 역할은 [`README.md` "레이아웃"](./README.md) 참조.

---

## 6. 변경 이력

| 날짜 | 변경 | 작성자 |
|------|------|--------|
| 2026-04-28 | 초안 (TTS attachment pipeline 조사 반영) | Sisyphus |
| 2026-04-28 | Slim entrypoint로 축소, 상세 docs/faq/로 분리 | Sisyphus |
