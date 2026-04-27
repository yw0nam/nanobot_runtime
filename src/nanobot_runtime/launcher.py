"""Workspace launcher: install LTM + TTS + idle hooks and start the gateway.

This module is the entry point that workspace ``.env`` configurations
target. Identity/runtime tunables (user_id, agent_id, LTM URL, idle
thresholds) come from ``YURI_*`` environment variables; TTS tunables
(backend URL, rules/modes paths) come from ``TTS_*`` since the TTS stack
is workspace-neutral. The same launcher binary can serve multiple
workspaces; the workspace itself only carries ``nanobot.json`` + ``.env``
+ ``resources/``.

Default paths resolve against the workspace working directory (cwd), not
against this file's location, so the launcher stays portable when
installed as a console script in a workspace ``.venv``.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Auto-load .env from workspace root (cwd) at import time.
load_dotenv(Path.cwd() / ".env")

from nanobot.agent.hook import AgentHook
from nanobot.agent.loop import AgentLoop

from nanobot_runtime.clients.irodori import IrodoriClient
from nanobot_runtime.config.idle import IdleConfig, QuietHours
from nanobot_runtime.gateway import run
from nanobot_runtime.services.channels.desktop_mate import LazyChannelTTSSink
from nanobot_runtime.services.hooks import build_ltm_hooks
from nanobot_runtime.services.hooks.tts import TTSHook
from nanobot_runtime.services.proactive.installer import install_idle_system_job
from nanobot_runtime.services.tts.chunker import SentenceChunker
from nanobot_runtime.services.tts.emotion_mapper import EmotionMapper
from nanobot_runtime.services.tts.modes import load_channel_modes
from nanobot_runtime.services.tts.preprocessor import Preprocessor


def _resolve_tts_rules_path() -> str:
    """Resolve the TTS rules YAML path at call time, not import time.

    Defaults to ``<cwd>/resources/tts_rules.yml`` so the launcher — which
    lives inside the package, not next to the workspace — picks up the
    workspace's own rules. Resolved per call so test harnesses that
    `chdir` after import still get the right path.
    """
    return os.getenv(
        "TTS_RULES_PATH",
        os.path.join(os.getcwd(), "resources", "tts_rules.yml"),
    )


def _resolve_tts_modes_path() -> str:
    """Resolve the TTS channel-mode YAML path at call time, not import time.

    Defaults to ``<cwd>/resources/tts_channel_modes.yml``. Same lazy-resolve
    pattern as ``_resolve_tts_rules_path`` so test harnesses that ``chdir``
    after import still get the right path.
    """
    return os.getenv(
        "TTS_MODES_PATH",
        os.path.join(os.getcwd(), "resources", "tts_channel_modes.yml"),
    )


def _build_tts_hook() -> TTSHook:
    """Assemble the TTS pipeline wired to DesktopMateChannel.

    The sink uses :class:`LazyChannelTTSSink` so the hook is independent of
    channel start-up order — the channel is resolved per-chunk at send
    time. If TTS is fully disabled for a workspace, set ``TTS_ENABLED=0``
    and the factory returns no TTS hook.

    A missing TTS rules YAML or modes YAML is treated as a misconfiguration,
    not a degraded mode — the operator gets a loud, actionable failure at
    startup.
    """
    rules_path = _resolve_tts_rules_path()
    if not os.path.exists(rules_path):
        raise FileNotFoundError(
            f"TTS rules YAML not found at {rules_path!r}. Set "
            "TTS_RULES_PATH or place the file at "
            "<workspace>/resources/tts_rules.yml. To run without TTS set "
            "TTS_ENABLED=0."
        )
    modes_path = _resolve_tts_modes_path()
    if not os.path.exists(modes_path):
        raise FileNotFoundError(
            f"TTS channel modes YAML not found at {modes_path!r}. Set "
            "TTS_MODES_PATH or place the file at "
            "<workspace>/resources/tts_channel_modes.yml. To run without TTS set "
            "TTS_ENABLED=0."
        )
    mode_map = load_channel_modes(modes_path)
    emotion_mapper = EmotionMapper.from_yaml(rules_path)
    synthesizer = IrodoriClient(
        base_url=os.getenv("TTS_URL", "http://192.168.0.41:8091"),
        reference_id=os.getenv("TTS_REF_AUDIO"),
    )
    return TTSHook(
        chunker_factory=SentenceChunker,
        preprocessor=Preprocessor(known_emojis=emotion_mapper.known_emojis),
        emotion_mapper=emotion_mapper,
        synthesizer=synthesizer,
        sink=LazyChannelTTSSink(mode_map=mode_map),
        barrier_timeout_seconds=float(os.getenv("TTS_BARRIER_TIMEOUT", "30")),
    )


def _build_idle_config() -> IdleConfig:
    """Read YURI_IDLE_* env vars into an :class:`IdleConfig`.

    Defaults: 5-minute idle threshold, 15-minute cooldown, 30s scan tick,
    120s startup grace, quiet hours 02:00–07:00 in Asia/Tokyo, desktop_mate
    channel only. Unset ``YURI_IDLE_QUIET_START`` + ``_END`` (both empty)
    disables the quiet window.
    """
    quiet_start = os.getenv("YURI_IDLE_QUIET_START", "02:00")
    quiet_end = os.getenv("YURI_IDLE_QUIET_END", "07:00")
    quiet_hours = (
        QuietHours(start=quiet_start, end=quiet_end)
        if quiet_start and quiet_end
        else None
    )
    channels = tuple(
        c.strip()
        for c in os.getenv("YURI_IDLE_CHANNELS", "desktop_mate").split(",")
        if c.strip()
    )
    return IdleConfig(
        enabled=os.getenv("YURI_IDLE_ENABLED", "1") != "0",
        idle_timeout_s=int(os.getenv("YURI_IDLE_TIMEOUT_S", "300")),
        cooldown_s=int(os.getenv("YURI_IDLE_COOLDOWN_S", "900")),
        scan_interval_s=int(os.getenv("YURI_IDLE_SCAN_INTERVAL_S", "30")),
        startup_grace_s=int(os.getenv("YURI_IDLE_STARTUP_GRACE_S", "120")),
        quiet_hours=quiet_hours,
        timezone=os.getenv("YURI_IDLE_TIMEZONE", "Asia/Tokyo"),
        channels=channels,
    )


def _hooks_factory(loop: AgentLoop) -> list[AgentHook]:
    hooks: list[AgentHook] = list(
        build_ltm_hooks(
            loop,
            user_id=os.getenv("YURI_LTM_USER_ID", "sangjun"),
            agent_id=os.getenv("YURI_LTM_AGENT_ID", "yuri"),
            ltm_url=os.getenv("YURI_LTM_URL", "http://127.0.0.1:7777/mcp/"),
            top_k=int(os.getenv("YURI_LTM_TOP_K", "5")),
        )
    )
    if os.getenv("TTS_ENABLED", "1") != "0":
        hooks.append(_build_tts_hook())

    idle_config = _build_idle_config()
    if idle_config.enabled:
        if loop.cron_service is None:
            # Operator opted in via YURI_IDLE_ENABLED; a single startup
            # warning is too easy to miss when the user-visible symptom is
            # "Yuri stopped greeting me." Fail loud so misconfig surfaces
            # at boot rather than as a slow degradation.
            raise RuntimeError(
                "YURI_IDLE_ENABLED=1 but AgentLoop has no cron_service. "
                "Either disable idle (YURI_IDLE_ENABLED=0) or ensure the "
                "AgentLoop is constructed with a CronService."
            )
        install_idle_system_job(
            agent=loop,
            sessions=loop.sessions,
            cron=loop.cron_service,
            config=idle_config,
        )

    return hooks


def main() -> None:
    run(
        hooks_factory=_hooks_factory,
        config_path=os.getenv("YURI_NANOBOT_CONFIG", "./nanobot.json"),
        workspace=os.getenv("YURI_WORKSPACE", "."),
    )


if __name__ == "__main__":
    main()
