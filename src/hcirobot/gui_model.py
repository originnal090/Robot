from __future__ import annotations

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
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=500))

    @property
    def can_start(self) -> bool:
        return self.session_state in (SessionState.STOPPED, SessionState.FAILED) and not self.estop_latched

    @property
    def can_arm(self) -> bool:
        return (
            self.session_state is SessionState.RUNNING
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
            self.estop_latched or bool(self.fault)
        )

    def begin_start(self) -> None:
        self.session_state = SessionState.STARTING
        self.video_status = "正在打开"
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
        self.append_log("开始创建预览会话；自治保持未武装")

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
        if active:
            self.session_state = SessionState.STOPPING
            self.video_status = "正在关闭"
        self.append_log("操作员急停已锁存")

    def reset(self) -> None:
        if self.session_state not in (SessionState.STOPPED, SessionState.FAILED):
            return
        self.estop_latched = False
        self.fault = ""
        self.control_state = "IDLE"
        self.session_state = SessionState.STOPPED
        self.command = ZERO_COMMAND
        self.append_log("故障与急停锁存已复位；自治仍未武装")

    def fail(self, message: str) -> None:
        self.session_state = SessionState.FAILED
        self.video_status = "错误"
        self.robot_status = "错误"
        self.armed = False
        self.fault = message
        self.control_state = "LOST_SAFE"
        self.command = ZERO_COMMAND
        self.append_log(f"故障：{message}")

    def apply_event(self, event: RuntimeEvent) -> None:
        if event.kind == "started":
            # "started" binds the session identity, so it runs before the filter;
            # every other event from an unbound or replaced session is dropped.
            if event.session_id:
                self.session_id = event.session_id
            self.session_state = SessionState.RUNNING
            self.robot_status = "已连接"
            self.append_log("预览会话已启动")
            return
        if event.session_id not in (0, self.session_id):
            return  # late arrival from a session that was already replaced
        if event.kind == "armed":
            if self.session_state is not SessionState.RUNNING:
                return  # a late armed event must not arm a stopped session
            self.armed = True
            self.append_log("自治已武装")
        elif event.kind == "estop":
            self.latch_estop()
        elif event.kind == "action":
            self.append_log(f"手动动作已下发：{event.message}")
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
            command = event.decision.command
            self.command = f"v={command.velocity:.2f}  steer={command.steer:+.2f}"
        elif event.kind == "state":
            self.append_log(event.message)
        elif event.kind == "stopping":
            self.session_state = SessionState.STOPPING
            self.armed = False
            self.control_state = "IDLE"
            self.command = ZERO_COMMAND
        elif event.kind == "finished":
            self.session_state = SessionState.STOPPED
            self.video_status = "已关闭"
            self.robot_status = "已断开"
            self.armed = False
            self.control_state = "LOST_SAFE" if self.estop_latched else "IDLE"
            self.command = ZERO_COMMAND
            self.target = "未确认"
            self.horizontal_error = "--"
            self.radius_ratio = "--"
            self.append_log(f"会话结束：{event.message}")

    def append_log(self, message: str) -> None:
        self.logs.append(message)
