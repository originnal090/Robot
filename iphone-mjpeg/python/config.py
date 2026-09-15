"""Central configuration for the iPhone MJPEG service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Config:
    host: str = os.environ.get("IPHONE_MJPEG_HOST", "0.0.0.0")
    port: int = _integer("IPHONE_MJPEG_PORT", 8088)
    bridge_port: int = _integer("IPHONE_MJPEG_BRIDGE_PORT", 18088)
    width: int = _integer("IPHONE_MJPEG_WIDTH", 640)
    height: int = _integer("IPHONE_MJPEG_HEIGHT", 480)
    fps: int = _integer("IPHONE_MJPEG_FPS", 30)
    stream_fps: int = _integer("IPHONE_MJPEG_STREAM_FPS", 120)
    send_buffer_bytes: int = _integer("IPHONE_MJPEG_SEND_BUFFER", 32 * 1024)
    jpeg_quality: int = _integer("IPHONE_MJPEG_JPEG_QUALITY", 60)
    rotation: int = _integer("IPHONE_MJPEG_ROTATION", 0)
    stale_after_seconds: float = float(os.environ.get("IPHONE_MJPEG_STALE_AFTER", "2.0"))
    native_restart_seconds: float = float(os.environ.get("IPHONE_MJPEG_RESTART_DELAY", "2.0"))

    def validate(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError("port must be between 1 and 65535")
        if not (1 <= self.bridge_port <= 65535):
            raise ValueError("bridge_port must be between 1 and 65535")
        if self.width <= 0 or self.height <= 0 or self.fps <= 0 or self.stream_fps <= 0:
            raise ValueError("width, height, fps, and stream_fps must be positive")
        if not (4 * 1024 <= self.send_buffer_bytes <= 1024 * 1024):
            raise ValueError("send_buffer_bytes must be between 4096 and 1048576")
        if not (1 <= self.jpeg_quality <= 100):
            raise ValueError("JPEG quality must be between 1 and 100")
        if self.rotation not in {0, 90, 180, 270}:
            raise ValueError("rotation must be 0, 90, 180, or 270")


def default_project_dir() -> Path:
    return Path(__file__).resolve().parents[1]
