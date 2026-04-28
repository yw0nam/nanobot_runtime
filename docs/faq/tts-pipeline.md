# FAQ — TTS Pipeline

> 텍스트 → 합성 → sink → 채널 출력의 전체 흐름과 확장 포인트.
> 이 문서는 [`CLAUDE.md`](../../CLAUDE.md) 의 entrypoint에서 참조됨.

---

## 1. 현재(STREAMING, DesktopMate) 흐름

```
LLM delta → TTSHook.on_stream → chunker.feed → 문장 검출
  → preprocessor.process → (text, emotion)
  → emotion_mapper.map(emotion) → keyframes
  → sink.is_enabled(session_key)?  ← mode_map 게이트
  → synthesizer.synthesize(text) [async]
  → TTSChunk(sequence, text, audio_base64, emotion, keyframes, session_key)
  → sink.send_tts_chunk(chunk)
       ↓ (LazyChannelTTSSink)
       DesktopMateChannel.send_tts_chunk → WS `tts_chunk` frame emit
on_stream_end(resuming=False) → TTS Barrier (모든 합성 task 완료 대기) → state cleanup
```

---

## 2. 계획 중(ATTACHMENT, Telegram) 흐름

플랜 위치: `docs/superpowers/plans/2026-04-26-tts-attachment-pipeline-telegram.md` (9 task, TDD)

```
... 위 STREAMING과 동일 (chunker → preprocessor → ... → synthesizer) ...
  → MultiplexingTTSSink.send_tts_chunk
      ├─ mode==STREAMING → LazyChannelTTSSink (live frame)
      └─ mode==ATTACHMENT → AttachmentTTSSink (per-session 버퍼링)

on_stream_end(resuming=False)
  → TTS Barrier
  → mux.on_session_end(session_key) [fan-out]
      ├─ LazyChannelTTSSink.on_session_end → no-op
      └─ AttachmentTTSSink.on_session_end → flush:
           ├─ buffer 정렬 (sequence 기준; 합성 task가 out-of-order 완료하므로 필수)
           ├─ OpusEncoder.encode_wav_chunks
           │    └─ ffmpeg concat → libopus 32k → OGG/Opus bytes
           ├─ <media_dir>/<channel>_<chat_id>_<ts>_<uuid>.ogg 저장
           └─ bus.publish_outbound(OutboundMessage(media=[ogg_path], content=""))

→ nanobot ChannelManager → TelegramChannel.send (upstream)
   └─ msg.media 순회: .ogg → bot.send_voice(file_handle)
```

**최종 output**: 사용자가 텔레그램에서 (a) streaming 텍스트 + (b) 턴 전체 합성 voice-note 1개 (waveform UI, 속도 조절). stream_end 후 ~1-2초.

---

## 3. 핵심 파일 cross-reference (runtime 쪽)

| 영역 | 파일 | 라인 | 역할 |
|------|------|------|------|
| Hook 본체 | `src/nanobot_runtime/services/hooks/tts/hook.py` | 71-250 | `TTSHook(AgentHook)` |
| Sink ABC | `src/nanobot_runtime/services/hooks/tts/abc.py` | 8-33 | `TTSSink` 추상 |
| 출력 모델 | `src/nanobot_runtime/services/hooks/tts/models.py` | 8-23 | `TTSChunk` (frozen Pydantic v2) |
| 의존 protocol | `src/nanobot_runtime/services/hooks/tts/protocols.py` | 1-44 | `SentenceChunker`, `TextPreprocessor`, `EmotionMapper`, `TTSSynthesizer`, `ReferenceIdResolver` |
| 모드 라우팅 | `src/nanobot_runtime/services/tts/modes.py` | 29-64 | `TTSMode` + `ChannelModeMap` + `load_channel_modes` |
| 합성 클라 | `src/nanobot_runtime/clients/irodori.py` | 59-88 | `IrodoriClient.synthesize` |
| 현 sink (DM) | `src/nanobot_runtime/services/channels/desktop_mate.py` | 379-453 | `LazyChannelTTSSink` |
| DM 송신 | `src/nanobot_runtime/services/channels/desktop_mate_tts.py` | 72-102 | `_DesktopMateTTSMixin.send_tts_chunk` |
| WS 프레임 | `src/nanobot_runtime/models/desktop_mate.py` | 132-157 | `TTSChunkFrame` |
| 부팅/조립 | `src/nanobot_runtime/launcher.py` | 70-111, 146-181 | `_build_tts_hook`, `_hooks_factory` |

## 4. 핵심 파일 cross-reference (nanobot upstream, 수정 X)

| 영역 | 파일 |
|------|------|
| Hook protocol | `nanobot/agent/hook.py:14-62` |
| Channel base | `nanobot/channels/base.py:15-197` |
| Channel registry | `nanobot/channels/registry.py:71` |
| Channel manager | `nanobot/channels/manager.py:59-98` |
| Message types | `nanobot/bus/events.py:9-37` |
| Telegram channel | `nanobot/channels/telegram.py:455-621` (`.venv/lib/python*/site-packages/...`) |

---

## 5. 데이터 모델 핵심

### TTSChunk
```python
class TTSChunk(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int                    # 0-based, 같은 session 안에서 단조 증가
    text: str                        # 클린업된 sentence
    audio_base64: str | None         # base64 WAV. None = 합성 실패 (silence)
    emotion: str | None              # 감지된 emoji (e.g. "😊")
    keyframes: list[dict[str, Any]]  # 감정→애니메이션 변환 결과
    session_key: str | None          # ATTACHMENT 모드용 (계획). 형식: <channel>:<chat_id>[:<thread>...]
```

### TTSSink contract
```python
class TTSSink(ABC):
    @abstractmethod
    async def send_tts_chunk(self, chunk: TTSChunk) -> None: ...

    @abstractmethod
    def is_enabled(self, session_key: str | None) -> bool:
        """dispatch 시점 + synth task 안 (second-chance)에서 두 번 호출됨"""

    # 계획 중 (TTS attachment plan Task 1):
    # @abstractmethod
    # async def on_session_end(self, session_key: str | None) -> None: ...
```

---

## 6. 모드 시스템

```yaml
# yuri/resources/tts_channel_modes.yml
default: none
channels:
  desktop_mate: streaming   # 실시간 sentence별 frame
  telegram: attachment      # 모드 선언됨, 파이프라인은 plan 진행 중
  slack: none               # 합성 자체 안 일어남 (GPU 절약)
```

```python
class TTSMode(str, Enum):
    STREAMING  = "streaming"    # 실시간, DesktopMate
    ATTACHMENT = "attachment"   # voice-note, Telegram
    NONE       = "none"         # text-only
```

---

## 7. 새 sink 작성 체크리스트

- [ ] `TTSSink(ABC)` 상속, `send_tts_chunk` + `is_enabled` 구현 (+ 계획 중 `on_session_end`)
- [ ] `is_enabled(session_key)` 안에서 **반드시 mode_map gate** 거치기 — 다른 채널 audio leak 방지
- [ ] `session_key` 파싱: `<channel>:<chat_id>[:<thread>...]`. `partition(":")` 로 prefix 추출
- [ ] Lazy resolve가 필요하면 module-level singleton 패턴 (`_LATEST_CHANNEL`) 활용 — `desktop_mate.py:53-75` 참조
- [ ] `audio_base64=None` (합성 실패) graceful 처리 — drop with warning, turn 전체를 망치지 말 것
- [ ] ATTACHMENT 모드면 sequence 기준 정렬 후 concat (out-of-order 완료 가능)
- [ ] 채널 모드 YAML에 추가: `resources/tts_channel_modes.yml`
- [ ] launcher의 `_build_tts_hook` 에서 sink 인스턴스화 + mux에 등록 (계획 시)

---

## 8. 자주 질문 받는 것

**Q: `chunker_factory`가 callable인 이유?**
A: `TTSHook`은 session별로 새 chunker 인스턴스를 만든다 (`_state_for(ctx)`). buffer/state 격리 필요해서.

**Q: TTS Barrier timeout은 왜 필요한가?**
A: 합성 task가 무한 hang하면 turn 전체가 막힘. 30s default (`TTS_BARRIER_TIMEOUT` env). 초과 시 cancel + warning.

**Q: `resuming=True` 일 때 왜 cleanup 안 하나?**
A: tool-call hop이라 turn이 안 끝났음. state 유지해서 sequence 끊김 없이 이어가야 함. 단, 현재 nanobot pin이 iteration 경계에서 `resuming=False` 보내는 패턴이 e2e에서 관찰됨 — README "`tts_chunk.sequence`" 항목 참조.

**Q: Slack 같은 NONE 모드 채널은 어떻게 되나?**
A: `LazyChannelTTSSink.is_enabled` 가 mode_map lookup → STREAMING 아니면 False → 합성 자체가 일어나지 않음. GPU 낭비 X, audio leak X.

---

## 9. 관련 plan / spec

- `docs/superpowers/plans/2026-04-26-tts-attachment-pipeline-telegram.md` — Telegram ATTACHMENT 9-task 플랜 (TDD)
- `docs/superpowers/specs/` — 더 깊은 설계 문서 (있으면 참조)
