## Summary

<!-- 1–3 bullets: what changed and why. Reference issues with `Fixes #N` / `Refs #N`. -->

-
-

## Runtime component(s) touched

<!-- Check all that apply. Remove the list if the change is purely docs / CI. -->

- [ ] `DesktopMateChannel` (WS protocol / routing / REST)
- [ ] `TTSHook` (hooks/tts.py)
- [ ] TTS dependency impl (chunker / preprocessor / emotion mapper / synthesizer)
- [ ] `LTMInjectionHook` / `LTMArgumentsHook` / `LTMSavingConsolidator`
- [ ] `IdleScanner` / `install_idle_system_job`
- [ ] Gateway launcher (gateway.py, monkey-patch surface)
- [ ] Tests (unit / integration / e2e)
- [ ] Docs / setup / operations
- [ ] CI / tooling / dependencies

## nanobot-ai compatibility

<!--
Does this PR touch any upstream nanobot-ai contract? If yes, name the field / method / hook and confirm which branch of yw0nam/nanobot it relies on.
Common trip-wires: AgentHookContext.session_key, AgentLoop._session_locks, Consolidator.archive, ChannelManager wiring, _SUPPORTED_PREFIXES version assertion.
Leave "N/A — no upstream contact" if this is pure runtime-internal.
-->

## Test plan

- [ ] `uv run pytest` (unit + integration, `-m 'not e2e'`) — all green
- [ ] `uv run pytest -m e2e` (if live infra affected — vLLM / Irodori TTS / LTM MCP)
- [ ] Manual smoke — <!-- describe the flow you ran by hand, or write N/A -->

## Notes for reviewer

<!-- Optional: design rationale, tradeoffs, follow-ups, screenshots, etc. -->
