# FAQ — Hook System

> AgentHook lifecycle, 등록 경로, 새 hook 작성 가이드.
> 이 문서는 [`CLAUDE.md`](../../CLAUDE.md) 의 entrypoint에서 참조됨.

---

## 1. Hook protocol

위치: `nanobot/agent/hook.py:14-62` (upstream)

```python
@dataclass(slots=True)
class AgentHookContext:
    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    streamed_content: bool = False
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    session_key: str | None = None   # ★ <channel>:<chat_id>[:<thread>...]


class AgentHook:
    def wants_streaming(self) -> bool: ...
    async def before_iteration(self, context: AgentHookContext) -> None: ...
    async def on_stream(self, context: AgentHookContext, delta: str) -> None: ...
    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None: ...
    async def before_execute_tools(self, context: AgentHookContext) -> None: ...
    async def after_iteration(self, context: AgentHookContext) -> None: ...
    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None: ...
```

---

## 2. Lifecycle (호출 순서)

```
before_iteration(ctx)
  → [LLM call w/ streaming]
  → on_stream(ctx, delta)        # delta마다 (wants_streaming() == True인 hook만)
  → on_stream_end(ctx, resuming) # resuming=True: tool-call hop, False: turn 완료

before_execute_tools(ctx)         # tool 있을 때
  → [tool 실행]

after_iteration(ctx)
finalize_content(ctx, content)    # 마지막에 응답 텍스트 변형 가능
```

**`resuming` 플래그 핵심**:
- `resuming=True` → tool-call로 turn이 이어짐. state 유지
- `resuming=False` → turn 완전히 종료. state cleanup
- ATTACHMENT 같이 turn 단위 flush가 필요한 경우 `resuming=False` 에서만 flush

---

## 3. 등록 경로

```
launcher.py::main()
  → gateway.run(hooks_factory=_hooks_factory, ...)
       ↓
       _install_monkey_patch(hooks_factory)
            ↓ wraps AgentLoop.__init__
            ↓ after init: loop._extra_hooks.extend(hooks_factory(loop))
       ↓
       nanobot CLI 호출 → AgentLoop 생성 → 패치된 __init__가 hooks 주입
```

`_hooks_factory` 시그니처:
```python
def _hooks_factory(loop: AgentLoop) -> list[AgentHook]:
    hooks: list[AgentHook] = []
    hooks.extend(build_ltm_hooks(loop, ...))
    if os.getenv("TTS_ENABLED", "1") != "0":
        hooks.append(_build_tts_hook())
    # idle은 cron job으로 등록 (다른 메커니즘)
    return hooks
```

---

## 4. 새 Hook 작성 가이드

### 4.1 디렉토리 구조

단일 파일이면:
```
src/nanobot_runtime/services/hooks/my_hook.py
tests/services/hooks/test_my_hook.py  ← 디렉토리 미러링
```

복수 파일이면 (LTM 처럼):
```
src/nanobot_runtime/services/hooks/my_hook/
├── __init__.py        # build_my_hooks factory + re-export
├── injection.py       # 검색/주입 로직
├── consolidator.py    # turn 종료 후 처리
└── ...
```

### 4.2 최소 skeleton

```python
# src/nanobot_runtime/services/hooks/my_hook.py
from nanobot.agent.hook import AgentHook, AgentHookContext


class MyHook(AgentHook):
    def __init__(self, *, some_dep: SomeDep) -> None:
        self._dep = some_dep

    def wants_streaming(self) -> bool:
        return False  # True면 on_stream 호출됨

    async def before_iteration(self, context: AgentHookContext) -> None:
        # iteration 시작 전 로직
        ...

    async def after_iteration(self, context: AgentHookContext) -> None:
        # iteration 끝난 후 로직
        ...
```

### 4.3 launcher 등록

```python
# launcher.py::_hooks_factory
def _hooks_factory(loop: AgentLoop) -> list[AgentHook]:
    hooks: list[AgentHook] = []
    hooks.extend(build_ltm_hooks(loop, ...))
    if os.getenv("TTS_ENABLED", "1") != "0":
        hooks.append(_build_tts_hook())
    if os.getenv("MY_HOOK_ENABLED", "1") != "0":
        hooks.append(MyHook(some_dep=...))   # ← 추가
    return hooks
```

### 4.4 TDD

```
tests/services/hooks/test_my_hook.py
```
- RED: failing test 먼저 (AgentHookContext fixtures 사용)
- GREEN: 최소 구현
- REFACTOR
- 자세히는 [`.claude/rules/CODING_RULES.md`](../../.claude/rules/CODING_RULES.md) §10

---

## 5. 자주 틀리는 부분

- **`on_stream_end(resuming=True)` 에서 state pop하면 turn 중간에 끊김** — `resuming=False`에서만 cleanup
- **`wants_streaming()` 안 override하면 `on_stream`이 아예 호출 안 됨** — 기본 False
- **`AgentHookContext.session_key`가 `None`일 수도 있음** — fallback 처리 필수 (`_FALLBACK_SESSION_KEY` 패턴)
- **Hook 안에서 raise하면 다른 hook까지 영향** — `try/except + logger.exception`로 격리. `try` 안 쓰면 CompositeHook fan-out이 도중에 끊김
- **streaming hook이 비싸면 `on_stream` 안에서 sync 작업 X** — `asyncio.create_task`로 떼어내라 (TTSHook의 `_synth_and_emit` 패턴 참조)
- **`finalize_content` 는 sync** — async 작업 못 함. async가 필요하면 `after_iteration` 으로 옮기기

---

## 6. CompositeHook fan-out

여러 hook을 한꺼번에 호출하는 어댑터. `nanobot/agent/hook.py:CompositeHook`.

- 각 hook 메서드를 직렬로 호출 (parallel 아님)
- 한 hook이 raise하면 logging만 하고 다음 hook으로 진행 (격리됨)
- `wants_streaming()` 이 True인 hook이 하나라도 있으면 streaming mode 활성

---

## 7. 자주 질문 받는 것

**Q: hook 순서가 보장되나?**
A: `_extra_hooks` 리스트 순서대로. `_hooks_factory` 에서 append 순서가 곧 호출 순서.

**Q: hook 끼리 통신 가능?**
A: 직접 X. `AgentHookContext` 의 `metadata` 같은 mutable 필드를 통해 간접적으로. 권장은 안 함 (coupling 증가).

**Q: 같은 hook을 여러 번 등록하면?**
A: 이론상 가능하지만 권장 X. 같은 메서드가 여러 번 호출됨. 보통 서로 다른 hook 클래스로 분리.

**Q: hook을 turn 중간에 disable 하려면?**
A: `is_enabled(ctx)` 같은 internal check를 메서드 시작에 넣고 early return. 동적 disable 메커니즘은 nanobot에 없음.
