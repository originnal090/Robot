"""One robot session shared by all browser observers; no Tk or web dependencies."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import cv2

from .app import ArmMode, RuntimeEvent, SessionControl
from .config import (
    ascend_yolo_config,
    controller_config,
    detector_config,
    load_config,
    startup_route_config,
)
from .frame_recorder import FrameRecorder
from .gamepad import GamepadMonitor, GamepadTeleop
from .gui_model import GuiModel, SessionState
from .session_runtime import SessionRuntime

ACTIONS = frozenset(
    {
        "nod",
        "shake",
        "left_trigger",
        "right_trigger",
        "right_grip",
        "head_down",
        "head_center",
        "head_up",
        "stand",
    }
)
MANUAL_SECONDS = 0.30
# Manual motion still has a 300 ms dead-man timeout. The ownership lease can
# tolerate browser/main-thread and Wi-Fi jitter without letting a stale manual
# command keep the robot moving.
LEASE_SECONDS = 3.0
STARTUP_LEASE_SECONDS = 15.0

_ASCEND_PRESETS = (
    (
        "yolo11n-640-v3",
        "YOLO11n v3（iPhone7 + TonyPi）",
        "/home/HwHiAiUser/hcirobot-ascend/models/yolo11n_640_v3.om",
        "混合相机训练版；推荐阈值 0.190",
        0.1899006367,
    ),
    (
        "yolo11n-640-v1",
        "YOLO11n v1（当前稳定版）",
        "/home/HwHiAiUser/hcirobot-ascend/models/yolo11n_640.om",
        "TonyPi 实测稳定版；保留现场兜底",
        0.053634531795978546,
    ),
)

_SVM_PRESETS = (
    (
        "route-balanced-a146-v2",
        "路线均衡 Hybrid A146 v2（推荐）",
        "artifacts/edge-route-20260915/route-balanced-a146-v2/profile.json",
        "新采集优化版；A146 + 模糊/背景样本",
    ),
    (
        "route-aug-v1",
        "路线增强 Hybrid",
        "artifacts/edge-route-20260914/route-aug-v1/profile.json",
        "加入贴底和模糊增强",
    ),
    (
        "route-balanced-a146-v1",
        "路线均衡 Hybrid A146 v1",
        "artifacts/edge-route-20260915/route-balanced-a146-v1/profile.json",
        "A146 第一版；保留用于与推荐版对照",
    ),
    (
        "experimental-v2",
        "路线 Experimental v2",
        "artifacts/edge-route-20260915/experimental-v2/profile.json",
        "新采集实验分支",
    ),
    (
        "route-balanced-v1",
        "路线均衡 Hybrid v1",
        "artifacts/edge-route-20260914/route-balanced-v1/profile.json",
        "第一轮路线采集的均衡版本",
    ),
    (
        "route-v1",
        "路线 Hybrid v1",
        "artifacts/edge-route-20260914/route-v1/profile.json",
        "第一轮路线采集基线",
    ),
    (
        "hybrid-v1-fast",
        "Hybrid v1 Fast（稳妥对照）",
        "artifacts/edge-versions-20260914/profiles/hybrid-v1-fast.json",
        "v1 权重 + ROI/恢复/批量特征",
    ),
    (
        "hybrid-v1-geometry",
        "Hybrid v1 Geometry（近距实验）",
        "artifacts/edge-versions-20260914/profiles/hybrid-v1-geometry.json",
        "放宽近距离大球和底边候选",
    ),
    (
        "hybrid-hn-v1-fast",
        "Hybrid HN v1（困难负样本）",
        "artifacts/edge-versions-20260914/profiles/hybrid-hn-v1-fast.json",
        "压制背景误框；可能漏掉底边球帽",
    ),
    (
        "hybrid-v1-recovery",
        "Hybrid v1 Recovery",
        "artifacts/edge-versions-20260914/profiles/hybrid-v1-recovery.json",
        "300 ms 近期目标恢复版本",
    ),
    (
        "hybrid-v1-legacy",
        "Hybrid v1 Legacy",
        "artifacts/edge-versions-20260914/profiles/hybrid-v1-legacy.json",
        "最初 LAB + SVM + ROI 对照",
    ),
    (
        "svm-v1",
        "SVM v1（全图基线）",
        "artifacts/edge-versions-20260914/profiles/svm-v1.json",
        "原 406 维全图候选分类基线",
    ),
    (
        "svm-feature-v2",
        "SVM Feature v2（回退对照）",
        "artifacts/edge-versions-20260914/profiles/svm-feature-v2.json",
        "去除颜色分位数的旧实验",
    ),
    (
        "hybrid-feature-v2",
        "Hybrid Feature v2（回退对照）",
        "artifacts/edge-versions-20260914/profiles/hybrid-feature-v2.json",
        "Hybrid 的 feature-v2 对照",
    ),
    (
        "hybrid-hn-v1-geometry",
        "Hybrid HN v1 Geometry（实验）",
        "artifacts/edge-versions-20260914/profiles/hybrid-hn-v1-geometry.json",
        "困难负样本与近距几何合并版",
    ),
)


def _detector_preset_available(project_root: Path, relative_path: str) -> bool:
    path = project_root / relative_path
    if not path.is_file():
        return False
    if path.suffix.lower() == ".npz":
        return True
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        model = profile.get("model")
        return (
            profile.get("schema_version") == 1
            and isinstance(model, str)
            and (path.parent / model).is_file()
        )
    except (OSError, ValueError, TypeError):
        return False


class ControlConflict(ValueError):
    """The requested operation does not belong to the current session/owner."""


class SessionService(SessionRuntime):
    def __init__(self, config_path: Path, *, gamepad: bool = False, clock=time.monotonic):
        self.config_path = config_path.resolve()
        self.config = load_config(self.config_path)
        self.model = GuiModel()
        self.session_control: SessionControl | None = None
        self.session_thread: threading.Thread | None = None
        self.active_detector = None
        self.active_controller = None
        self._lock = threading.RLock()
        self._clock = clock
        self._shutdown = threading.Event()
        self._encode_ready = threading.Event()
        self._pending_frame = None
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._last_frame_at: float | None = None
        self._frame_generation = 0
        self._clients: set[str] = set()
        self._owner: str | None = None
        self._lease = ""
        self._expires = 0.0
        self._manual_seq = -1
        self._output = {"v": 0.0, "steer": 0.0, "lateral": 0.0, "source": "shutdown"}
        self.frame_recorder = FrameRecorder(on_log=self._log)
        self.gamepad_monitor = GamepadMonitor(on_log=self._log) if gamepad else None
        self.gamepad_teleop = GamepadTeleop(self.gamepad_monitor) if gamepad else None
        self._ticker: threading.Thread | None = None
        self._encoder: threading.Thread | None = None

    def open(self) -> None:
        if self._ticker is not None:
            return
        if self.gamepad_monitor:
            self.gamepad_monitor.start()
        self._ticker = threading.Thread(target=self._tick, name="web-control", daemon=True)
        self._encoder = threading.Thread(target=self._encode, name="web-jpeg", daemon=True)
        self._ticker.start()
        self._encoder.start()

    def close(self) -> None:
        with self._lock:
            self._shutdown.set()
            self._stop_locked()
            self._encode_ready.set()
        if self.gamepad_monitor:
            self.gamepad_monitor.stop()
        for thread in (self.session_thread, self._ticker, self._encoder):
            if thread:
                thread.join(timeout=10)
        self.frame_recorder.stop_session()

    def connect(self) -> str:
        with self._lock:
            client = uuid.uuid4().hex
            self._clients.add(client)
            return client

    def disconnect(self, client: str) -> None:
        with self._lock:
            self._clients.discard(client)
            if self._owner == client:
                self._lose_owner("控制页面已断开，会话已停止")

    def claim(self, client: str) -> str:
        with self._lock:
            self._expire()
            if client not in self._clients:
                raise ControlConflict("请先连接状态通道")
            if self._owner is not None:
                if self._owner == client:
                    return self._lease
                raise ControlConflict("控制权已被另一页面或本机手柄占用")
            if self._worker_alive():
                raise ControlConflict("等待上一会话清理结束")
            self._owner = client
            self._lease = uuid.uuid4().hex
            self._expires = self._clock() + LEASE_SECONDS
            self._manual_seq = -1
            self._log("浏览器已取得控制权")
            return self._lease

    def heartbeat(self, client: str, lease: str) -> None:
        with self._lock:
            self._require_owner(client, lease)
            duration = (
                STARTUP_LEASE_SECONDS
                if self.model.session_state is SessionState.STARTING
                else LEASE_SECONDS
            )
            self._expires = self._clock() + duration

    def release(self, client: str, lease: str) -> None:
        with self._lock:
            self._require_owner(client, lease)
            self._lose_owner("控制权已释放，会话已停止")

    def _require_owner(self, client: str, lease: str, session_id: int | None = None) -> None:
        self._expire()
        if self._shutdown.is_set() or not lease or (self._owner, self._lease) != (client, lease):
            raise ControlConflict("控制权已失效，请重新取得控制权")
        if session_id is not None and session_id != self.model.session_id:
            raise ControlConflict("指令属于旧会话，已拒绝")

    def _expire(self) -> None:
        if self._owner not in (None, "local") and self._clock() >= self._expires:
            self._lose_owner("浏览器心跳超时，会话已停止")

    def _lose_owner(self, reason: str) -> None:
        self._stop_locked()
        self._owner = None
        self._lease = ""
        self._log(reason)

    def defaults(self) -> dict:
        cfg = self.config
        npu = ascend_yolo_config(cfg.get("npu", {}))
        startup = startup_route_config(cfg.get("startup", {}))
        project_root = Path.cwd()
        ascend_presets = [
            {
                "id": ident,
                "label": label,
                "path": path,
                "description": description,
                "confidence": confidence,
                "available": Path(path).is_file(),
            }
            for ident, label, path, description, confidence in _ASCEND_PRESETS
        ]
        if npu.model and all(item["path"] != npu.model for item in ascend_presets):
            ascend_presets.insert(
                0,
                {
                    "id": "configured",
                    "label": "配置文件模型",
                    "path": npu.model,
                    "description": "config.toml 当前配置",
                    "confidence": npu.confidence,
                    "available": Path(npu.model).is_file(),
                },
            )
        presets = [
            {
                "id": ident,
                "label": label,
                "path": path,
                "description": description,
                "available": _detector_preset_available(project_root, path),
            }
            for ident, label, path, description in _SVM_PRESETS
        ]
        svm_model = next((item["path"] for item in presets if item["available"]), "")
        return {
            "source": str(cfg["video"]["source"]),
            "backend": cfg["robot"]["backend"],
            "host": cfg["robot"]["host"],
            "port": cfg["robot"]["port"],
            "approach_mode": cfg["controller"].get("approach_mode", "normal_then_slow"),
            "arrival_size_version": cfg["controller"].get("arrival_size_version", "v2"),
            "obstacle": cfg.get("obstacle", {}).get("enabled", True),
            # Forward lead-in is deliberately one-shot and opt-in per Web session.
            "startup_forward": False,
            "startup_enabled": startup.enabled,
            "startup_forward_seconds": startup.forward_seconds,
            "gamepad_enabled": self.gamepad_monitor is not None,
            "detector_backend": "ascend" if npu.enabled else "lab",
            "detector_model_path": npu.model if npu.enabled else "",
            "detector_confidence": npu.confidence if npu.enabled else None,
            "ascend_model_path": npu.model,
            "ascend_confidence": npu.confidence,
            "ascend_presets": ascend_presets,
            "svm_model_path": svm_model,
            "svm_presets": presets,
            "control_lease_seconds": LEASE_SECONDS,
            "detection": asdict(detector_config(cfg["detection"])),
            "controller": asdict(controller_config(cfg["controller"])),
        }

    def start_session(self, client: str, lease: str, options: dict[str, Any]) -> None:
        # HTTP schema validation happens before this call. Runtime assembly still
        # validates the complete TOML, detector profile and obstacle configuration.
        with self._lock:
            self._require_owner(client, lease)
            if self._worker_alive() or not self.model.can_start:
                raise ControlConflict("会话忙碌或故障尚未复位")
            local = options.pop("local_gamepad", False)
            if local and self.gamepad_monitor is None:
                raise ControlConflict("服务未启用本机手柄，请使用 --gamepad 启动")
            values = dict(options, config=str(self.config_path))
            # Direct callers using the older edge_model_path remain compatible,
            # but the WebUI's explicit backend is never inferred from a stale path.
            model_path = str(
                values.get("detector_model_path", values.get("edge_model_path", "")) or ""
            ).strip()
            detector_backend = values.get("detector_backend")
            if detector_backend is None:
                suffix = Path(model_path).suffix.lower()
                detector_backend = "ascend" if suffix == ".om" else "svm" if model_path else "lab"
            if values["control_only"]:
                detector_backend, model_path = "lab", ""
            elif detector_backend == "ascend" and Path(model_path).suffix.lower() != ".om":
                raise ValueError("Ascend NPU 识别器需要 .om 模型")
            elif detector_backend == "svm" and Path(model_path).suffix.lower() not in {
                ".json",
                ".npz",
            }:
                raise ValueError("SVM / Hybrid 识别器需要 .json 配置或 .npz 权重")
            elif detector_backend not in {"lab", "ascend", "svm"}:
                raise ValueError("未知识别器类型")
            values["detector_backend"] = detector_backend
            values["detector_model_path"] = model_path
            confidence = values.get("detector_confidence")
            if confidence is not None:
                confidence = float(confidence)
                if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError("识别置信度必须在 0–1 之间")
            values["detector_confidence"] = confidence
            values["edge_model_path"] = model_path
            values["use_edge_model"] = detector_backend != "lab"
            if values["control_only"]:
                values["obstacle"] = False
            # Ascend ACL model initialization may briefly hold the interpreter,
            # preventing the WebSocket heartbeat task from running. A session
            # always starts unarmed; once the worker publishes "started",
            # heartbeats restore the normal runtime lease before arming.
            self._expires = max(self._expires, self._clock() + STARTUP_LEASE_SECONDS)
            self.model.begin_start(
                obstacle_enabled=values["obstacle"],
                control_only=values["control_only"],
            )
            self._clear_frame()
            self.active_detector = self.active_controller = None
            control = self.session_control = SessionControl()
            self.model.session_id = control.session_id
            self._manual_seq = -1
            if local:
                self._owner, self._lease = "local", ""
                self._log("控制权交给本机手柄；B 键武装/抢断，网页仍可停止和急停")
            self.session_thread = threading.Thread(
                target=self._run_session,
                args=(values, control),
                name="robot-session",
                daemon=True,
            )
            self.session_thread.start()

    def _run_session(self, values: dict, control: SessionControl) -> None:
        try:
            self._session_worker(values, control)
        finally:
            self.frame_recorder.stop_session()
            with self._lock:
                if self._owner == "local":
                    self._owner = None

    def _worker_alive(self) -> bool:
        return self.session_thread is not None and self.session_thread.is_alive()

    def stop(self, *, estop: bool = False) -> None:
        with self._lock:
            self._stop_locked(estop=estop)

    def _stop_locked(self, *, estop: bool = False) -> None:
        if self.session_control:
            if estop:
                self.session_control.request_estop()
            else:
                self.session_control.request_stop()
        if estop:
            self.model.latch_estop()
        else:
            self.model.request_stop()
        self._clear_frame()

    def command(
        self,
        client: str,
        lease: str,
        session_id: int,
        operation: str,
        arm_mode: ArmMode = "direct",
        startup_forward_seconds: float | None = None,
    ) -> None:
        with self._lock:
            self._require_owner(client, lease, session_id)
            if operation == "reset":
                if self._worker_alive() or not self.model.can_reset:
                    raise ControlConflict("等待会话结束后再复位")
                self.model.reset()
            elif operation == "arm":
                self.request_arm(arm_mode, startup_forward_seconds)
            elif operation == "disarm":
                self.request_disarm()
            else:
                raise ValueError("未知会话指令")

    # GamepadTeleop uses these same guarded operations as the web handlers.
    def request_arm(
        self,
        mode: ArmMode = "direct",
        startup_forward_seconds: float | None = None,
    ) -> None:
        if not self.model.can_arm or self.session_control is None:
            raise ControlConflict("当前不能武装，请检查会话、画面和故障状态")
        self.session_control.request_manual(0.0, 0.0, MANUAL_SECONDS)
        self.session_control.request_arm(mode, startup_forward_seconds)
        self.model.armed = True  # gate manual requests before the worker acknowledges

    def request_disarm(self) -> None:
        if self.session_control is None or self.model.session_state is not SessionState.RUNNING:
            raise ControlConflict("没有运行中的会话")
        self.session_control.consume_arm()
        self.session_control.request_disarm()
        self.session_control.request_manual(0.0, 0.0, MANUAL_SECONDS)
        self.model.armed = False

    def request_manual(
        self,
        velocity: float,
        steer: float,
        seconds: float = MANUAL_SECONDS,
        *,
        lateral: float = 0.0,
        source: str = "gamepad",
    ) -> None:
        if not self.model.can_manual or self.session_control is None:
            raise ControlConflict("请先解除自治，手动控制才可用")
        self.session_control.request_manual(
            velocity,
            steer,
            MANUAL_SECONDS,
            lateral=lateral,
            source=source,
        )

    def manual(
        self,
        client: str,
        lease: str,
        session_id: int,
        seq: int,
        velocity: float,
        steer: float,
        lateral: float,
    ) -> None:
        with self._lock:
            self._require_owner(client, lease, session_id)
            if seq <= self._manual_seq:
                raise ControlConflict("重复或乱序的运动指令已拒绝")
            self.request_manual(velocity, steer, lateral=lateral, source="manual")
            self._manual_seq = seq

    def request_action(self, name: str) -> None:
        if name not in ACTIONS:
            raise ValueError("不支持的动作")
        if not self.model.can_manual or self.session_control is None:
            raise ControlConflict("动作仅在未武装的运行会话中可用")
        self.session_control.request_action(name)

    def action(self, client: str, lease: str, session_id: int, name: str) -> None:
        with self._lock:
            self._require_owner(client, lease, session_id)
            self.request_action(name)

    def tune(
        self, client: str, lease: str, session_id: int, detection: dict, controller: dict
    ) -> dict:
        with self._lock:
            self._require_owner(client, lease, session_id)
            if self.active_detector is None or not self._worker_alive():
                raise ControlConflict("参数更新需要运行中的视觉会话")
            detector, control = self.active_detector, self.active_controller
            # Runtime settings such as stepping and approach mode are session-only.
            allowed = {
                "align_enter_error",
                "align_exit_error",
                "arrival_radius_ratio",
                "final_approach_radius_ratio",
                "final_approach_bottom_ratio",
                "final_approach_frames",
                "final_approach_velocity",
                "final_approach_seconds",
            }
            if controller.keys() - allowed:
                raise ValueError("仅支持热更新对准、到达和盲区末段参数")
            detector_fields = {
                "lab_min",
                "lab_max",
                "minimum_contour_area",
                "minimum_circularity",
                "minimum_aspect_ratio",
                "maximum_aspect_ratio",
                "confirmation_frames",
                "release_frames",
                "core_a_min",
                "minimum_core_fraction",
            }
            if detection.keys() - detector_fields:
                raise ValueError("不支持热更新该检测参数")
            for key in ("lab_min", "lab_max"):
                if key in detection and (
                    not isinstance(detection[key], list) or len(detection[key]) != 3
                ):
                    raise ValueError("LAB 阈值需要三个整数")
            for key in (
                "lab_min",
                "lab_max",
                "confirmation_frames",
                "release_frames",
                "core_a_min",
            ):
                if key in detection:
                    items = detection[key] if isinstance(detection[key], list) else [detection[key]]
                    if any(type(item) is not int for item in items):
                        raise ValueError(f"{key} 必须是整数")
            if (
                "final_approach_frames" in controller
                and type(controller["final_approach_frames"]) is not int
            ):
                raise ValueError("final_approach_frames 必须是整数")
            for value in list(detection.values()) + list(controller.values()):
                items = value if isinstance(value, list) else [value]
                if any(
                    isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                    for x in items
                ):
                    raise ValueError("参数必须是有限数值")
            det_cfg = detector_config(asdict(detector.config) | detection)
            ctrl_cfg = controller_config(asdict(control.config) | controller)
        # Detector update takes its own lock; never block the lease watchdog on it.
        detector.update_config(det_cfg)
        control.update_config(ctrl_cfg)
        self._log("检测/控制参数已热更新")
        return {"detection": asdict(det_cfg), "controller": asdict(ctrl_cfg)}

    def capture(self, client: str, lease: str, session_id: int, active: bool) -> None:
        with self._lock:
            self._require_owner(client, lease, session_id)
            if active:
                if self.model.session_state is not SessionState.RUNNING or self.model.control_only:
                    raise ControlConflict("采集需要运行中的视频会话")
                self.frame_recorder.start_session()
                self._log("原始帧采集已开始")
                return
        self.frame_recorder.stop_session()

    def _publish_event(self, event: RuntimeEvent) -> None:
        with self._lock:
            if event.session_id not in (0, self.model.session_id):
                return
            if event.kind == "error":
                self.model.fail(event.message)
            else:
                self.model.apply_event(event)
            if event.kind == "started" and self._owner not in (None, "local"):
                # Model/source initialization is complete. A silent browser now
                # returns to the normal fast-fail lease even if it sent no new heartbeat.
                self._expires = min(self._expires, self._clock() + LEASE_SECONDS)
            if event.kind in ("error", "finished", "stopping", "estop", "video_stale"):
                self._clear_frame()
            if (
                event.kind in ("frame", "manual")
                and self.model.session_state is SessionState.RUNNING
            ):
                self._output = {
                    "v": event.output_v,
                    "steer": event.output_steer,
                    "lateral": event.output_lateral,
                    "source": event.output_source,
                }
                if event.frame is not None:
                    self._last_frame_at = self._clock()
                    self._pending_frame = (self._frame_generation, event.frame)
                    self._encode_ready.set()

    def _clear_frame(self) -> None:
        self._frame_generation += 1
        self._pending_frame = self._jpeg = None
        self._last_frame_at = None
        self._output = {"v": 0.0, "steer": 0.0, "lateral": 0.0, "source": "shutdown"}

    def _log(self, message: str) -> None:
        with self._lock:
            self.model.append_log(message)

    def snapshot(self, client: str = "") -> dict:
        with self._lock:
            self._expire()
            state = asdict(self.model)
            state["session_state"] = self.model.session_state.name.lower()
            state["session_label"] = self.model.session_state.value
            state["logs"] = list(self.model.logs)
            state["capabilities"] = {
                name: getattr(self.model, "can_" + name)
                for name in ("start", "arm", "manual", "stop", "reset")
            }
            busy = self._worker_alive()
            state["capabilities"]["start"] &= not busy
            state["capabilities"]["reset"] &= not busy
            state["owner"] = (
                "local"
                if self._owner == "local"
                else ("you" if self._owner == client else "other" if self._owner else "none")
            )
            state["output"] = dict(self._output)
            state["frame_ready"] = self._jpeg is not None
            state["frame_age_ms"] = (
                None
                if self._last_frame_at is None
                else round((self._clock() - self._last_frame_at) * 1000)
            )
            active, saved, dropped, directory = self.frame_recorder.snapshot()
            state["capture"] = {
                "active": active,
                "saved": saved,
                "dropped": dropped,
                "directory": directory,
            }
            connected, message = (
                self.gamepad_monitor.status() if self.gamepad_monitor else (False, "未启用本机手柄")
            )
            reading = self.gamepad_monitor.latest() if self.gamepad_monitor else None
            state["gamepad"] = {
                "enabled": self.gamepad_monitor is not None,
                "connected": connected,
                "message": message,
                "name": reading.name if reading else "",
                "drive": round(reading.drive_axis, 3) if reading else 0.0,
                "lateral": round(reading.lateral_axis, 3) if reading else 0.0,
                "steer": round(reading.steer_axis, 3) if reading else 0.0,
                "head": round(reading.head_axis, 3) if reading else 0.0,
                "buttons": sorted(reading.buttons) if reading else [],
            }
            if self.active_detector is not None and self.active_controller is not None:
                state["tuning"] = {
                    "detection": asdict(self.active_detector.config),
                    "controller": asdict(self.active_controller.config),
                }
            return state

    def jpeg(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._jpeg_seq, self._jpeg

    def _tick(self) -> None:
        while not self._shutdown.wait(0.03):
            try:
                with self._lock:
                    self._expire()
                    if self.gamepad_teleop:
                        local = self._owner == "local"
                        self.gamepad_teleop.poll(
                            self,
                            armed=self.model.armed,
                            live=local and self.model.session_state is SessionState.RUNNING,
                            can_arm=local and self.model.can_arm,
                            can_manual=local and self.model.can_manual,
                            log=self._log,
                        )
            except Exception as exc:  # noqa: BLE001 - fail closed on dispatcher failure
                self._log(f"控制服务异常：{exc}")
                self.stop(estop=True)

    def _encode(self) -> None:
        while not self._shutdown.is_set():
            self._encode_ready.wait(0.1)
            self._encode_ready.clear()
            with self._lock:
                pending, self._pending_frame = self._pending_frame, None
            if pending is None:
                continue
            generation, frame = pending
            try:
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise ValueError("JPEG 编码失败")
                payload = encoded.tobytes()
                with self._lock:
                    if generation == self._frame_generation:
                        self._jpeg = payload
                        self._jpeg_seq += 1
            except Exception as exc:  # noqa: BLE001 - preview encoding is a side output
                self._log(f"视频预览编码失败：{exc}")
            self._shutdown.wait(0.1)  # encode once at <=10 Hz, shared by all viewers
