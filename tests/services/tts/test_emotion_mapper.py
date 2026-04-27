"""Tests for EmotionMapper — map emotion string/emoji → keyframe list."""

from __future__ import annotations

from pathlib import Path

from nanobot_runtime.services.tts.emotion_mapper import EmotionMapper


_CONFIG: dict[str, dict] = {
    "😊": {
        "keyframes": [
            {"duration": 0.4, "targets": {"happy": 1.0}},
            {"duration": 0.3, "targets": {"neutral": 0.5}},
        ]
    },
    "😭": {
        "keyframes": [
            {"duration": 0.5, "targets": {"sad": 1.0}},
        ]
    },
    "default": {
        "keyframes": [
            {"duration": 0.3, "targets": {"neutral": 1.0}},
        ]
    },
}


def test_known_emotion_returns_its_keyframes() -> None:
    m = EmotionMapper(_CONFIG)
    out = m.map("😊")
    assert out == [
        {"duration": 0.4, "targets": {"happy": 1.0}},
        {"duration": 0.3, "targets": {"neutral": 0.5}},
    ]


def test_none_emotion_returns_default() -> None:
    m = EmotionMapper(_CONFIG)
    out = m.map(None)
    assert out == [{"duration": 0.3, "targets": {"neutral": 1.0}}]


def test_unknown_emotion_returns_default() -> None:
    m = EmotionMapper(_CONFIG)
    out = m.map("🚀")
    assert out == [{"duration": 0.3, "targets": {"neutral": 1.0}}]


def test_empty_config_uses_hardcoded_default() -> None:
    m = EmotionMapper({})
    out = m.map(None)
    assert out == [{"duration": 0.3, "targets": {"neutral": 1.0}}]


def test_empty_string_emotion_returns_default() -> None:
    m = EmotionMapper(_CONFIG)
    out = m.map("")
    assert out == [{"duration": 0.3, "targets": {"neutral": 1.0}}]


def test_from_yaml_loads_emotion_motion_map(tmp_path: Path) -> None:
    yaml_text = """
emotion_motion_map:
  "😊":
    keyframes:
      - duration: 0.4
        targets:
          happy: 1.0
  default:
    keyframes:
      - duration: 0.3
        targets:
          neutral: 1.0
"""
    p = tmp_path / "tts_rules.yml"
    p.write_text(yaml_text, encoding="utf-8")
    m = EmotionMapper.from_yaml(p)
    assert m.map("😊") == [{"duration": 0.4, "targets": {"happy": 1.0}}]
    assert m.map(None) == [{"duration": 0.3, "targets": {"neutral": 1.0}}]
