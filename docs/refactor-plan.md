# Refactor: src layout (DesktopMatePlus 스타일)

Branch: `refactor/src-layout`
Baseline: `pytest -q` → 213 passed, 10 deselected

## 목표 구조

```
src/nanobot_runtime/
├── __init__.py              # 공개 API (경로 갱신)
├── gateway.py               # 진입점
├── config/                  # Pydantic 설정
│   ├── __init__.py
│   └── desktop_mate.py      # ← channels/desktop_mate_config.py
├── core/                    # 인프라 (이미 복사됨)
│   ├── __init__.py
│   ├── logger.py
│   └── error_classifier.py
├── models/                  # 도메인/와이어 스키마
│   ├── __init__.py
│   └── desktop_mate.py      # ← channels/desktop_mate_protocol.py
└── services/                # 비즈니스 로직
    ├── __init__.py
    ├── channels/            # ← channels/ (config, protocol 빠진 나머지)
    ├── hooks/               # ← hooks/
    ├── tts/                 # ← tts/
    └── proactive/           # ← proactive/
```

## TDD 불변식

매 step 종료 시 `uv run pytest -q` → **213 passed**. 깨지면 step 롤백, 원인 찾고 재진행.

## Step 1 — models 레이어 분리

- `channels/desktop_mate_protocol.py` → `models/desktop_mate.py`
- `models/__init__.py` 생성 (빈 파일 또는 re-export)
- 모든 `from nanobot_runtime.channels.desktop_mate_protocol import ...` → `from nanobot_runtime.models.desktop_mate import ...`
- `tests/channels/test_desktop_mate_protocol.py` → `tests/models/test_desktop_mate.py` + import 갱신
- **Gate**: 213 passed

## Step 2 — config 레이어 분리

- `channels/desktop_mate_config.py` → `config/desktop_mate.py`
- `config/__init__.py` 생성
- import 경로 일괄 갱신
- **Gate**: 213 passed

## Step 3 — services 레이어 (대규모 이동)

- `services/__init__.py` 생성
- `channels/` → `services/channels/`
- `hooks/` → `services/hooks/`
- `tts/` → `services/tts/`
- `proactive/` → `services/proactive/`
- `gateway.py` 임포트 갱신
- 최상위 `__init__.py` 갱신: `from nanobot_runtime.services.hooks import ...`
- `pyproject.toml` entry_point 갱신: `nanobot_runtime.services.channels.desktop_mate:DesktopMateChannel`
- 테스트 디렉토리 미러링: `tests/channels/` → `tests/services/channels/`, `tests/tts/` → `tests/services/tts/`, `tests/proactive/` → `tests/services/proactive/`, `tests/test_ltm_*.py` → `tests/services/hooks/`, `tests/test_tts_hook.py` → `tests/services/hooks/`
- **Gate**: 213 passed

## Step 4 — core 정리

- `core/middleware.py` 제거 (Starlette 의존 — 라이브러리에 부적합, 미사용)
- `core/__init__.py`에 `setup_logging`, `get_request_id` 노출
- 게이트웨이 옵션: 라이브러리 특성상 `setup_logging()` 자동 호출은 하지 않음 (호스트 워크스페이스 결정에 위임)
- **Gate**: 213 passed

## Step 5 — 최종 검증

- `uv run pytest -q` (전체)
- `git status` / `git diff --stat`
- 사용자 리뷰 후 commit
