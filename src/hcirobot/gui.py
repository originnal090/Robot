from __future__ import annotations

import contextlib
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

import cv2
from PIL import Image, ImageTk

from .app import RuntimeEvent, SessionControl, run_loop
from .cli import build_source
from .config import controller_config, detector_config, load_config, unity_status_config
from .controller import ControllerConfig, VisualApproachController
from .detector import DetectorConfig, RedBallDetector
from .frame_recorder import FrameRecorder
from .gamepad import TOGGLE_HINT, GamepadMonitor, GamepadTeleop, map_to_command
from .gui_model import GuiModel, SessionState
from .robot import (
    FanoutRobot,
    MirrorTcpRobot,
    RecordingRobot,
    TcpRobotClient,
    parse_endpoint,
    same_endpoint,
)
from .unity_udp import UnityStatusPublisher, fanout_event_sinks
from .video import RetryingSource

BG = "#101417"
PANEL = "#181e22"
PANEL_ALT = "#20282d"
TEXT = "#e9eef0"
MUTED = "#91a0a8"
ACCENT = "#f2c94c"
GOOD = "#4cc38a"
DANGER = "#e24a4a"
BORDER = "#344047"

MANUAL_JOG_LEVEL = 0.35
MANUAL_JOG_SECONDS = 0.35
MANUAL_STOP_SECONDS = 0.3

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def _auto_arm_allowed(backend: str, host: str) -> bool:
    """Demo auto-arm is restricted to loopback TCP so it can only ever reach a simulator."""
    normalized_host = (host or "").strip().lower().strip("[]")
    return backend.strip().lower() == "tcp" and normalized_host in _LOOPBACK_HOSTS


def _default_tuning() -> dict[str, str]:
    """Entry fallbacks derived from the dataclass defaults, before config.toml is read."""
    detector = DetectorConfig()
    controller = ControllerConfig()
    return {
        "lab_min_l": str(detector.lab_min[0]),
        "lab_min_a": str(detector.lab_min[1]),
        "lab_min_b": str(detector.lab_min[2]),
        "lab_max_l": str(detector.lab_max[0]),
        "lab_max_a": str(detector.lab_max[1]),
        "lab_max_b": str(detector.lab_max[2]),
        "minimum_contour_area": str(detector.minimum_contour_area),
        "minimum_circularity": str(detector.minimum_circularity),
        "minimum_aspect_ratio": str(detector.minimum_aspect_ratio),
        "maximum_aspect_ratio": str(detector.maximum_aspect_ratio),
        "confirmation_frames": str(detector.confirmation_frames),
        "release_frames": str(detector.release_frames),
        "core_a_min": str(detector.core_a_min),
        "minimum_core_fraction": str(detector.minimum_core_fraction),
        "align_enter_error": str(controller.align_enter_error),
        "align_exit_error": str(controller.align_exit_error),
        "arrival_radius_ratio": str(controller.arrival_radius_ratio),
    }


class RobotControlApp:
    def __init__(
        self,
        root: tk.Tk,
        config_path: Path = Path("config.toml"),
        *,
        demo: bool = False,
    ) -> None:
        self.root = root
        self.model = GuiModel()
        self.config_path = config_path
        self.demo = demo
        self.demo_auto_arm_done = False
        self.events: queue.Queue[RuntimeEvent] = queue.Queue()
        self.latest_frame: RuntimeEvent | None = None
        self.session_control: SessionControl | None = None
        self.session_thread: threading.Thread | None = None
        self.active_detector: RedBallDetector | None = None
        self.active_controller: VisualApproachController | None = None
        self.tuning_vars: dict[str, tk.StringVar] = {}
        self.manual_buttons: list[ttk.Button] = []
        self._closing = False
        self._photo: ImageTk.PhotoImage | None = None
        # Raw-frame sample capture; logs hop to the mainloop via the queue.
        self.frame_recorder = FrameRecorder(on_log=self._publish_capture_log)
        self._resize_after_id: str | None = None
        self._pending_view_size: tuple[int, int] | None = None
        self._rendered_view_size: tuple[int, int] | None = None
        self._log_version = -1
        self._build_styles()
        self._build_window()
        # The monitor owns the pygame backend on its own thread; logs hop to
        # the mainloop through the events queue (Tk is not thread-safe).  It
        # must exist before _refresh_view, which reads its status.
        self.gamepad_monitor = GamepadMonitor(on_log=self._publish_gamepad_log)
        self.gamepad_teleop = GamepadTeleop(self.gamepad_monitor)
        self._load_defaults()
        self._refresh_view()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(30, self._drain_events)
        self.gamepad_monitor.start()
        # One delayed log so the gamepad state is discoverable without hunting
        # for the sidebar block; connect/disconnect transitions log on their own.
        self._gamepad_status_reported = False
        self.root.after(1200, self._report_gamepad_status_once)
        if self.demo:
            self.model.append_log(
                "演示模式：将自动开始预览" + ("并自动武装（仅限本机 TCP 后端）" if demo else "")
            )
            self.root.after(200, self._demo_start)

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL_ALT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure(
            "PanelTitle.TLabel", background=PANEL, foreground=ACCENT, font=("Segoe UI", 9, "bold")
        )
        style.configure(
            "Value.TLabel", background=PANEL, foreground=ACCENT, font=("Segoe UI", 10, "bold")
        )
        style.configure(
            "Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 16, "bold")
        )
        style.configure("TButton", padding=(12, 8), background=PANEL_ALT, foreground=TEXT)
        style.map("TButton", background=[("active", "#2b353b"), ("disabled", "#151a1d")])
        style.configure(
            "Accent.TButton", background=ACCENT, foreground="#141414", font=("Segoe UI", 10, "bold")
        )
        style.map("Accent.TButton", background=[("active", "#ffd965"), ("disabled", "#5e583e")])
        style.configure(
            "Danger.TButton", background=DANGER, foreground="white", font=("Segoe UI", 10, "bold")
        )
        style.map("Danger.TButton", background=[("active", "#ff5c5c")])
        style.configure("TEntry", padding=7, fieldbackground=PANEL_ALT, foreground=TEXT)
        style.configure("TCombobox", padding=6, fieldbackground=PANEL_ALT, foreground=TEXT)
        style.configure("TLabelframe", background=PANEL, foreground=TEXT, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=PANEL, foreground=MUTED)
        style.configure("Panel.TCheckbutton", background=PANEL, foreground=TEXT)
        style.map("Panel.TCheckbutton", background=[("active", PANEL), ("selected", PANEL)])

    def _build_window(self) -> None:
        self.root.title("TonyPi 视觉自治控制台")
        # DPI-aware rendering means physical pixels; scale the window size so a
        # high-DPI display gets the same layout proportions (point-based fonts
        # scale themselves through tk scaling).
        scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.root.geometry(f"{round(1280 * scale)}x{round(840 * scale)}")
        self.root.minsize(round(1024 * scale), round(720 * scale))
        self.root.configure(bg=BG)

        # The bottom control strip is packed FIRST (side="bottom") so the
        # expanding content area can never clip it, whatever the window size.
        controls = ttk.Frame(self.root, padding=(18, 0, 18, 16))
        controls.pack(side="bottom", fill="x")
        self.start_button = ttk.Button(controls, text="开始预览", command=self._start_session)
        self.start_button.pack(side="left")
        self.arm_button = ttk.Button(
            controls, text="武装自治", style="Accent.TButton", command=self._arm
        )
        self.arm_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(controls, text="停止", command=self._stop)
        self.stop_button.pack(side="left")
        self.reset_button = ttk.Button(controls, text="复位", command=self._reset)
        self.reset_button.pack(side="left", padx=8)
        self.capture_button = ttk.Button(
            controls, text="采集样本", style="Accent.TButton", command=self._toggle_capture
        )
        self.capture_button.pack(side="left", padx=8)
        self.estop_button = ttk.Button(
            controls, text="紧急停止", style="Danger.TButton", command=self._estop
        )
        self.estop_button.pack(side="right")
        ttk.Label(controls, text="软件停止不能替代物理断电", style="Muted.TLabel").pack(
            side="right", padx=14
        )

        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(side="top", fill="x")
        ttk.Label(header, text="TonyPi 视觉自治控制台", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="纯视觉 · 搜寻 / 对准 / 接近", style="Muted.TLabel").pack(
            side="left", padx=16
        )
        self.session_badge = tk.Label(
            header,
            text="已停止",
            bg=PANEL_ALT,
            fg=TEXT,
            padx=12,
            pady=5,
            font=("Segoe UI", 9, "bold"),
        )
        self.session_badge.pack(side="right")

        status = ttk.Frame(self.root, style="Panel.TFrame", padding=(18, 10))
        status.pack(side="top", fill="x", padx=18)
        self.status_values: dict[str, ttk.Label] = {}
        for key, label in (
            ("video", "视频"),
            ("robot", "机器人"),
            ("control", "控制状态"),
            ("gamepad", "手柄"),
            ("frames", "帧数"),
        ):
            block = ttk.Frame(status, style="Panel.TFrame")
            block.pack(side="left", padx=(0, 28))
            ttk.Label(block, text=label, style="Panel.TLabel").pack(anchor="w")
            value = ttk.Label(block, text="--", style="Value.TLabel")
            value.pack(anchor="w")
            self.status_values[key] = value

        body = ttk.Frame(self.root, padding=(18, 14))
        body.pack(side="top", fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        viewer = ttk.Frame(body, style="Panel.TFrame", padding=1)
        viewer.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        viewer.rowconfigure(0, weight=1)
        viewer.columnconfigure(0, weight=1)
        self.video_label = tk.Label(
            viewer,
            text="等待开始预览",
            bg="#07090b",
            fg=MUTED,
            font=("Segoe UI", 14),
            compound="center",
        )
        self.video_label.grid(row=0, column=0, sticky="nsew")
        # Resizes are debounced: rendering on every <Configure> made dragging the
        # window laggy and, because setting an image changes the label's requested
        # size, caused a visible re-layout/re-render settling loop.
        self.video_label.bind("<Configure>", self._on_video_configure)

        telemetry = ttk.Frame(viewer, style="Panel.TFrame", padding=(12, 9))
        telemetry.grid(row=1, column=0, sticky="ew")
        self.telemetry_values: dict[str, ttk.Label] = {}
        for key, title in (
            ("target", "目标"),
            ("error", "横向误差"),
            ("radius", "半径比例"),
            ("command", "输出"),
            ("source", "来源"),
        ):
            cell = ttk.Frame(telemetry, style="Panel.TFrame")
            cell.pack(side="left", fill="x", expand=True)
            ttk.Label(cell, text=title, style="Panel.TLabel").pack(anchor="w")
            value = ttk.Label(cell, text="--", style="Value.TLabel")
            value.pack(anchor="w")
            self.telemetry_values[key] = value

        # Scrollable sidebar: the tuning/obstacle/manual panels overflow small
        # windows, so they live in a canvas-driven scroll container.
        sidebar_shell = ttk.Frame(body, style="Panel.TFrame")
        sidebar_shell.grid(row=0, column=1, sticky="nsew")
        sidebar_shell.rowconfigure(0, weight=1)
        sidebar_shell.columnconfigure(0, weight=1)
        self.sidebar_canvas = tk.Canvas(sidebar_shell, bg=PANEL, highlightthickness=0, bd=0)
        self.sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        sidebar_scrollbar = ttk.Scrollbar(
            sidebar_shell, orient="vertical", command=self.sidebar_canvas.yview
        )
        sidebar_scrollbar.grid(row=0, column=1, sticky="ns")
        self.sidebar_canvas.configure(yscrollcommand=sidebar_scrollbar.set)
        self.sidebar_canvas.bind(
            "<Configure>",
            lambda event: self.sidebar_canvas.itemconfigure(
                self._sidebar_window, width=event.width
            ),
        )
        # Wheel scrolling only while the pointer is over the sidebar, so the
        # video view never loses its events.
        self.sidebar_canvas.bind("<Enter>", self._bind_sidebar_wheel)
        self.sidebar_canvas.bind("<Leave>", self._unbind_sidebar_wheel)

        sidebar = ttk.Frame(self.sidebar_canvas, style="Panel.TFrame", padding=14)
        self._sidebar_window = self.sidebar_canvas.create_window(
            (0, 0), window=sidebar, anchor="nw"
        )
        sidebar.bind(
            "<Configure>",
            lambda _event: self.sidebar_canvas.configure(
                scrollregion=self.sidebar_canvas.bbox("all")
            ),
        )
        sidebar.columnconfigure(1, weight=1)

        self.source_var = tk.StringVar(value="synthetic")
        self.backend_var = tk.StringVar(value="recording")
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="5075")
        self.config_var = tk.StringVar(value=str(self.config_path))

        self._field(sidebar, 0, "视频源", self.source_var, browse=True)
        ttk.Label(
            sidebar,
            text="可填 synthetic、摄像头编号、视频路径或 MJPEG URL",
            style="PanelMuted.TLabel",
            wraplength=300,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(sidebar, text="控制后端", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", pady=4
        )
        backend = ttk.Combobox(
            sidebar, textvariable=self.backend_var, values=("recording", "tcp"), state="readonly"
        )
        backend.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        self._field(sidebar, 3, "机器人地址", self.host_var)
        self._field(sidebar, 4, "端口", self.port_var)
        # Optional command mirror, e.g. 127.0.0.1:5075 for the Unity twin.
        self.mirror_var = tk.StringVar(value="")
        self._field(sidebar, 5, "镜像地址", self.mirror_var)
        ttk.Label(
            sidebar,
            text="镜像地址可留空；填 host:port 后每条命令同时发一份（如本机 Unity 孪生）",
            style="PanelMuted.TLabel",
            wraplength=300,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._field(sidebar, 7, "配置文件", self.config_var, browse=True, config_file=True)

        ttk.Separator(sidebar).grid(row=8, column=0, columnspan=3, sticky="ew", pady=10)
        self._build_tuning_panel(sidebar, row=9)
        ttk.Separator(sidebar).grid(row=11, column=0, columnspan=3, sticky="ew", pady=10)
        self._build_obstacle_panel(sidebar, row=12)
        ttk.Separator(sidebar).grid(row=14, column=0, columnspan=3, sticky="ew", pady=10)
        self._build_manual_panel(sidebar, row=15)
        ttk.Separator(sidebar).grid(row=17, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(sidebar, text="运行日志", style="PanelTitle.TLabel").grid(
            row=18, column=0, columnspan=3, sticky="w"
        )
        log_frame = ttk.Frame(sidebar, style="Panel.TFrame")
        log_frame.grid(row=19, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        sidebar.rowconfigure(19, weight=1)
        self.log_text = tk.Text(
            log_frame,
            height=8,
            bg="#0c1012",
            fg="#c8d2d7",
            insertbackground=TEXT,
            relief="flat",
            state="disabled",
            wrap="word",
            font=("Cascadia Mono", 9),
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_tuning_panel(self, sidebar: ttk.Frame, row: int) -> None:
        ttk.Label(sidebar, text="参数调优", style="PanelTitle.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        panel = ttk.Frame(sidebar, style="Panel.TFrame")
        panel.grid(row=row + 1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        for column in (1, 2, 3):
            panel.columnconfigure(column, weight=1, uniform="tuning")
        self.tuning_vars = {
            key: tk.StringVar(value=value) for key, value in _default_tuning().items()
        }

        def entry(grid_row: int, column: int, key: str) -> None:
            ttk.Entry(panel, textvariable=self.tuning_vars[key], width=6).grid(
                row=grid_row, column=column, sticky="ew", pady=2, padx=2
            )

        def label(grid_row: int, column: int, text: str) -> None:
            ttk.Label(panel, text=text, style="Panel.TLabel").grid(
                row=grid_row, column=column, sticky="w", pady=2, padx=(0, 4)
            )

        label(0, 0, "LAB下限")
        entry(0, 1, "lab_min_l")
        entry(0, 2, "lab_min_a")
        entry(0, 3, "lab_min_b")
        label(1, 0, "LAB上限")
        entry(1, 1, "lab_max_l")
        entry(1, 2, "lab_max_a")
        entry(1, 3, "lab_max_b")
        label(2, 0, "最小面积")
        entry(2, 1, "minimum_contour_area")
        label(2, 2, "圆度")
        entry(2, 3, "minimum_circularity")
        label(3, 0, "长宽比下限")
        entry(3, 1, "minimum_aspect_ratio")
        label(3, 2, "长宽比上限")
        entry(3, 3, "maximum_aspect_ratio")
        label(4, 0, "确认帧数")
        entry(4, 1, "confirmation_frames")
        label(4, 2, "释放帧数")
        entry(4, 3, "release_frames")
        label(5, 0, "对准进入误差")
        entry(5, 1, "align_enter_error")
        label(5, 2, "对准退出误差")
        entry(5, 3, "align_exit_error")
        label(6, 0, "到达半径比")
        entry(6, 1, "arrival_radius_ratio")
        label(7, 0, "红核A下限")
        entry(7, 1, "core_a_min")
        label(7, 2, "红核占比")
        entry(7, 3, "minimum_core_fraction")
        ttk.Label(
            panel,
            text="LAB 顺序为 L/A/B；红核占比设 0 关闭该门控；点“应用参数”后热更新",
            style="PanelMuted.TLabel",
            wraplength=300,
        ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 2))
        buttons = ttk.Frame(panel, style="Panel.TFrame")
        buttons.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(0, 2))
        self.apply_params_button = ttk.Button(
            buttons,
            text="应用参数",
            style="Accent.TButton",
            width=10,
            command=self._apply_params,
        )
        self.apply_params_button.pack(side="left")
        self.apply_params_button.configure(state="disabled")
        ttk.Button(buttons, text="恢复默认", width=10, command=self._restore_params).pack(
            side="left", padx=8
        )

    def _build_obstacle_panel(self, sidebar: ttk.Frame, row: int) -> None:
        self.obstacle_panel = ttk.Labelframe(
            sidebar, text=" 避障 ", style="TLabelframe", padding=(10, 6)
        )
        self.obstacle_panel.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        # Default comes from config [obstacle] enabled (set in _load_defaults);
        # the switch is only read when a session starts, so it freezes mid-run.
        self.obstacle_var = tk.BooleanVar(value=False)
        self.obstacle_checkbutton = ttk.Checkbutton(
            self.obstacle_panel,
            text="启用避障",
            variable=self.obstacle_var,
            style="Panel.TCheckbutton",
        )
        self.obstacle_checkbutton.pack(anchor="w")
        grid = ttk.Frame(self.obstacle_panel, style="Panel.TFrame")
        grid.pack(anchor="w", fill="x", pady=(4, 0))
        self.obstacle_values: dict[str, ttk.Label] = {}
        specs = (
            ("distance", "距离(mm)"),
            ("vision", "视觉受阻"),
            ("state", "策略状态"),
            ("avoids", "避障次数"),
        )
        for index, (key, title) in enumerate(specs):
            cell = ttk.Frame(grid, style="Panel.TFrame")
            cell.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 18), pady=1)
            ttk.Label(cell, text=title, style="Panel.TLabel").pack(anchor="w")
            value = ttk.Label(cell, text="--", style="Value.TLabel")
            value.pack(anchor="w")
            self.obstacle_values[key] = value

    def _build_manual_panel(self, sidebar: ttk.Frame, row: int) -> None:
        ttk.Label(sidebar, text="手动测试", style="PanelTitle.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        panel = ttk.Frame(sidebar, style="Panel.TFrame")
        panel.grid(row=row + 1, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        action_row = ttk.Frame(panel, style="Panel.TFrame")
        action_row.pack(anchor="w")
        nod = ttk.Button(action_row, text="点头", width=8, command=lambda: self._send_action("nod"))
        nod.pack(side="left")
        shake = ttk.Button(
            action_row, text="摇头", width=8, command=lambda: self._send_action("shake")
        )
        shake.pack(side="left", padx=8)
        # Course button namespace: works on both TCP_connect.py and tonypi_server.
        up_front = ttk.Button(
            action_row, text="前爬起", width=8, command=lambda: self._send_action("left_trigger")
        )
        up_front.pack(side="left")
        up_back = ttk.Button(
            action_row, text="后爬起", width=8, command=lambda: self._send_action("right_trigger")
        )
        up_back.pack(side="left", padx=8)
        self.manual_buttons = [nod, shake, up_front, up_back]
        ttk.Label(
            panel,
            text="爬起/灭火发课程 CMD（left_trigger/right_trigger/right_grip）；点头/摇头需 robot_side 扩展服务",
            style="PanelMuted.TLabel",
            wraplength=300,
        ).pack(anchor="w", pady=(3, 2))
        extinguish = ttk.Button(
            panel, text="蹲下灭火", width=10, command=lambda: self._send_action("right_grip")
        )
        extinguish.pack(anchor="w", pady=(0, 5))
        self.manual_buttons.append(extinguish)

        jog_row = ttk.Frame(panel, style="Panel.TFrame")
        jog_row.pack(anchor="w")
        jog_specs = (
            ("前进", MANUAL_JOG_LEVEL, 0.0),
            ("后退", -MANUAL_JOG_LEVEL, 0.0),
            ("左转", 0.0, -MANUAL_JOG_LEVEL),
            ("右转", 0.0, MANUAL_JOG_LEVEL),
        )
        for text, velocity, steer in jog_specs:
            button = ttk.Button(
                jog_row,
                text=text,
                width=6,
                command=lambda v=velocity, s=steer: self._send_manual(v, s, MANUAL_JOG_SECONDS),
            )
            button.pack(side="left", padx=(0, 6))
            self.manual_buttons.append(button)
        stop_jog = ttk.Button(
            panel,
            text="停止点动",
            width=10,
            command=lambda: self._send_manual(0.0, 0.0, MANUAL_STOP_SECONDS),
        )
        stop_jog.pack(anchor="w", pady=(6, 0))
        self.manual_buttons.append(stop_jog)

        ttk.Separator(panel).pack(fill="x", pady=(8, 4))
        gamepad_row = ttk.Frame(panel, style="Panel.TFrame")
        gamepad_row.pack(anchor="w")
        ttk.Label(gamepad_row, text="手柄", style="Panel.TLabel").pack(side="left")
        self.gamepad_status_value = ttk.Label(gamepad_row, text="检测中", style="Value.TLabel")
        self.gamepad_status_value.pack(side="left", padx=(10, 16))
        ttk.Label(gamepad_row, text="模式", style="Panel.TLabel").pack(side="left")
        self.gamepad_mode_value = ttk.Label(gamepad_row, text="--", style="Value.TLabel")
        self.gamepad_mode_value.pack(side="left", padx=(10, 0))
        self.gamepad_axes_value = ttk.Label(
            panel, text="摇杆 v=+0.00 steer=+0.00", style="PanelMuted.TLabel"
        )
        self.gamepad_axes_value.pack(anchor="w", pady=(3, 0))
        ttk.Label(
            panel,
            text=f"{TOGGLE_HINT}（XInput 布局；Windows 免依赖，其他系统需 pygame）",
            style="PanelMuted.TLabel",
            wraplength=300,
        ).pack(anchor="w", pady=(3, 0))
        for button in self.manual_buttons:
            button.configure(state="disabled")

    def _field(
        self,
        parent,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        browse: bool = False,
        config_file: bool = False,
    ) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(
            row=row, column=0, sticky="w", pady=4
        )
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4, padx=(8, 5))
        if browse:
            command = self._browse_config if config_file else self._browse_video
            ttk.Button(parent, text="选择", command=command).grid(row=row, column=2, pady=4)

    def _load_defaults(self) -> None:
        try:
            config = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001 - a missing/broken config stays a resettable fault.
            self.model.fail(f"{type(exc).__name__}: {exc}")
            return
        self.source_var.set(str(config["video"]["source"]))
        self.backend_var.set(str(config["robot"]["backend"]))
        self.host_var.set(str(config["robot"]["host"]))
        self.port_var.set(str(config["robot"]["port"]))
        obstacle = config.get("obstacle")
        # Missing section/key defaults to enabled, matching the CLI and the
        # worker-side policy builder; config.toml stays the off switch.
        self.obstacle_var.set(
            bool(obstacle.get("enabled", True)) if isinstance(obstacle, dict) else True
        )
        try:
            self._set_tuning_from_config(config)
        except Exception as exc:  # noqa: BLE001 - fall back to the dataclass defaults.
            self.model.append_log(f"读取调优参数失败，使用内置默认：{type(exc).__name__}: {exc}")

    def _set_tuning_from_config(self, config: dict) -> None:
        detection = config["detection"]
        controller = config["controller"]
        lab_min = tuple(detection["lab_min"])
        lab_max = tuple(detection["lab_max"])
        values: dict[str, object] = {
            "lab_min_l": lab_min[0],
            "lab_min_a": lab_min[1],
            "lab_min_b": lab_min[2],
            "lab_max_l": lab_max[0],
            "lab_max_a": lab_max[1],
            "lab_max_b": lab_max[2],
            "minimum_contour_area": detection["minimum_contour_area"],
            "minimum_circularity": detection["minimum_circularity"],
            "minimum_aspect_ratio": detection["minimum_aspect_ratio"],
            "maximum_aspect_ratio": detection["maximum_aspect_ratio"],
            "confirmation_frames": detection["confirmation_frames"],
            "release_frames": detection["release_frames"],
            "core_a_min": detection["core_a_min"],
            "minimum_core_fraction": detection["minimum_core_fraction"],
            "align_enter_error": controller["align_enter_error"],
            "align_exit_error": controller["align_exit_error"],
            "arrival_radius_ratio": controller["arrival_radius_ratio"],
        }
        for key, value in values.items():
            self.tuning_vars[key].set(str(value))

    def _browse_video(self) -> None:
        selected = filedialog.askopenfilename(title="选择视频文件")
        if selected:
            self.source_var.set(selected)

    def _browse_config(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择配置文件", filetypes=(("TOML", "*.toml"), ("全部", "*.*"))
        )
        if selected:
            self.config_var.set(selected)

    def _demo_start(self) -> None:
        """Demo mode entry: auto-start the preview session once the window is up."""
        if self._closing:
            return
        if not self.model.can_start:
            self.root.after(200, self._demo_start)
            return
        self._start_session()

    def _demo_try_arm(self) -> None:
        """Arm automatically in demo mode, but only for a loopback TCP backend."""
        if self.demo_auto_arm_done or not self.model.can_arm or self.session_control is None:
            return
        if not _auto_arm_allowed(self.backend_var.get(), self.host_var.get().strip()):
            self.demo_auto_arm_done = True
            self.model.append_log(
                "演示模式：当前后端不是本机 TCP，保持未武装；请手动点击“武装自治”"
            )
            self._sync_logs()
            return
        self._arm()
        self.demo_auto_arm_done = True

    def _start_session(self) -> None:
        if not self.model.can_start:
            return
        mirror_text = self.mirror_var.get().strip()
        if mirror_text:
            try:
                mirror_endpoint = parse_endpoint(mirror_text)
            except ValueError as exc:
                self.model.append_log(f"镜像地址无效：{exc}")
                self._refresh_view()
                return
        else:
            mirror_endpoint = None
        values = {
            "config": self.config_var.get().strip(),
            "source": self.source_var.get().strip(),
            "backend": self.backend_var.get(),
            "host": self.host_var.get().strip(),
            "port": self.port_var.get().strip(),
            "mirror": mirror_endpoint,
            "obstacle": bool(self.obstacle_var.get()),
        }
        self.model.begin_start(obstacle_enabled=values["obstacle"])
        self._clear_frame_view()
        self.active_detector = None
        self.active_controller = None
        self._refresh_view()
        self.session_control = SessionControl()
        self.session_thread = threading.Thread(
            target=self._session_worker,
            args=(values, self.session_control),
            name="robot-session",
            daemon=True,
        )
        self.session_thread.start()

    def _session_worker(self, values: dict[str, Any], control: SessionControl) -> None:
        source = None
        robot = None
        unity_publisher = None
        run_loop_entered = False
        try:
            config = load_config(Path(values["config"]))
            unity_publisher = UnityStatusPublisher(unity_status_config(config.get("unity")))
            # Built before any hardware/video resource is opened, so a bad
            # obstacle config surfaces through the normal error event path.
            obstacle_policy = self._build_obstacle_policy(
                config,
                values["backend"],
                bool(values["obstacle"]),
            )

            def open_source():
                return build_source(
                    values["source"], config["video"], realtime=values["source"] == "synthetic"
                )

            if values["source"].startswith(("http://", "https://")) or values["source"].isdigit():
                # Live MJPEG/webcam over flaky WiFi: reconnect instead of dying.
                # Video files keep their video_ended semantics.
                source = RetryingSource(
                    open_source,
                    on_event=lambda message: self._publish_event(RuntimeEvent("warning", message)),
                )
                retry_video = True
            else:
                source = open_source()
                retry_video = False
            if values["backend"] == "tcp":
                robot = TcpRobotClient(
                    values["host"],
                    int(values["port"]),
                    float(config["robot"]["connect_timeout_seconds"]),
                    float(config["robot"]["send_interval_seconds"]),
                )
                if values["mirror"] is not None:
                    mirror_host, mirror_port = values["mirror"]
                    if same_endpoint(
                        values["host"], int(values["port"]), (mirror_host, mirror_port)
                    ):
                        # The 5075 server keeps ONE client: mirroring to the
                        # primary itself would kick our own session in a loop.
                        self._publish_event(
                            RuntimeEvent(
                                "warning",
                                "镜像地址与机器人相同，已忽略；镜像应指向 Unity 等另一个端点",
                            )
                        )
                    else:
                        mirror = MirrorTcpRobot(
                            mirror_host,
                            mirror_port,
                            connect_timeout=1.0,
                            on_status=self._publish_mirror_log,
                        )
                        robot = FanoutRobot(robot, (mirror,))
            else:
                robot = RecordingRobot()
            # Attach before connect so Stop/E-stop can cancel a pending TCP start.
            control.attach_robot(robot)
            if values["backend"] == "tcp":
                robot.connect()
            detector = RedBallDetector(detector_config(config["detection"]))
            controller = VisualApproachController(controller_config(config["controller"]))
            self.active_detector = detector
            self.active_controller = controller
            run_kwargs: dict[str, Any] = {}
            if obstacle_policy is not None:
                run_kwargs["obstacle_policy"] = obstacle_policy
            run_loop_entered = True
            run_loop(
                source,
                detector,
                controller,
                robot,
                armed=False,
                frame_timeout_seconds=float(config["video"]["frame_timeout_seconds"]),
                session=control,
                event_sink=fanout_event_sinks(self._publish_event, unity_publisher),
                frame_recorder=self.frame_recorder,
                video_retry=retry_video,
                **run_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - worker reports errors to the GUI.
            cancelled_start = control.stopped and not run_loop_entered
            message = f"{type(exc).__name__}: {exc}"
            if not run_loop_entered:
                # Once run_loop is entered, its finally owns the cleanup (and may
                # still raise its own cleanup_error afterwards); before that, a
                # leaked TCP connection would lock every later session out of the
                # single-connection robot_side server until the GUI restarts.
                if robot is not None:
                    try:
                        robot.close()
                    except Exception as cleanup_exc:  # noqa: BLE001 - keep cleaning up.
                        message += f"; robot cleanup failed: {cleanup_exc}"
                if source is not None:
                    try:
                        source.close()
                    except Exception as cleanup_exc:  # noqa: BLE001 - keep cleaning up.
                        message += f"; source cleanup failed: {cleanup_exc}"
            event = (
                RuntimeEvent("finished", "stop_requested")
                if cancelled_start
                else RuntimeEvent("error", message)
            )
            self._publish_event(event)
            if unity_publisher is not None:
                unity_publisher.publish(event)
        finally:
            if unity_publisher is not None:
                unity_publisher.close()

    def _build_obstacle_policy(self, config: dict, backend: str, enabled: bool):
        """Obstacle policy for this session, or None when reactive avoidance is off.

        The navigation import is deliberately lazy: it only happens for tcp
        sessions with the switch on, and any failure (missing module, invalid
        config) propagates to the worker's error path instead of killing the GUI.
        An explicit ``enabled = false`` in config.toml wins over the switch and
        is logged instead of silently skipped; a missing section/key defaults to
        enabled, matching the CLI.
        """
        if backend != "tcp" or not enabled:
            return None
        obstacle_section = config.get("obstacle")
        if obstacle_section is None:
            obstacle_section = {}
        if not isinstance(obstacle_section, dict) or not obstacle_section.get("enabled", True):
            self._publish_event(RuntimeEvent("state", "避障已被配置文件禁用；本次会话不启用避障"))
            return None
        from hcirobot.navigation import ObstaclePolicy, obstacle_config

        return ObstaclePolicy(obstacle_config(obstacle_section))

    def _publish_event(self, event: RuntimeEvent) -> None:
        if event.kind == "frame":
            self.latest_frame = event
            return
        self.events.put(event)

    def _publish_gamepad_log(self, message: str) -> None:
        """Monitor-thread entry point; the events queue is the thread-safe hop."""
        if not self._closing:
            self.events.put(RuntimeEvent("gamepad", message))

    def _publish_capture_log(self, message: str) -> None:
        """Recorder-writer-thread entry point into the mainloop."""
        if not self._closing:
            self.events.put(RuntimeEvent("capture", message))

    def _publish_mirror_log(self, message: str) -> None:
        """Session-thread mirror status hop into the mainloop."""
        if not self._closing:
            self.events.put(RuntimeEvent("mirror", message))

    def _toggle_capture(self) -> None:
        active, _saved, _dropped, _directory = self.frame_recorder.snapshot()
        if active:
            saved, dropped, directory = self.frame_recorder.stop_session()
            self.model.append_log(f"样本采集已停止：{saved} 帧保存至 {directory}（丢弃 {dropped}）")
        elif self.model.session_state is SessionState.RUNNING:
            directory = self.frame_recorder.start_session()
            self.model.append_log(f"样本采集已开始（原始帧 PNG）：{directory}")
        self._sync_logs()
        self._refresh_view()

    def _report_gamepad_status_once(self) -> None:
        if self._closing or self._gamepad_status_reported:
            return
        self._gamepad_status_reported = True
        connected, text = self.gamepad_monitor.status()
        if not connected:
            self.model.append_log(f"手柄：{text}；可用后自动连接（{TOGGLE_HINT}）")
            self._sync_logs()

    def _poll_gamepad(self) -> None:
        try:
            self.gamepad_teleop.poll(
                self.session_control,
                armed=self.model.armed,
                live=self.model.session_state is SessionState.RUNNING,
                can_arm=self.model.can_arm,
                can_manual=self.model.can_manual,
                log=self.model.append_log,
            )
        except Exception as exc:  # noqa: BLE001 - teleop must never kill the GUI tick.
            self.model.append_log(f"手柄轮询异常：{type(exc).__name__}: {exc}")

    def _drain_events(self) -> None:
        try:
            self._drain_pending()
        except Exception as exc:  # noqa: BLE001 - a GUI tick must never kill the mainloop.
            try:
                self.model.append_log(f"界面刷新异常：{type(exc).__name__}: {exc}")
                self._sync_logs()
            except Exception as log_exc:  # noqa: BLE001 - even logging must not raise here.
                print(f"gui drain error: {exc!r}; logging failed: {log_exc!r}", file=sys.stderr)
        if self._closing and (self.session_thread is None or not self.session_thread.is_alive()):
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            return
        try:
            self.root.after(30, self._drain_events)
        except tk.TclError:
            pass

    def _drain_pending(self) -> None:
        # Queue events are drained first so started/finished ordering wins over
        # frame data, which bypasses the queue via self.latest_frame.
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(event)
            except Exception as exc:  # noqa: BLE001 - one bad event must not kill the loop.
                self.model.append_log(f"事件处理异常：{type(exc).__name__}: {exc}")
        frame_event, self.latest_frame = self.latest_frame, None
        if frame_event is not None and self._should_show_frame(frame_event):
            try:
                self.model.apply_event(frame_event)
                self._render_frame(frame_event)
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the loop.
                self.model.append_log(f"帧处理异常：{type(exc).__name__}: {exc}")
        if self.demo:
            self._demo_try_arm()
        self._poll_gamepad()
        self._refresh_view()

    def _handle_event(self, event: RuntimeEvent) -> None:
        # "started" binds the session identity (the model applies it before its
        # own filter), so it must bypass the staleness check here as well.
        if event.kind != "started" and not self._event_matches_session(event):
            return
        if event.kind == "error":
            self.model.fail(event.message)
            self._clear_frame_view("会话已结束")
            return
        self.model.apply_event(event)
        if event.kind in ("finished", "estop"):
            self._clear_frame_view("会话已结束")

    def _event_matches_session(self, event: RuntimeEvent) -> bool:
        return event.session_id in (0, self.model.session_id)

    def _should_show_frame(self, event: RuntimeEvent) -> bool:
        return (
            self._event_matches_session(event) and self.model.session_state is SessionState.RUNNING
        )

    def _clear_frame_view(self, message: str = "等待开始预览") -> None:
        self.latest_frame = None
        self._photo = None
        try:
            self.video_label.configure(image="", text=message)
        except tk.TclError:
            pass

    def _render_frame(self, event: RuntimeEvent) -> None:
        if event.frame is None:
            return
        try:
            rgb = cv2.cvtColor(event.frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            width = max(320, self.video_label.winfo_width())
            height = max(240, self.video_label.winfo_height())
            self._rendered_view_size = (width, height)
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            self._photo = ImageTk.PhotoImage(image)
            self.video_label.configure(image=self._photo, text="")
        except Exception as exc:  # noqa: BLE001 - a corrupt frame must not kill the GUI.
            self._photo = None
            self.model.append_log(f"帧渲染失败：{type(exc).__name__}: {exc}")
            try:
                self.video_label.configure(image="", text="帧渲染失败")
            except tk.TclError:
                pass

    def _render_latest(self) -> None:
        event = self.latest_frame
        if event is None or not self._should_show_frame(event):
            return
        self._render_frame(event)

    def _on_video_configure(self, event: tk.Event) -> None:
        """Debounce resize events into a single re-render per size change."""
        size = (event.width, event.height)
        if size <= (1, 1) or size == self._pending_view_size:
            return
        self._pending_view_size = size
        if self._resize_after_id is None:
            self._resize_after_id = self.root.after(80, self._render_after_resize)

    def _render_after_resize(self) -> None:
        self._resize_after_id = None
        if self._pending_view_size == self._rendered_view_size:
            return  # the periodic frame tick already rendered at this size
        self._render_latest()

    def _bind_sidebar_wheel(self, _event: tk.Event) -> None:
        self.sidebar_canvas.bind_all("<MouseWheel>", self._on_sidebar_wheel)

    def _unbind_sidebar_wheel(self, _event: tk.Event) -> None:
        self.sidebar_canvas.unbind_all("<MouseWheel>")

    def _on_sidebar_wheel(self, event: tk.Event) -> None:
        first, last = self.sidebar_canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return  # nothing to scroll
        self.sidebar_canvas.yview_scroll(-round(event.delta / 120), "units")

    def _apply_params(self) -> None:
        if not (
            self.model.session_state is SessionState.RUNNING
            and self.active_detector is not None
            and self.active_controller is not None
        ):
            self.model.append_log("参数未应用：需要运行中的会话")
            self._refresh_view()
            return
        try:
            detector_cfg = DetectorConfig(
                lab_min=(
                    self._tuning_int("lab_min_l"),
                    self._tuning_int("lab_min_a"),
                    self._tuning_int("lab_min_b"),
                ),
                lab_max=(
                    self._tuning_int("lab_max_l"),
                    self._tuning_int("lab_max_a"),
                    self._tuning_int("lab_max_b"),
                ),
                minimum_contour_area=self._tuning_float("minimum_contour_area"),
                minimum_circularity=self._tuning_float("minimum_circularity"),
                minimum_aspect_ratio=self._tuning_float("minimum_aspect_ratio"),
                maximum_aspect_ratio=self._tuning_float("maximum_aspect_ratio"),
                confirmation_frames=self._tuning_int("confirmation_frames"),
                release_frames=self._tuning_int("release_frames"),
                core_a_min=self._tuning_int("core_a_min"),
                minimum_core_fraction=self._tuning_float("minimum_core_fraction"),
            )
            controller_cfg = ControllerConfig(
                align_enter_error=self._tuning_float("align_enter_error"),
                align_exit_error=self._tuning_float("align_exit_error"),
                arrival_radius_ratio=self._tuning_float("arrival_radius_ratio"),
            )
            self.active_detector.update_config(detector_cfg)
            self.active_controller.update_config(controller_cfg)
            self.model.append_log("检测/控制参数已热更新")
        except Exception as exc:  # noqa: BLE001 - invalid values must never crash the GUI.
            self.model.append_log(f"参数应用失败：{type(exc).__name__}: {exc}")
        self._refresh_view()

    def _restore_params(self) -> None:
        try:
            config = load_config(Path(self.config_var.get().strip()))
            self._set_tuning_from_config(config)
            self.model.append_log("参数输入已恢复为配置文件数值")
        except Exception as exc:  # noqa: BLE001 - report and keep the current entries.
            self.model.append_log(f"恢复默认参数失败：{type(exc).__name__}: {exc}")
        self._refresh_view()

    def _tuning_int(self, key: str) -> int:
        return int(self.tuning_vars[key].get().strip())

    def _tuning_float(self, key: str) -> float:
        return float(self.tuning_vars[key].get().strip())

    def _send_action(self, name: str) -> None:
        control = self.session_control
        if control is None or not self.model.can_manual:
            return
        try:
            control.request_action(name)
            self.model.append_log(f"已请求手动动作：{name}")
        except ValueError as exc:
            self.model.append_log(f"手动动作被拒绝：{exc}")
        self._refresh_view()

    def _send_manual(self, velocity: float, steer: float, seconds: float) -> None:
        control = self.session_control
        if control is None or not self.model.can_manual:
            return
        try:
            control.request_manual(velocity, steer, seconds)
            self.model.append_log(
                f"手动点动：v={velocity:+.2f} steer={steer:+.2f} 持续 {seconds:.2f}s"
            )
        except ValueError as exc:
            self.model.append_log(f"手动点动被拒绝：{exc}")
        self._refresh_view()

    def _arm(self) -> None:
        if self.model.can_arm and self.session_control is not None:
            self.session_control.request_arm()
            self.arm_button.configure(state="disabled")

    def _stop(self) -> None:
        if self.session_control is not None:
            self.session_control.request_stop()
        self.model.request_stop()
        self._clear_frame_view("正在停止")
        self._refresh_view()

    def _estop(self) -> None:
        if self.session_control is not None:
            self.session_control.request_estop()
        self.model.latch_estop()
        self._clear_frame_view("正在停止")
        self._refresh_view()

    def _reset(self) -> None:
        self.model.reset()
        self._refresh_view()

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._resize_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._resize_after_id)
        self._stop()
        self.gamepad_monitor.stop()
        with contextlib.suppress(Exception):
            self.frame_recorder.stop_session()
        self.start_button.configure(state="disabled")
        self.arm_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.reset_button.configure(state="disabled")
        if self.session_thread is None or not self.session_thread.is_alive():
            self.root.destroy()

    def _refresh_view(self) -> None:
        model = self.model
        self.session_badge.configure(text=model.session_state.value)
        badge_color = GOOD if model.session_state is SessionState.RUNNING else PANEL_ALT
        if model.session_state is SessionState.FAILED or model.estop_latched:
            badge_color = DANGER
        self.session_badge.configure(bg=badge_color)
        self.status_values["video"].configure(text=model.video_status)
        self.status_values["robot"].configure(text=model.robot_status)
        self.status_values["control"].configure(text=model.control_state)
        gamepad_connected, gamepad_text = self.gamepad_monitor.status()
        self.status_values["gamepad"].configure(
            text=self._gamepad_status_summary(gamepad_connected, gamepad_text),
            foreground=GOOD if gamepad_connected else MUTED,
        )
        self.status_values["frames"].configure(text=str(model.frame_count))
        self.telemetry_values["target"].configure(text=model.target)
        self.telemetry_values["error"].configure(text=model.horizontal_error)
        self.telemetry_values["radius"].configure(text=model.radius_ratio)
        self.telemetry_values["command"].configure(text=model.command)
        self.telemetry_values["source"].configure(text=model.output_source)
        self.obstacle_values["distance"].configure(text=model.obstacle_distance)
        self.obstacle_values["vision"].configure(text=model.vision_blocked)
        self.obstacle_values["state"].configure(text=model.obstacle_state)
        self.obstacle_values["avoids"].configure(text=str(model.avoid_count))
        self.obstacle_values["state"].configure(
            foreground=DANGER if model.latched_blocked else ACCENT
        )
        self.obstacle_checkbutton.configure(
            state="normal" if model.can_toggle_obstacle else "disabled"
        )
        self.start_button.configure(state="normal" if model.can_start else "disabled")
        self.arm_button.configure(state="normal" if model.can_arm else "disabled")
        self.stop_button.configure(state="normal" if model.can_stop else "disabled")
        self.reset_button.configure(state="normal" if model.can_reset else "disabled")
        capture_active, capture_saved, _dropped, _dir = self.frame_recorder.snapshot()
        self.capture_button.configure(
            text=f"停止采集（{capture_saved}）" if capture_active else "采集样本",
            state="normal"
            if capture_active or model.session_state is SessionState.RUNNING
            else "disabled",
        )
        manual_state = "normal" if model.can_manual else "disabled"
        for button in self.manual_buttons:
            button.configure(state=manual_state)
        gamepad_connected, gamepad_text = self.gamepad_monitor.status()
        self.gamepad_status_value.configure(
            text=gamepad_text,
            foreground=GOOD if gamepad_connected else MUTED,
        )
        if model.session_state is not SessionState.RUNNING:
            gamepad_mode, mode_color = "未运行", MUTED
        elif model.armed:
            gamepad_mode, mode_color = "自主寻路", ACCENT
        else:
            gamepad_mode, mode_color = "手动控制", GOOD
        self.gamepad_mode_value.configure(text=gamepad_mode, foreground=mode_color)
        reading = self.gamepad_monitor.latest()
        axes_velocity, axes_steer = map_to_command(
            reading.drive_axis, reading.steer_axis, self.gamepad_teleop.deadzone
        )
        self.gamepad_axes_value.configure(
            text=f"摇杆 v={axes_velocity:+.2f} steer={axes_steer:+.2f}"
            if reading.connected
            else "摇杆 --（未连接）"
        )
        params_ready = (
            model.session_state is SessionState.RUNNING
            and self.active_detector is not None
            and self.active_controller is not None
        )
        self.apply_params_button.configure(state="normal" if params_ready else "disabled")
        self._sync_logs()

    @staticmethod
    def _gamepad_status_summary(connected: bool, text: str) -> str:
        """Short top-bar form of the monitor status; full text lives in the sidebar."""
        if connected:
            return text if len(text) <= 24 else text[:23] + "…"
        if "pygame" in text:
            return "未装 pygame"
        if "未检测到" in text:
            return "未检测到"
        return "未连接"

    def _sync_logs(self) -> None:
        # Version gate: comparing the whole text widget against the model every
        # 30 ms tick measurably slows the UI; only touch it when logs changed.
        if self._log_version == self.model.log_version:
            return
        self._log_version = self.model.log_version
        content = "\n".join(self.model.logs)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", content)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def _enable_windows_dpi_awareness() -> None:
    """Render at native pixels on high-DPI displays instead of Windows bitmap scaling.

    Without this the whole window is DPI-stretched by the system (blurry text,
    laggy redraws).  Point-based fonts still scale via tk scaling; the window
    geometry is scaled in _build_window to keep the layout proportions.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except (AttributeError, OSError):
            with contextlib.suppress(Exception):
                ctypes.windll.user32.SetProcessDPIAware()  # Windows 7 fallback
    except Exception:  # noqa: BLE001, S110 - cosmetic; must never block startup.
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    _enable_windows_dpi_awareness()
    parser = argparse.ArgumentParser(
        prog="hcirobot-gui",
        description="TonyPi 视觉自治控制台（支持 --config 指定配置、--demo 一键演示）",
    )
    parser.add_argument("--config", default="config.toml", help="TOML 配置文件路径")
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "演示模式：自动开始预览，并在条件满足时自动武装（仅当控制后端为本机 TCP 时才自动武装）"
        ),
    )
    args = parser.parse_args(argv)
    root = tk.Tk()
    RobotControlApp(root, Path(args.config), demo=args.demo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
