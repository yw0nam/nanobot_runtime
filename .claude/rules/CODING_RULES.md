# Coding Rules — spow12's Personal Style Guide

---

## 1. Imports

```python
# ORDER: stdlib → third-party → local, blank line between each group
from datetime import datetime
from pathlib import Path

import yaml
from loguru import logger

from src.services.agent_service.service import AgentService
```

- **Absolute imports only.** Never use relative imports (`from .module import X`).
- **No `from __future__ import annotations`.** Use Python 3.10+ native syntax directly.
- **TYPE_CHECKING guard** for circular-import-only imports:
  ```python
  from typing import TYPE_CHECKING
  if TYPE_CHECKING:
      from src.services.proactive_service import ProactiveService
  ```
- **Multi-line imports** with parentheses for long paths — never backslash continuation.
- **No `__all__`** re-export declarations unless it's a public library package.

---

## 2. Type Hints

- **Strict everywhere.** Every function parameter and return value must be typed. No bare `Any`.
- **`Type | None`** not `Optional[Type]`. Python 3.10+ union syntax only.
- **`list[str]`** not `List[str]`. Lowercase builtins for generics.
- Instance attribute types declared inline at assignment:
  ```python
  self._heartbeat_tasks: set[asyncio.Task] = set()
  self._col: Collection
  ```

---

## 3. Naming Conventions

| Scope | Style | Example |
|-------|-------|---------|
| Files/Modules | `snake_case.py` | `task_status_middleware.py` |
| Classes | `PascalCase` | `OpenAIChatAgent` |
| Functions/Methods | `snake_case` | `initialize_async()` |
| Variables | `snake_case` | `request_id` |
| Constants | `UPPER_CASE` | `TOKEN_QUEUE_SENTINEL` |
| Private members | `_leading_underscore` | `_mcp_tools`, `_load_config()` |
| Private module-level constants | `_UPPER_CASE` | `_PERSONAS_PATH`, `_FS_MUTATING_TOOLS` |

---

## 4. Logging

**Framework: Loguru only. Never `print()`, never stdlib `logging`.**

```python
from loguru import logger
```

### Exception logging — always preserve traceback

```python
# CORRECT
except Exception:
    logger.exception("Failed to send message")

# WRONG — loses traceback
except Exception as e:
    logger.error(f"Failed to send message: {e}")
```

For non-fatal (warning level) exceptions that should still show traceback:
```python
except Exception:
    logger.opt(exception=True).warning("Slack cleanup failed (ignored)")
```

### Log style
- Use emoji markers for key lifecycle events:
  ```python
  logger.info(f"🚀 Starting {settings.app_name} v{settings.app_version}")
  logger.info(f"🔌 WebSocket connected: {connection_id}")
  logger.info(f"⚡ WebSocket disconnected: {connection_id}")
  logger.info(f"➡️ {request.method} {request.url.path}")
  logger.info(f"⬅️ {request.method} {request.url.path} ({response.status_code}) - {elapsed:.2f}ms")
  ```
- Bind request IDs for correlation:
  ```python
  with logger.contextualize(request_id=request_id):
      response = await call_next(request)
  ```
- **Never log** passwords, tokens, session secrets, or PII.
- **No DEBUG logs in production code paths.**

---

## 5. Error Handling

- Always re-raise with context in FastAPI routes:
  ```python
  except Exception as e:
      raise HTTPException(500, f"Error retrieving chat history: {e}") from e
  ```
- **Fail loudly.** Do not swallow exceptions with silent fallbacks or empty-default returns unless explicitly noted as non-fatal.
- **No `asyncio.gather(return_exceptions=True)` as a silence mechanism.** If you use it, handle each result explicitly.

---

## 6. Configuration

**Use Pydantic `BaseModel` + `Field()`.** Never `@dataclass` for config objects.

```python
class WebSocketConfig(BaseModel):
    ping_interval_seconds: int = Field(
        default=30, ge=1, description="Interval between ping messages in seconds"
    )
    pong_timeout_seconds: int = Field(
        default=10, ge=1, description="Timeout for pong response in seconds"
    )
```

- Runtime config loaded from YAML files via `yaml.safe_load()`.
- Settings resolved through central `settings` object — no hardcoded URLs, ports, or credentials.
- Module-level constants for path resolution:
  ```python
  _PERSONAS_PATH = Path(__file__).resolve().parents[3] / "yaml_files" / "personas.yml"
  ```

---

## 7. Pydantic Models

- **V2 syntax only.** `model_config = ConfigDict(...)`, `@field_validator` with `@classmethod`.
- Every field has a `description=` in `Field()`.
- Use `field_validator` for cross-field or value constraints; don't do manual checks in route handlers.
- `model_config = ConfigDict(arbitrary_types_allowed=True)` when embedding non-Pydantic objects (e.g., LangChain).

---

## 8. File & Class Structure

### File order
1. Module docstring (if needed)
2. Imports (stdlib → third-party → local)
3. Module-level constants (`_PRIVATE_CONST`, `PUBLIC_CONST`)
4. Module-level helper functions (prefixed `_`)
5. Class definitions

### Class method order
1. `__init__`
2. Public methods
3. Private methods (prefixed `_`)
4. `@abstractmethod` methods (if ABC subclass)

### Section comments — use unicode dash style:
```python
# ── Connection Bookkeeping ────────────────────────────────────────────────────

# ── Paths ─────────────────────────────────────────────────────────────────────
```

---

## 9. Comments & Docstrings

### Docstrings — Google-style on every public function/class:
```python
def connect(self, websocket: WebSocket) -> UUID:
    """Accept a new WebSocket connection.

    Args:
        websocket: The WebSocket connection.

    Returns:
        Unique connection identifier.
    """
```

### Inline comments — only for non-obvious logic:
```python
# Resolve services config path relative to main.yml location
base_dir = yaml_path.parent
```

- No comments that restate what the code does.
- No references to ticket numbers, PR numbers, or caller names in comments.

---

## 10. Testing

- **TDD mandatory:** RED → GREEN → REFACTOR. Write failing test first.
- Test naming: `TestClassName.test_does_behavior_when_condition`
- Test file structure mirrors `src/`:
  - `src/services/tts_service/service.py` → `tests/services/tts_service/test_service.py`
- Use `pytest.fixture` for shared setup. Fixtures are descriptive, not generic.
- Architecture / structural tests for layering violations live in `tests/` with a `_KNOWN_VIOLATIONS` escape hatch for tracked debt.
- **E2E tests must pass** (`bash scripts/e2e.sh`) before a task is marked done. No exceptions.

---

## 11. Anti-Patterns — Never Do These

| Anti-pattern | Why |
|---|---|
| `print(...)` | Use `logger` — always |
| `Optional[T]` / `Union[T, None]` | Use `T \| None` |
| `from __future__ import annotations` | Not needed in Python 3.10+ |
| `@dataclass` for config | Use Pydantic `BaseModel` + `Field()` |
| `from .module import X` (relative) | Use absolute imports (`from src.`) |
| Silent exception swallowing | Fail loudly; log with traceback |
| `asyncio.gather(return_exceptions=True)` as silence | Handle each result explicitly |
| Hardcoded URLs / credentials | Use `settings` object or YAML |
| `Any` type | Find the correct type |
| `type: ignore` | Fix the type, don't suppress |
| DEBUG logs in production paths | Dev only |
| `__all__` in non-library modules | Not needed |
| Section comments with `# ---` dashes | Use `# ── Name ───` unicode style |
