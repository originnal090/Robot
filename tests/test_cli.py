from __future__ import annotations

from pathlib import Path

from hcirobot import cli

ROOT = Path(__file__).resolve().parents[1]


def test_check_config_exits_before_runtime_resources(monkeypatch, capsys) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("runtime resource was opened")

    monkeypatch.setattr(cli, "build_source", unexpected)
    monkeypatch.setattr(cli, "UnityStatusPublisher", unexpected)

    result = cli.main(["--config", str(ROOT / "config.toml"), "--check-config"])

    assert result == 0
    assert capsys.readouterr().out.strip() == "config ok"


def test_check_config_validates_cli_overrides_before_resources(monkeypatch, capsys) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("runtime resource was opened")

    monkeypatch.setattr(cli, "build_source", unexpected)
    monkeypatch.setattr(cli, "UnityStatusPublisher", unexpected)

    result = cli.main(
        [
            "--config",
            str(ROOT / "config.toml"),
            "--check-config",
            "--backend",
            "tcp",
            "--robot-port",
            "0",
        ]
    )

    assert result == 1
    assert "robot port" in capsys.readouterr().err


def test_invalid_robot_port_override_fails_before_video_open(monkeypatch, capsys) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("video source was opened")

    monkeypatch.setattr(cli, "build_source", unexpected)

    result = cli.main(
        [
            "--config",
            str(ROOT / "config.toml"),
            "--backend",
            "tcp",
            "--robot-port",
            "0",
        ]
    )

    assert result == 1
    assert "robot port" in capsys.readouterr().err


def test_empty_robot_host_override_fails_before_video_open(monkeypatch, capsys) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("video source was opened")

    monkeypatch.setattr(cli, "build_source", unexpected)

    result = cli.main(
        [
            "--config",
            str(ROOT / "config.toml"),
            "--backend",
            "tcp",
            "--robot-host",
            "",
        ]
    )

    assert result == 1
    assert "robot host" in capsys.readouterr().err


def test_synthetic_smoke_reports_termination(capsys) -> None:
    result = cli.main(
        [
            "--config",
            str(ROOT / "config.toml"),
            "--source",
            "synthetic",
            "--backend",
            "recording",
            "--max-frames",
            "2",
            "--realtime",
        ]
    )

    assert result == 0
    assert "termination=max_frames" in capsys.readouterr().out
