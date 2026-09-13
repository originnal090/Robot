from __future__ import annotations

from pathlib import Path

from hcirobot.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_orangepi_template_is_valid_and_targets_tcp() -> None:
    config = load_config(ROOT / "deploy/config/config.orangepi.toml")

    assert config["robot"]["backend"] == "tcp"
    assert config["unity"]["enabled"] is False


def test_orangepi_systemd_never_auto_arms() -> None:
    unit = (ROOT / "deploy/systemd/hcirobot@.service").read_text(encoding="utf-8")

    executable_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert executable_lines
    assert all("--arm" not in line for line in executable_lines)
    assert "--check-config" in unit


def test_tonypi_production_environment_requires_hardware_sonar() -> None:
    environment = (ROOT / "deploy/env/tonypi.env.example").read_text(encoding="utf-8")
    assignments = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in environment.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert assignments["TONYPI_MODE"] == "hardware"
    assert assignments["TONYPI_REQUIRE_SONAR"] == "1"
    assert assignments["TONYPI_ALLOW_SIM_WITH_HARDWARE"] == "0"
    assert "TONYPI_SONAR_SIM" not in assignments


def test_tonypi_systemd_runs_preflight_before_service() -> None:
    unit = (ROOT / "deploy/systemd/tonypi-server.service").read_text(encoding="utf-8")

    assert "ExecStartPre=" in unit and "--check" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "Restart=on-failure" in unit
