# nanobot_runtime

여러 nanobot workspace 가 공유하는 런타임 글루 — AgentLoop hook 구현체,
gateway 런처(monkey-patch 방식), LTM(mem0) 연동 유틸, proactive idle
watcher. 개발은 이 저장소에서 하고, 각 workspace 는 `git clone` 해서 쓴다.

## 문서

- [`docs/setup.md`](./docs/setup.md) — 처음부터 끝까지 세팅 가이드 (Prerequisites → 외부 서비스 → workspace skeleton → 기동)
- [`docs/operations.md`](./docs/operations.md) — 운영 (업데이트 sync, 테스트, 로그, troubleshooting)
- [`tests/e2e/README.md`](./tests/e2e/README.md) — Live E2E 시나리오 + Idle 수동 smoke recipe

이 README 는 **패키지 구조·invariants·개발 워크플로우** 를 다루며, 실제
세팅 절차는 위 두 문서를 참고.

## 레이아웃

```
nanobot_runtime/
├── src/nanobot_runtime/
│   ├── gateway.py              # run(hooks_factory=...) — Typer app 으로 dispatch
│   ├── hooks/
│   │   ├── ltm_args.py         # LTMArgumentsHook (wire-level id 교정)
│   │   ├── ltm_client.py       # LTMMCPClient (FastMCP 어댑터)
│   │   ├── ltm_consolidator.py # LTMSavingConsolidator + install_ltm_saving
│   │   ├── ltm_injection.py    # LTMInjectionHook (before_iteration 검색·주입)
│   │   ├── tts.py              # TTSHook (스트리밍 음성 합성)
│   │   └── __init__.py         # build_ltm_hooks factory
│   ├── proactive/
│   │   └── idle.py             # IdleScanner + install_idle_system_job
│   ├── channels/
│   │   └── desktop_mate.py     # DesktopMate WS 채널 (예시 채널)
│   └── tts/                    # SentenceChunker / Preprocessor / EmotionMapper / IrodoriClient
├── tests/                      # pytest, `uv run pytest`
├── scripts/sync-to-yuri.sh     # parent → workspace clone 동기화
├── docs/                       # setup.md, operations.md
└── pyproject.toml              # nanobot-ai 는 PyPI, path 의존 없음
```

새 워크스페이스 만들기 → [`docs/setup.md`](./docs/setup.md).

## 왜 이렇게 되어 있는가 (non-obvious 한 부분)

- **`nanobot-ai` 는 PyPI 에서만 받는다.** 과거에 `[tool.uv.sources]` 로
  `../nanobot` 경로 의존을 걸면 클론이 다른 디렉토리 레이아웃에서 전혀
  resolve 되지 않는다 (상대경로가 consumer 기준으로 꼬임). `nanobot-ai`
  는 PyPI 에 publish 되어 있으므로 경로 의존을 걸 이유가 없다.

- **Gateway 진입은 Typer `app` 을 통해 dispatch** (`_run_gateway` 같은
  private 함수 쓰지 않음). PyPI 0.1.5.post1 에서는 `_run_gateway` 가
  없다 — `@app.command()` 로 감싼 `gateway(...)` 가 유일한 공식 경로다.
  그래서 `gateway.py::run()` 은 `app(args=["gateway", "--config", ...,
  "--workspace", ...], standalone_mode=False)` 로 호출한다.

- **Hook 주입은 `AgentLoop.__init__` monkey-patch.** nanobot CLI 게이트웨이는
  hook injection point 를 공개하지 않으므로, `run(hooks_factory=...)` 가
  `AgentLoop.__init__` 래핑 후 `self._extra_hooks` 에 append 한다. 같은
  프로세스이므로 CLI 가 생성하는 AgentLoop 도 패치가 적용된다.

- **`LTMSavingConsolidator` 는 인스턴스 교체가 아니라 in-place 바인딩 재지정.**
  `install_ltm_saving()` 이 `loop.consolidator.archive` 바인딩만 덮어써서
  AutoCompact (외부 호출) 와 `Consolidator.maybe_consolidate_by_tokens`
  (내부 `self.archive` 호출) 양쪽 모두 자동으로 LTM 저장으로 흐른다.
  Consolidator 인스턴스 자체를 re-wrap 하면 AutoCompact 의 stale reference
  때문에 한쪽 경로가 누락된다.

- **버전 Pin.** `gateway.py` 는 `nanobot.__version__` 이 `0.1.5.x` 가
  아니면 기동을 거부한다. Private API 사용부가 있어서 drift 감지 필수.

- **Workspace launcher 는 패키지 안에 들어 있다.** 워크스페이스가 자체
  `run_gateway.py` 를 두지 않고 `nanobot-launcher` console script
  (`nanobot_runtime.launcher:main`) 를 그대로 쓴다. 덕분에 워크스페이스
  디렉토리는 `nanobot.json` + `.env` + `resources/` 만 들고 있는 throwaway
  세팅으로 유지 가능. Launcher 안의 모든 default path (`tts_rules.yml` 등)
  는 `__file__` 이 아니라 **cwd 기준** 으로 resolve 한다 — 그래야 패키지
  안에 살면서도 워크스페이스의 `resources/` 를 가리킬 수 있다. 하드코딩된
  `YURI_*` env var prefix 는 첫 워크스페이스 명을 따른 잔재 (두 번째
  워크스페이스 도입 시 generic prefix 로 마이그레이션 예정).

- **Channel 은 nanobot 의 매 iteration end 를 wire 의 `stream_end` 로
  번역하지 않는다.** Tool-call hop 마다 nanobot 은 빈 content 의
  `OutboundMessage` (`_tool_hint` / `_tool_events` 메타) 를, 그리고 새
  stream_id + 빈 delta + `_stream_end` 로 마킹된 `send_delta` 를 발생시킨다.
  여기에 그대로 `stream_end` 프레임을 흘리면 FE / E2E 가 "turn 완료"
  로 오해해서 reply 도착 전에 read 를 멈춘다. `DesktopMateChannel.send` /
  `send_delta` 는 (1) `_stream_end:True` 가 명시되지 않은 `send` 호출과
  (2) **새** stream_id 에 대한 빈 `_stream_end` `send_delta` 호출을 모두
  silently drop 한다 — 진짜 streaming 의 finalizer (existing stream_id 의
  empty `_stream_end`) 만 wire 에 나간다.

- **`tts_chunk.sequence` 는 stream 단위로 리셋된다.** 한 turn 안에서
  multi-iteration (tool call) 이 생기면 nanobot 은 새 stream_id 를 발급하고,
  `SentenceChunker` 는 새 stream 마다 sequence 를 0 부터 다시 시작한다. FE /
  테스트는 한 turn 에서 `[0, 1, 2, 0, 1]` 같은 시퀀스를 받을 수 있어야
  하며, monotonic 검증은 `0` 가 새 stream 의 시작이라고 가정하고 segment
  단위로 한다.

## 개발 워크플로우

개발 원본(본 레포) → 여러 워크스페이스 클론으로 반영하는 흐름은
[`docs/operations.md`](./docs/operations.md#1-런타임-업데이트-워크플로우) 에 정리. `scripts/sync-to-yuri.sh` 가
gate keeper (정식: clean+pushed 검사 후 ff-only, 긴급: rsync+reminder).

## 새 Hook 추가 가이드

1. `src/nanobot_runtime/hooks/my_hook.py` 작성 — `AgentHook` 상속
2. `src/nanobot_runtime/hooks/__init__.py` 에서 re-export
3. TDD: `tests/test_my_hook.py` 먼저 RED 로 작성
4. (선택) 공통 factory 가 필요하면 `build_ltm_hooks` 옆에 형제 factory 추가
