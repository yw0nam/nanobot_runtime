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
def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe inherited YURI_* and TTS_* env vars so each test starts from defaults."""
    for k in list(os.environ):
        if k.startswith("YURI_") or k.startswith("TTS_"):
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = str(tmp_path / "resources" / "tts_rules.yml")
    assert _resolve_tts_rules_path() == expected


def test_tts_rules_path_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_RULES_PATH", "/tmp/custom_rules.yml")
    assert _resolve_tts_rules_path() == "/tmp/custom_rules.yml"


# ── _resolve_tts_modes_path ────────────────────────────────────────────


class TestResolveTtsModesPath:
    def test_modes_path_defaults_to_cwd_resources_modes_yml(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        from nanobot_runtime.launcher import _resolve_tts_modes_path

        expected = str(tmp_path / "resources" / "tts_channel_modes.yml")
        assert _resolve_tts_modes_path() == expected

    def test_modes_path_env_override_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TTS_MODES_PATH", "/tmp/custom_modes.yml")
        from nanobot_runtime.launcher import _resolve_tts_modes_path

        assert _resolve_tts_modes_path() == "/tmp/custom_modes.yml"


# ── _build_tts_hook fail-loud guards ───────────────────────────────────


class TestBuildTtsHookFailsWhenModesMissing:
    def test_raises_file_not_found_when_modes_missing_with_actionable_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # Lay down a valid rules file so we get past the first guard.
        rules = tmp_path / "tts_rules.yml"
        rules.write_text("rules: []\n")
        monkeypatch.setenv("TTS_RULES_PATH", str(rules))
        # Don't create the modes file → expect FileNotFoundError.
        monkeypatch.setenv("TTS_MODES_PATH", str(tmp_path / "missing.yml"))

        from nanobot_runtime.launcher import _build_tts_hook

        with pytest.raises(FileNotFoundError) as ei:
            _build_tts_hook()
        msg = str(ei.value)
        assert "TTS_MODES_PATH" in msg
        assert "TTS_ENABLED=0" in msg

    def test_raises_file_not_found_when_rules_missing_with_actionable_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # Don't lay down a rules file → first guard must fail loud, before
        # the modes guard ever runs (so a refactor that swaps the order or
        # silently skips the rules check is caught here).
        monkeypatch.setenv("TTS_RULES_PATH", str(tmp_path / "missing_rules.yml"))
        # Modes path also points at a missing file; the rules guard must fire
        # FIRST so this never gets consulted.
        monkeypatch.setenv("TTS_MODES_PATH", str(tmp_path / "missing_modes.yml"))

        from nanobot_runtime.launcher import _build_tts_hook

        with pytest.raises(FileNotFoundError) as ei:
            _build_tts_hook()
        msg = str(ei.value)
        assert "TTS_RULES_PATH" in msg
        assert "tts_rules.yml" in msg
        assert "TTS_ENABLED=0" in msg
        # Confirm the rules guard fired, not the modes guard.
        assert "TTS_MODES_PATH" not in msg

    def test_does_not_check_modes_when_tts_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # When TTS_ENABLED=0, _hooks_factory must skip _build_tts_hook
        # entirely — no rules check, no modes check.
        #
        # We isolate the TTS branch by stubbing the only other branches
        # _hooks_factory exercises (LTM hook construction and the idle
        # cron install) so a failure in this test can ONLY mean the TTS
        # branch was incorrectly entered. No bare-except — every failure
        # mode is named.
        monkeypatch.setattr(
            "nanobot_runtime.launcher.build_ltm_hooks",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            "nanobot_runtime.launcher.install_idle_system_job",
            lambda **k: None,  # never actually installed (we set IDLE off)
        )
        monkeypatch.setenv("TTS_ENABLED", "0")
        monkeypatch.setenv("YURI_IDLE_ENABLED", "0")
        monkeypatch.chdir(tmp_path)  # no resources/ dir at all

        from unittest.mock import MagicMock
        from nanobot_runtime.launcher import _hooks_factory

        loop = MagicMock()
        loop.cron_service = None  # not consulted because IDLE is off

        hooks = _hooks_factory(loop)

        assert hooks == []
