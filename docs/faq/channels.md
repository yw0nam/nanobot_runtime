# FAQ — Channel System

> 채널 등록, discovery, sink 작성 메커니즘.
> 이 문서는 [`CLAUDE.md`](../../CLAUDE.md) 의 entrypoint에서 참조됨.

---

## 1. 등록 메커니즘 (요약)

- **Discovery**: nanobot 패키지의 `nanobot/channels/` 안 `*.py` 파일을 pkgutil로 스캔 + setuptools entry_points (`nanobot.channels`) 플러그인. **built-in이 우선** (외부 플러그인이 같은 이름 못 가림).
- **Activation**: `nanobot.json` 의 `channels.<name>.enabled: true` 만 부팅됨.
- **Config substitution**: `${ENV_VAR}` 패턴은 nanobot config loader가 `.env` 값으로 치환 (launcher가 `dotenv.load_dotenv` 한 결과).

핵심 파일:
- `nanobot/channels/registry.py:71` — `discover_all()`
- `nanobot/channels/manager.py:59-98` — `_init_channels()` (config의 `enabled: true` 만 부팅)
- `nanobot_runtime/gateway.py:89-147` — `_install_channel_manager_patch()` (session_manager 자동 주입 generic화)

---

## 2. 현재 yuri/nanobot.json 상태 (조사 시점 snapshot)

```json
{
  "channels": {
    "telegram":     { "enabled": true,  "token": "${TELEGRAM_BOT_TOKEN}", "allowFrom": ["${TELEGRAM_ALLOW_USER_ID}"], "streaming": true },
    "slack":        { "enabled": true,  "botToken": "${SLACK_BOT_TOKEN}", "appToken": "${SLACK_APP_TOKEN}", ... },
    "desktop_mate": { "enabled": false, ... },
    "websocket":    { "enabled": false }
  }
}
```

**주의**: workspace 별로 가변. 항상 직접 확인하라 — 이 표는 한 시점 snapshot.

---

## 3. 사용 가능한 built-in 채널 (nanobot 0.1.5.x)

`/nanobot/channels/` pkgutil 스캔 대상:

- **telegram** — `python-telegram-bot v20+`, long-polling
- **slack** — Socket Mode + Events API
- **discord**, **matrix**, **dingtalk**, **feishu**, **qq**, **wecom**, **weixin**, **whatsapp**, **email**, **mochat**, **msteams**
- **websocket** — embedded WebUI surface

runtime 추가:
- **desktop_mate** (custom in `nanobot_runtime/services/channels/desktop_mate.py`) — DesktopMate WS 프로토콜 + LazyChannelTTSSink

---

## 4. Channel base class (`BaseChannel`)

위치: `nanobot/channels/base.py:15-197`

```python
class BaseChannel(ABC):
    name: str = "base"
    display_name: str = "Base"

    def __init__(self, config: Any, bus: MessageBus): ...

    @abstractmethod
    async def start(self) -> None: ...     # async, long-running

    @abstractmethod
    async def stop(self) -> None: ...      # cleanup

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None: ...  # text + media

    async def send_delta(self, chat_id: str, delta: str, metadata: dict | None = None) -> None: ...
    """optional: streaming text chunks. supports_streaming property가 config의 streaming 플래그로 제어"""

    async def transcribe_audio(self, file_path: str) -> str: ...
    """STT (Groq Whisper / OpenAI Whisper). 모든 채널이 상속받음"""
```

---

## 5. Outbound 메시지 라우팅

```
hook/sink가 OutboundMessage publish
    ↓
nanobot ChannelManager가 picks up
    ↓
msg.channel == "telegram" → TelegramChannel.send(msg)
                          ↓
                          msg.media 순회:
                            - .ogg → bot.send_voice(file_handle)
                            - .jpg/.png → bot.send_photo(...)
                            - .mp4 → bot.send_video(...)
                            - 기타 → bot.send_document(...)
                          msg.content (텍스트) → bot.send_message(...)
```

→ **TTS sink는 `OutboundMessage(media=[ogg_path], content="")` 만 publish하면 됨.** 텔레그램 특화 코드를 runtime에서 작성할 필요 X.

---

## 6. 새 채널 추가 절차

[`docs/operations.md` §4.5](../operations.md#45-새-채널--tts-모드-추가) 참조. 요약:

1. **(외부 플러그인 방식)** 새 패키지에서 `BaseChannel` 상속 + `entry_points` 등록
2. **(runtime fork 방식)** `services/channels/<name>.py` 작성, `BaseChannel` 상속
3. nanobot.json에 `channels.<name>: { enabled: true, ... }` 추가
4. workspace `.env`에 채널별 secrets
5. (TTS 필요하면) `tts_channel_modes.yml`에 모드 추가 + sink 작성 → [tts-pipeline.md](./tts-pipeline.md) §7

---

## 7. Channel + 세션 매핑

`AgentHookContext.session_key` 형식:
- DesktopMate: `desktop_mate:<chat_id>`
- Telegram: `telegram:<chat_id>` (DM) 또는 `telegram:<chat_id>:<thread_id>` (그룹/topic)
- Slack: `slack:<channel_id>` (또는 thread)

prefix만 잘라서 (`partition(":")`) 채널 이름을 얻으면 mode_map lookup 가능.

---

## 8. 자주 질문 받는 것

**Q: 채널 부팅 순서가 hook 등록보다 먼저인가?**
A: 아니. launcher → gateway → nanobot CLI → AgentLoop 생성 (이때 monkey-patch가 hooks 주입) → ChannelManager가 채널 부팅. 따라서 hook이 먼저 등록되고, sink가 channel을 lazy resolve해야 안전 (`LazyChannelTTSSink._LATEST_CHANNEL` 패턴).

**Q: 같은 채널 여러 인스턴스 (multi-tenancy) 가능?**
A: 현재 구조는 single-instance per channel name. multi-tenancy가 필요하면 channel name을 다르게 (`telegram_a`, `telegram_b`) 등록해야 함.

**Q: send_voice 형식 요구사항?**
A: Telegram이 voice-note UI (waveform + 속도조절) 표시하려면 **OGG container + Opus codec** 필요. MP3/M4A는 generic audio로 떨어짐. → [pitfalls.md](./pitfalls.md) §2 참조.
