# FAQ — 자주 틀리는 함정들

> 이번 조사 (2026-04-28) 로 발견하거나 README/CODING_RULES에 흩어져 있던 것들 모음.
> 이 문서는 [`CLAUDE.md`](../../CLAUDE.md) 의 entrypoint에서 참조됨.

---

## 1. Telegram voice-note mimetype 함정 (PTB)

`python-telegram-bot v20+` 의 `bot.send_voice(voice=bytes)` 호출 시:
- **`filename` kwarg 없으면 mimetype이 `application/octet-stream`** 으로 떨어져 voice-note UI (waveform + 속도조절) 안 나옴
- 내부적으로 `mimetypes.guess_type(filename)` 로 mimetype 결정. filename이 없으면 default fallback

**우리 파이프라인은 안전**:
- `OutboundMessage.media=[ogg_path]` 로 **파일 경로**를 전달
- nanobot의 TelegramChannel이 `.ogg` 확장자를 보고 voice 분류 + `bot.send_voice(open(path, "rb"))` 호출
- 파일 핸들 + path → mimetype 자동 추론 → 안전

**미래에 in-memory bytes로 바로 보내려 하면**:
```python
# ❌ WRONG — mimetype = application/octet-stream
await bot.send_voice(chat_id, voice=ogg_bytes)

# ✅ CORRECT
await bot.send_voice(chat_id, voice=ogg_bytes, filename="voice.ogg")
```

---

## 2. OGG/Opus가 voice-note UI의 필수 조건

Telegram voice-note (waveform + 1x/1.5x/2x 속도 조절) 표시 조건:
- **OGG container + Opus codec**
- MP3 / M4A는 그냥 첨부 audio로 떨어짐 (waveform 안 나옴)
- WAV는 일부 클라에서 재생 자체가 안 될 수 있음

ffmpeg 권장 설정 (계획의 `OpusEncoderConfig` 기준):
```
-ar 48000          # Telegram 권장 sample rate
-ac 1              # mono
-c:a libopus
-b:a 32k           # speech용 default
-application voip  # 음성 최적화 ('audio', 'lowdelay'도 있음)
-f ogg
```

---

## 3. ATTACHMENT 모드는 sequence 정렬이 필수

- `TTSHook._dispatch_sentence` 는 sentence를 in-order로 dispatch하지만,
- **합성 task는 out-of-order로 완료** (네트워크/모델 지연 차이)
- Sink는 완료 순서대로 chunk 받음

→ ATTACHMENT 모드에서 **반드시 sequence 기준 sort 후 concat**:

```python
buffered.sort(key=lambda pair: pair[0])  # (sequence, wav_bytes)
wav_chunks = [wav for _, wav in buffered]
opus_bytes = await encoder.encode_wav_chunks(wav_chunks)
```

STREAMING 모드는 sentence별 즉시 emit이므로 정렬 불필요 (DesktopMate FE가 sequence로 재생 순서 결정).

---

## 4. `TTSChunk.audio_base64` 는 `None` 일 수 있음

- IrodoriClient는 합성 실패 시 None 반환 (graceful degrade — fail loud는 안 함)
- DesktopMate FE는 None을 silence로 해석하고 재생 진행
- ATTACHMENT sink는 None을 **drop with warning** (1개 문장 실패가 turn 전체를 망치지 않게)

```python
async def send_tts_chunk(self, chunk: TTSChunk) -> None:
    if chunk.audio_base64 is None:
        logger.warning("TTS chunk seq={} has no audio (synth failed); dropping", chunk.sequence)
        return
    ...
```

---

## 5. TelegramChannel 은 upstream 에 이미 있음

수정 충동 참아라:
- 위치: `.venv/lib/python*/site-packages/nanobot/channels/telegram.py:455-621`
- voice 송신 코드 이미 존재 (line 495-506의 `_get_media_type` → `bot.send_voice` 디스패치)
- runtime에서 `OutboundMessage.media=[ogg_path]` 만 publish하면 됨

**upstream 수정이 필요해 보이면**:
1. hook으로 가능한지 먼저 검토
2. monkey-patch가 필요하면 `gateway.py` 의 기존 패턴 (`_install_monkey_patch`, `_install_channel_manager_patch`) 따라가기
3. 그래도 필요하면 → nanobot 자체에 PR. **runtime layer에서 99% 해결됨**

---

## 6. `session_key` 라이프사이클

- 같은 chat_id의 turn이 끝나면 (`on_stream_end(resuming=False)`) `_states[key]` pop → 다음 turn은 sequence 0부터
- multi-iteration turn (tool-call hop) 에선 `resuming=True` 라 state 유지 → sequence 끊김 없이 증가
- **단, README §"`tts_chunk.sequence`" 항목 참고**: 현재 nanobot pin 이 iteration 경계에서 `resuming=False` 보내는 패턴이 e2e에서 관찰됨 → wire 출력이 `[0,1,2,0,1]` 처럼 segment 단위 reset
- → FE/test 는 segment 단위 검증 권장 (`sequence == 0` 을 새 segment 시작으로 간주). nanobot upstream 정정되면 single-segment로 돌아갈 수 있음

---

## 7. monkey-patch 시 deferred import 필요

`gateway.py` 패턴:
```python
def run(...) -> None:
    _install_monkey_patch(hooks_factory)
    _install_run_patch()
    _install_channel_manager_patch()

    # nanobot CLI는 import-time side effect 가 있음.
    # 패치를 먼저 깔아야 import가 패치된 클래스를 본다.
    from nanobot.cli.commands import app   # ← deferred!

    app(args=["gateway", "--config", ..., "--workspace", ...], standalone_mode=False)
```

→ `from nanobot.cli.commands import app` 을 파일 top-level로 올리면 패치 전에 nanobot이 로드돼서 패치가 안 먹는다.

---

## 8. 채널 부팅 순서 vs Hook 등록 순서

```
launcher → gateway.run() → monkey-patch 설치 → nanobot CLI →
  AgentLoop 생성 (이때 hooks_factory가 hooks 주입) →
  ChannelManager._init_channels() (채널 부팅)
```

→ **Hook이 먼저 등록되고 채널이 나중에 부팅**. Hook 안에서 채널을 즉시 resolve하려 하면 RuntimeError.

해결: `LazyChannelTTSSink` 패턴 (module-level singleton + lazy lookup)
- 채널이 `__init__` 에서 `_LATEST_CHANNEL = self` 로 자기 자신 등록
- sink는 `send_tts_chunk` 시점에 `get_desktop_mate_channel()` 호출 (RuntimeError catch 후 warn-once)

---

## 9. 채널 모드 YAML이 없으면 부팅 거부

`launcher.py::_build_tts_hook` 가 `TTS_RULES_PATH`, `TTS_MODES_PATH` 양쪽을 부팅 시 검증:
```python
if not os.path.exists(rules_path):
    raise FileNotFoundError(...)  # fail loud, not degrade
```

→ TTS 끄려면 `TTS_ENABLED=0`. YAML 누락 시 silent fallback이 아니라 actionable error 메시지로 부팅 실패.

---

## 10. TTS Barrier timeout

`asyncio.wait(state.pending, timeout=barrier_timeout)` 에서 timeout 도달하면:
- 남은 task `cancel()` 호출
- `logger.warning` 로 알림
- `on_stream_end` 정상 종료 (turn은 이어짐)

→ **timeout=30s default** (`TTS_BARRIER_TIMEOUT` env). 합성 서버가 느리면 늘려라. timeout 너무 짧으면 voice-note에 누락 sentence 발생.

---

## 11. silent 예외 swallowing 절대 금지

CODING_RULES §5 위반인데 자주 본다:
```python
# ❌ WRONG
except Exception:
    pass

# ❌ WRONG (traceback loss)
except Exception as e:
    logger.error(f"Failed: {e}")

# ✅ CORRECT
except Exception:
    logger.exception("Failed: ...")  # traceback preserved

# ✅ CORRECT (non-fatal warning + traceback)
except Exception:
    logger.opt(exception=True).warning("Cleanup failed (ignored)")
```
