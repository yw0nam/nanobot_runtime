# nanobot_runtime

여러 nanobot workspace 가 공유하는 런타임 글루 — AgentLoop hook 구현체,
gateway 런처(monkey-patch 방식), LTM(mem0) 연동 유틸. 개발은 이 저장소에서
하고, 각 workspace 는 `git clone` 해서 쓴다.

## 레이아웃

```
nanobot_runtime/
├── src/nanobot_runtime/
│   ├── gateway.py              # run(hooks_factory=...) — Typer app 으로 dispatch
│   └── hooks/
│       ├── ltm_args.py         # LTMArgumentsHook (wire-level id 교정)
│       ├── ltm_client.py       # LTMMCPClient (FastMCP 어댑터)
│       ├── ltm_consolidator.py # LTMSavingConsolidator + install_ltm_saving
│       ├── ltm_injection.py    # LTMInjectionHook (before_iteration 검색·주입)
│       └── __init__.py         # build_ltm_hooks factory
├── tests/                      # pytest, `uv run pytest`
└── pyproject.toml              # nanobot-ai 는 PyPI, path 의존 없음
```

## 새 워크스페이스 세팅

```bash
cd agents/<workspace_name>
git clone https://github.com/yw0nam/nanobot_runtime.git
```

워크스페이스의 `pyproject.toml`:

```toml
[project]
name = "<workspace_name>-workspace"
requires-python = ">=3.12"
dependencies = [
    "nanobot-ai>=0.1.5.post1",   # PyPI 에서 설치 — 경로 의존 X
    "nanobot-runtime",
]

[tool.uv.sources]
nanobot-runtime = { path = "./nanobot_runtime", editable = true }
```

워크스페이스의 `run_gateway.py` (얇은 shim):

```python
import os, sys
from nanobot.agent.hook import AgentHook
from nanobot.agent.loop import AgentLoop
from nanobot_runtime.gateway import run
from nanobot_runtime.hooks import build_ltm_hooks


def _hooks_factory(loop: AgentLoop) -> list[AgentHook]:
    return build_ltm_hooks(
        loop,
        user_id=os.getenv("LTM_USER_ID", "<default_user>"),
        agent_id=os.getenv("LTM_AGENT_ID", "<workspace_name>"),
        ltm_url=os.getenv("LTM_URL", "http://127.0.0.1:7777/mcp/"),
        top_k=int(os.getenv("LTM_TOP_K", "5")),
    )


if __name__ == "__main__":
    sys.exit(run(hooks_factory=_hooks_factory))
```

그 다음:

```bash
uv sync
uv run python run_gateway.py
```

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

## 개발 워크플로우

```bash
# 1) nanobot_runtime 자체 개발 (테스트 가능)
cd agents/nanobot_runtime
uv sync
uv run pytest           # 22개 unit, 1개 integration marker

# 2) 변경 후 GitHub push
git add -A && git commit -m "..."
git push

# 3) 각 워크스페이스에서 업데이트 당겨오기
cd agents/<workspace_name>/nanobot_runtime
git pull
# editable install 이므로 uv sync 재실행 불필요
```

## 새 Hook 추가 가이드

1. `src/nanobot_runtime/hooks/my_hook.py` 작성 — `AgentHook` 상속
2. `src/nanobot_runtime/hooks/__init__.py` 에서 re-export
3. TDD: `tests/test_my_hook.py` 먼저 RED 로 작성
4. (선택) 공통 factory 가 필요하면 `build_ltm_hooks` 옆에 형제 factory 추가
