"""Tests for the launcher's env-var-driven config builders.

The full ``main()`` entry point is exercised by the e2e suite; here we
cover the pure-logic env-var parsing in ``_build_idle_config`` and
``_resolve_tts_rules_path`` which are easy to get wrong (typos, empty
strings, comma-list edge cases) and would otherwise only surface as
mis-scheduled crons or missing TTS rules at startup.
"""
import os

import pytest

from nanobot_runtime.launcher import _build_idle_config, _resolve_tts_rules_path


# ── _build_idle_config ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_yuri_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe any inherited YURI_* env vars so each test starts from defaults."""
    for k in list(os.environ):
        if k.startswith("YURI_"):
            monkeypatch.delenv(k, raising=False)


def test_idle_config_defaults_match_docs() -> None:
    cfg = _build_idle_config()
    assert cfg.enabled is True
    assert cfg.idle_timeout_s == 300
    assert cfg.cooldown_s == 900
    assert cfg.scan_interval_s == 30
    assert cfg.startup_grace_s == 120
    assert cfg.timezone == "Asia/Tokyo"
    assert cfg.channels == ("desktop_mate",)
    assert cfg.quiet_hours is not None
    assert cfg.quiet_hours.start == "02:00"
    assert cfg.quiet_hours.end == "07:00"


def test_idle_config_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YURI_IDLE_ENABLED", "0")
    assert _build_idle_config().enabled is False


def test_idle_config_quiet_hours_disabled_when_both_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting both YURI_IDLE_QUIET_START and _END to "" turns off quiet
    hours entirely. Documented behaviour — used by the manual idle smoke."""
    monkeypatch.setenv("YURI_IDLE_QUIET_START", "")
    monkeypatch.setenv("YURI_IDLE_QUIET_END", "")
    assert _build_idle_config().quiet_hours is None


def test_idle_config_channels_split_strips_whitespace_and_blanks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YURI_IDLE_CHANNELS", "desktop_mate, slack ,")
    assert _build_idle_config().channels == ("desktop_mate", "slack")


def test_idle_config_int_envvar_typo_surfaces_as_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integer-coerced env vars (timeout/cooldown/scan/grace) raise
    ValueError on garbage input rather than silently defaulting — keeps
    operator typos loud at startup."""
    monkeypatch.setenv("YURI_IDLE_TIMEOUT_S", "5m")
    with pytest.raises(ValueError):
        _build_idle_config()


# ── _resolve_tts_rules_path ────────────────────────────────────────────


def test_tts_rules_path_defaults_to_cwd_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = str(tmp_path / "resources" / "tts_rules.yml")
    assert _resolve_tts_rules_path() == expected


def test_tts_rules_path_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YURI_TTS_RULES_PATH", "/tmp/custom_rules.yml")
    assert _resolve_tts_rules_path() == "/tmp/custom_rules.yml"
