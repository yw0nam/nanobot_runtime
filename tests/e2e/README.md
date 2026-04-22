# Live-infrastructure E2E suite

These tests spawn a real nanobot gateway subprocess and drive it through a real
WebSocket client. The in-process regression suite under `tests/regression/`
already covers the wire contract with fakes; this suite catches the
integration bugs fakes can't see — nanobot AgentLoop ordering, real vLLM
streaming timing, Irodori synthesis latency, and LTM MCP roundtrips.

## What you need

1. A configured workspace (a sibling `yuri/` directory by default, or set
   `YURI_WORKSPACE=<path>`) containing:
   - `nanobot.json` with `channels.desktop_mate.enabled=true`
   - `.venv/` with `uv sync` already run
   - `run_gateway.py` as the entrypoint
2. Three backends reachable:
   - vLLM (per `nanobot.json` `providers.vllm.apiBase`)
   - Irodori TTS (defaults to `http://192.168.0.41:8091`; override with
     `YURI_TTS_URL`)
   - LTM MCP (per `nanobot.json` `tools.mcpServers.ltm.url`)
3. Free port `8765` (the WS channel default). Override with `YURI_E2E_WS_PORT`.

When any of the above is missing the tests skip; you can run
`pytest tests/e2e/ -m e2e -v` to see the skip reasons.

## Running

```bash
cd nanobot_runtime
.venv/bin/python -m pytest tests/e2e/ -m e2e -v
```

`-m e2e` is required — the suite is **not** collected by the default
`pytest` run (see `pyproject.toml` `addopts`) because it's slow and
depends on external services.

Set a longer gateway-startup timeout if your workspace is slow to boot:

```bash
YURI_E2E_TIMEOUT=40 pytest tests/e2e/ -m e2e
```

## What gets verified

| Scenario | Verification |
| --- | --- |
| A — new session full lifecycle | ordered frames `ready → stream_start → delta* → stream_end → tts_chunk*` |
| B — resumed session | server honours client-supplied `chat_id` on `message` |
| C — long response | SentenceChunker produces ≥2 `tts_chunk`s with increasing `sequence` |
| D — emotion emoji | stripped from `delta.text`; preserved in `tts_chunk.text`; `emotion` tag set |
| E — parallel chats | two concurrent WS connections receive distinct `chat_id`s, no cross-talk |
| G — `?tts=0` URL override | client receives no `tts_chunk` **and** gateway log shows zero `IrodoriClient.synthesize:` lines |
| H — inbound `tts_enabled:false` | same as G |

Scenarios F (reconnect across a new WS) and I (default-on guard) are
covered by the in-process regression suite only — reconnect needs a
dedicated nanobot session-TTL harness.

## Diagnostics

The fixture writes gateway stdout+stderr to `/tmp/yuri_e2e_gateway.log`.
On failure the fixture dumps the tail automatically; for passing runs
you can inspect it manually:

```bash
tail -n 100 /tmp/yuri_e2e_gateway.log
```
