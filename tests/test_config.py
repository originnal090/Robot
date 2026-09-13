from __future__ import annotations

from pathlib import Path

import pytest

from hcirobot.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_repository_configs_are_valid() -> None:
    assert load_config(ROOT / "config.toml")["robot"]["backend"] == "recording"
    assert load_config(ROOT / "config.unity.toml")["robot"]["backend"] == "tcp"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('backend = "recording"', 'backend = "tpc"', "robot backend"),
        ("port = 5075", "port = 0", "robot port"),
        ("fps = 10.0", "fps = 0.0", "video fps"),
        ("frame_timeout_seconds = 0.75", "frame_timeout_seconds = -1", "video frame timeout"),
    ],
)
def test_invalid_runtime_config_fails_before_resources_open(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    text = (ROOT / "config.toml").read_text(encoding="utf-8").replace(old, new, 1)
    path = tmp_path / "invalid.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):
        load_config(path)


def test_unknown_section_key_is_rejected(tmp_path: Path) -> None:
    text = (ROOT / "config.toml").read_text(encoding="utf-8").replace(
        "send_interval_seconds = 0.10",
        "send_interval_seconds = 0.10\nsend_intervl_seconds = 0.20",
        1,
    )
    path = tmp_path / "unknown.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="send_intervl_seconds"):
        load_config(path)


def test_unknown_optional_section_key_is_rejected(tmp_path: Path) -> None:
    text = (ROOT / "config.toml").read_text(encoding="utf-8").replace(
        "stale_after_s = 1.0",
        "stale_after_s = 1.0\nstale_after_seconds = 1.0",
        1,
    )
    path = tmp_path / "unknown-obstacle.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="stale_after_seconds"):
        load_config(path)


def test_unknown_top_level_section_is_rejected(tmp_path: Path) -> None:
    text = (ROOT / "config.toml").read_text(encoding="utf-8") + "\n[typo]\nvalue = 1\n"
    path = tmp_path / "unknown-section.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="typo"):
        load_config(path)
