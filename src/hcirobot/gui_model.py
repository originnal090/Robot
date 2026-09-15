from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from .app import RuntimeEvent

ZERO_COMMAND = "v=0.00  steer=+0.00"


class SessionState(str, Enum):
    STOPPED = "已停止"
    STARTING = "正在启动"
    RUNNING = "运行中"
    STOPPING = "正在停止"
    FAILED = "故障"


@dataclass(slots=True)
class GuiModel:
    session_state: SessionState = SessionState.STOPPED
    video_status: str = "未连接"
    robot_status: str = "未连接"
    control_state: str = "IDLE"
    frame_count: int = 0
    armed: bool = False
    estop_latched: bool = False
    fault: str = ""
    session_id: int = 0
    target: str = "未确认"
    horizontal_error: str = "--"
    radius_ratio: str = "--"
    command: str = ZERO_COMMAND
    output_source: str = "--"
    obstacle_state: str = "无数据"
    obstacle_reason: str = ""
    obstacle_distance: str = "--"
    vision_blocked: str = "--"
    avoid_count: int = 0
    obstacle_enabled: bool = False
    control_only: bool = False
    latched_blocked: bool = False
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    # Bumped on every append so the view can skip unchanged log re-syncs.
    log_version: int = 0

    @property
    def can_start(self) -> bool:
        return (
            self.session_state in (SessionState.STOPPED, SessionState.FAILED)
            and not self.estop_latched
            and not self.latched_blocked
        )

    @property
    def can_toggle_obstacle(self) -> bool:
        """The obstacle switch is only read at session start, so freeze it once live."""
        return self.session_state in (SessionState.STOPPED, SessionState.FAILED) and not self.armed

    @property
    def can_arm(self) -> bool:
        return (
            self.session_state is SessionState.RUNNING
            and not self.control_only
            and self.video_status == "画面正常"
            and not self.armed
            and not self.estop_latched
            and not self.fault
        )

    @property
    def can_manual(self) -> bool:
        """Manual nudges are allowed on any live, disarmed, fault-free session."""
        return (
            self.session_state is SessionState.RUNNING
            and not self.armed
            and not self.estop_latched
            and not self.fault
        )

    @property
    def can_stop(self) -> bool:
        return self.session_state in (SessionState.STARTING, SessionState.RUNNING)

    @property
    def can_reset(self) -> bool:
        return self.session_state in (SessionState.STOPPED, SessionState.FAILED) and (
            self.estop_latched or self.latched_blocked or bool(self.fault)
        )

    def begin_start(self, obstacle_enabled: bool = False, *, control_only: bool = False) -> None:
        self.session_state = SessionState.STARTING
        self.control_only = control_only
        self.video_status = "已禁用" if control_only else "正在打开"
        self.robot_status = "正在连接"
        self.control_state = "IDLE"
        self.frame_count = 0
        self.armed = False
        self.fault = ""
        self.session_id = 0
        self.target = "未确认"
        self.horizontal_error = "--"
        self.radius_ratio = "--"
        self.command = ZERO_COMMAND
        self.output_source = "--"
        # Snapshot of the obstacle switch; the worker reads the frozen value.
        self.obstacle_enabled = obstacle_enabled and not control_only
        self._reset_obstacle_telemetry()
        message = (
            "开始创建仅手柄控制会话；不连接图传，自治不可武装"
            if control_only
            else "开始创建预览会话；自治保持未武装"
        )
        if self.obstacle_enabled:
            # Obstacle stopping is live while disarmed; maneuvers still need arm.
            message += "；避障停车生效，倒退/转向机动仅在武装后执行"
        self.append_log(message)

    def request_stop(self) -> None:
        if self.can_stop:
            self.session_state = SessionState.STOPPING
            self.armed = False
            self.video_status = "正在关闭"
            self.control_state = "IDLE"
            self.command = ZERO_COMMAND
            self.append_log("已请求停止；等待会话确认结束")

    def latch_estop(self) -> None:
        active = self.session_state in (SessionState.STARTING, SessionState.RUNNING)
        self.estop_latched = True
        self.armed = False
        self.control_state = "LOST_SAFE"
        self.command = ZERO_COMMAND
        self._reset_obstacle_telemetry()
        if active:
            self.session_state = SessionState.STOPPING
            self.video_status = "正在关闭"
        self.append_log("操作员急停已锁存")

    def reset(self) -> None:
        if self.session_state not in (SessionState.STOPPED, SessionState.FAILED):
            return
        self.estop_latched = False
        self.latched_blocked = False
        self.fault = ""
        self.control_state = "IDLE"
        self.session_state = SessionState.STOPPED
        self.command = ZERO_COMMAND
        self._reset_obstacle_telemetry()
        self.append_log("故障、急停与避障锁存已复位；自治仍未武装")

    def fail(self, message: str) -> None:
        self.session_state = SessionState.FAILED
        self.video_status = "已禁用" if self.control_only else "错误"
        self.robot_status = "错误"
        self.armed = False
        self.fault = message
        self.control_state = "LOST_SAFE"
        self.command = ZERO_COMMAND
        self._reset_obstacle_telemetry()
        self.append_log(f"故障：{message}")

    def apply_event(self, event: RuntimeEvent) -> None:
        if event.kind == "started":
            if self.session_id and event.session_id not in (0, self.session_id):
                return
            # "started" binds the session identity, so it runs before the filter;
            # every other event from an unbound or replaced session is dropped.
            if event.session_id:
                self.session_id = event.session_id
            if self.session_state is not SessionState.STARTING:
                return  # stop/estop during startup must not revive the session
            self.session_state = SessionState.RUNNING
            self.robot_status = "已连接"
            self.append_log("仅手柄控制会话已启动" if self.control_only else "预览会话已启动")
            return
        if event.session_id not in (0, self.session_id):
            return  # late arrival from a session that was already replaced
        if event.kind == "armed":
            if self.session_state is not SessionState.RUNNING:
                return  # a late armed event must not arm a stopped session
            self.armed = True
            self.append_log("自治已武装")
        elif event.kind == "disarmed":
            self.armed = False
            self.append_log("自治已解除（手柄抢断），手动控制生效")
        elif event.kind == "estop":
            self.latch_estop()
        elif event.kind == "action":
            self.append_log(f"动作已下发：{event.message}")
        elif event.kind == "obstacle":
            # Reached only after the session-id filter above, so stale policy
            # telemetry from a replaced session is dropped like any other event.
            self._apply_obstacle_event(event)
        elif event.kind == "frame" and event.decision is not None and event.detection is not None:
            if self.session_state is not SessionState.RUNNING:
                return  # a stale frame must never revive a stopped session
            self.video_status = "画面正常"
            self.frame_count = event.frame_count
            self.control_state = event.decision.state.value
            self.target = "已确认" if event.detection.has_current_target else "未确认"
            error = event.decision.horizontal_error
            radius = event.decision.radius_ratio
            self.horizontal_error = "--" if error is None else f"{error:+.3f}"
            self.radius_ratio = "--" if radius is None else f"{radius:.3f}"
            self.command = f"v={event.output_v:.2f}  steer={event.output_steer:+.2f}"
            self.output_source = str(event.output_source or "--")
        elif event.kind == "state":
            self.append_log(event.message)
        elif event.kind == "video_retry":
            if self.session_state is SessionState.RUNNING:
                self.video_status = "等待画面" if "已恢复" in event.message else "重连中"
            self.append_log(event.message)
        elif event.kind == "video_stale":
            if self.session_state is SessionState.RUNNING:
                self.video_status = "画面冻结"
                self.command = ZERO_COMMAND
                self.output_source = str(event.output_source or "video_stale_hold")
            self.append_log(event.message)
        elif event.kind == "video_recovered":
            if self.session_state is SessionState.RUNNING:
                self.video_status = "等待画面"
            self.append_log(event.message)
        elif event.kind == "autonomy_paused":
            if self.session_state is not SessionState.RUNNING:
                return
            self.armed = False
            self.control_state = "VIDEO_HOLD"
            self.command = ZERO_COMMAND
            self.output_source = str(event.output_source or "video_stale_hold")
            self.append_log(event.message)
        elif event.kind == "autonomy_resumed":
            if self.session_state is not SessionState.RUNNING:
                return
            self.armed = True
            self.control_state = "SEARCHING"
            self.command = ZERO_COMMAND
            self.append_log(event.message)
        elif event.kind == "manual_fallback":
            if self.session_state is not SessionState.RUNNING:
                return
            self.armed = False
            self.control_state = "MANUAL"
            self.command = ZERO_COMMAND
            self.output_source = str(event.output_source or "video_stale_hold")
            self.append_log(event.message)
        elif event.kind == "manual":
            if self.session_state is not SessionState.RUNNING:
                return
            self.control_state = "MANUAL"
            self.frame_count = event.frame_count
            self.command = f"v={event.output_v:.2f}  steer={event.output_steer:+.2f}"
            self.output_source = str(event.output_source or "manual")
        elif event.kind in ("gamepad", "warning", "capture", "mirror"):
            # Background-thread notes (connection changes, degraded side outputs,
            # frame-recorder completion).
            self.append_log(event.message)
        elif event.kind == "stopping":
            self.session_state = SessionState.STOPPING
            self.armed = False
            self.control_state = "IDLE"
            self.command = ZERO_COMMAND
        elif event.kind == "finished":
            termination = event.message or "completed"
            fault_terminations = {
                "video_timeout": "视频超时",
                "video_frozen": "画面冻结",
                "video_ended": "视频流意外结束",
                "robot_connection_lost": "机器人连接丢失",
                "runtime_error": "运行时错误",
            }
            self.video_status = "已禁用" if self.control_only else "已关闭"
            self.robot_status = "已断开"
            self.armed = False
            self.command = ZERO_COMMAND
            self.output_source = "--"
            self.target = "未确认"
            self.horizontal_error = "--"
            self.radius_ratio = "--"
            self._reset_obstacle_telemetry()
            if termination in fault_terminations:
                self.session_state = SessionState.FAILED
                self.control_state = "LOST_SAFE"
                if not self.fault:
                    self.fault = fault_terminations[termination]
                    self.append_log(f"故障：{self.fault}")
            elif self.fault:
                self.session_state = SessionState.FAILED
                self.control_state = "LOST_SAFE"
            else:
                self.session_state = SessionState.STOPPED
                self.control_state = "LOST_SAFE" if self.estop_latched else "IDLE"
            self.append_log(f"会话结束：{termination}")

    def _reset_obstacle_telemetry(self) -> None:
        """Clear live obstacle telemetry; latched_blocked deliberately survives."""
        self.obstacle_state = "无数据"
        self.obstacle_reason = ""
        self.obstacle_distance = "--"
        self.vision_blocked = "--"
        self.avoid_count = 0

    def _apply_obstacle_event(self, event: RuntimeEvent) -> None:
        try:
            payload = json.loads(event.message)
            if not isinstance(payload, dict):
                raise TypeError("payload is not a JSON object")
            state = str(payload["state"]).strip()
            if not state:
                raise ValueError("payload has an empty state")
        except (ValueError, KeyError, TypeError) as exc:
            # Malformed telemetry degrades to a log line; it must never crash the GUI.
            self.append_log(f"避障消息解析失败：{type(exc).__name__}: {exc}")
            return
        previous_state = self.obstacle_state
        self.obstacle_state = state
        self.obstacle_reason = str(payload.get("reason", ""))
        distance = payload.get("distance")
        if distance is not None:
            try:
                self.obstacle_distance = f"{float(distance):.0f}"
            except (TypeError, ValueError):
                self.obstacle_distance = "--"
        vision_blocked = payload.get("vision_blocked")
        if isinstance(vision_blocked, bool):
            self.vision_blocked = "是" if vision_blocked else "否"
        if "avoid_count" in payload:
            try:
                self.avoid_count = int(payload["avoid_count"])
            except (TypeError, ValueError):
                pass
        elif state == "AVOIDING" and previous_state != "AVOIDING":
            # No explicit counter in the payload: count each entry into AVOIDING.
            self.avoid_count += 1
        action = str(payload.get("action", "")).strip()
        detail = f"（{action}）" if action else ""
        # Optional newer-payload key: maneuvers exist but are held back because
        # autonomy is disarmed. Tolerant: missing or non-bool means no note.
        if payload.get("suppressed") is True:
            detail += "；机动被压制（未武装）"
        if state == "BLOCKED":
            if not self.latched_blocked:
                self.latched_blocked = True
                self.append_log(f"避障锁存 BLOCKED{detail}：{self.obstacle_reason or '未知原因'}")
        else:
            self.append_log(f"避障 {state}{detail}：{self.obstacle_reason}")

    def append_log(self, message: str) -> None:
        self.logs.append(message)
        self.log_version += 1
