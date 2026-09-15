"""Shared Tk/headless session assembly and resource cleanup.

Hosts provide _publish_event(), frame_recorder, and the active detector/controller
references. No desktop toolkit is imported here.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .app import RuntimeEvent, SessionControl, run_loop, run_manual_loop
from .cli import build_source
from .config import (
    ascend_yolo_config,
    controller_config,
    detector_config,
    load_config,
    startup_route_config,
    unity_status_config,
)
from .controller import VisualApproachController
from .detector import RedBallDetector
from .detector_profile import load_detector
from .robot import FanoutRobot, MirrorTcpRobot, RecordingRobot, TcpRobotClient, same_endpoint
from .startup_route import StartupRoute
from .unity_udp import UnityStatusPublisher, fanout_event_sinks
from .video import RetryingSource

FRAME_STALE_STOP_SECONDS = 0.35
DUPLICATE_FRAME_LIMIT = 5


class SessionRuntime:
    def _session_worker(self, values: dict[str, Any], control: SessionControl) -> None:
        def publish(event: RuntimeEvent) -> None:
            self._publish_event(replace(event, session_id=control.session_id))

        source = None
        detector = None
        robot = None
        unity_publisher = None
        run_loop_entered = False
        try:
            config = load_config(Path(values["config"]))
            unity_publisher = UnityStatusPublisher(unity_status_config(config.get("unity")))
            if values["control_only"]:
                robot = self._build_robot_backend(values, config, publish)
                control.attach_robot(robot)
                self._connect_robot_backend(values, robot, publish)
                self.active_detector = None
                self.active_controller = None
                run_loop_entered = True
                run_manual_loop(
                    robot,
                    session=control,
                    event_sink=fanout_event_sinks(publish, unity_publisher),
                )
                return
            # The mode selector is read once per session, like the obstacle switch.
            config["controller"] = dict(config["controller"], approach_mode=values["approach_mode"])
            detection = detector_config(config["detection"])
            if values["use_edge_model"] and Path(values["edge_model_path"]).suffix.lower() == ".om":
                from .ascend_yolo import AscendYoloDetector

                npu_values = dict(config.get("npu", {}), enabled=True, model=values["edge_model_path"])
                detector = AscendYoloDetector(ascend_yolo_config(npu_values), detection)
                publish(RuntimeEvent("state", f"识别器：Ascend NPU {values['edge_model_path']}"))
            elif values["use_edge_model"]:
                detector = load_detector(Path(values["edge_model_path"]), detection)
                publish(RuntimeEvent("state", f"识别器：端侧模型 {values['edge_model_path']}"))
            else:
                detector = RedBallDetector(detection)
                publish(RuntimeEvent("state", "识别器：LAB"))
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
                    on_event=lambda message: publish(RuntimeEvent("video_retry", message)),
                )
                retry_video = True
            else:
                source = open_source()
                retry_video = False
            robot = self._build_robot_backend(values, config, publish)
            # Attach before connect so Stop/E-stop can cancel a pending TCP start.
            control.attach_robot(robot)
            self._connect_robot_backend(values, robot, publish)
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
                event_sink=fanout_event_sinks(publish, unity_publisher),
                frame_recorder=self.frame_recorder,
                video_retry=retry_video,
                frame_stale_stop_seconds=min(
                    FRAME_STALE_STOP_SECONDS,
                    float(config["video"]["frame_timeout_seconds"]),
                ),
                duplicate_frame_limit=DUPLICATE_FRAME_LIMIT,
                preserve_manual_on_video_failure=True,
                resume_autonomy_on_video_recovery=True,
                startup_route=StartupRoute(startup_route_config(config.get("startup", {}))),
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
                if detector is not None:
                    close_detector = getattr(detector, "close", None)
                    if close_detector is not None:
                        try:
                            close_detector()
                        except Exception as cleanup_exc:  # noqa: BLE001
                            message += f"; detector cleanup failed: {cleanup_exc}"
            event = (
                RuntimeEvent("finished", "stop_requested")
                if cancelled_start
                else RuntimeEvent("error", message)
            )
            publish(event)
            if unity_publisher is not None:
                unity_publisher.publish(event)
        finally:
            if unity_publisher is not None:
                unity_publisher.close()

    def _build_robot_backend(self, values: dict[str, Any], config: dict, publish):
        if values["backend"] != "tcp":
            return RecordingRobot()
        robot = TcpRobotClient(
            values["host"],
            values["port"],
            float(config["robot"]["connect_timeout_seconds"]),
            float(config["robot"]["send_interval_seconds"]),
        )
        if values["mirror"] is None:
            return robot
        mirror_host, mirror_port = values["mirror"]
        if same_endpoint(values["host"], values["port"], (mirror_host, mirror_port)):
            publish(
                RuntimeEvent(
                    "warning",
                    "镜像地址与机器人相同，已忽略；镜像应指向 Unity 等另一个端点",
                )
            )
            return robot
        mirror = MirrorTcpRobot(
            mirror_host,
            mirror_port,
            connect_timeout=1.0,
            on_status=lambda message: publish(RuntimeEvent("mirror", message)),
        )
        return FanoutRobot(robot, (mirror,))

    @staticmethod
    def _connect_robot_backend(values: dict[str, Any], robot, publish) -> None:
        if values["backend"] != "tcp":
            return
        endpoint = f"{values['host']}:{values['port']}"
        publish(RuntimeEvent("state", f"正在连接机器人：{endpoint}"))
        try:
            robot.connect()
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ConnectionError(f"无法连接机器人 {endpoint}：{exc}") from exc
        publish(RuntimeEvent("state", f"机器人 TCP 已连接：{endpoint}"))

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
