#!/usr/bin/env python3
"""TonyPi 机器人端扩展 TCP 服务（课程 Example/TCP_connect.py 的超集）。

协议（与课程服务保持兼容）：
    JSONL 控制帧: {"v":float, "steer":float, "grab":bool, "t":"ISO8601"}\\n
        死区 0.20 → 离散动作，转向优先（v<0 支持后退）。
    CMD:<name>\\n
        课程原映射: right_grip→outfire, right_trigger→stand_up_back,
                    left_trigger→stand_up_front
        本服务新增: nod（点头）、shake（摇头）、stand（强制回到站立并停步）
        未知 CMD：忽略并计数（session.unknown_cmds）。
    DIST:<毫米>\\n
        本服务新增（PC→机器人方向只读）：每 DIST_INTERVAL_S（默认 0.3s）向当前
        客户端推送一拍超声波距离。99999 为课程 SDK 的"传感器未连接"哨兵值，
        原样发送，由 PC 端策略过滤。可用 TONYPI_SONAR_SIM 注入模拟源
        （详见 robot_side/README.md）。课程原版与 main 分支版均无此能力。

头部舵机（已对照课程 HiwonderSDK 核实）：
    Board.setPWMServoPulse(servo_id, pulse=1500, use_time=1000)
        servo_id ∈ {1, 2}；pulse 被钳位到 [500, 2500] µs；
        use_time 被钳位到 [20, 30000] ms。
    Functions/ColorTrack.py 用法：
        舵机 1 = 头部俯仰（y_dis，中立 1500，代码限 [1000, 2000]）
        舵机 2 = 头部偏航（x_dis，代码限 [500, 2500]）

运行模式：
    - 导入 hiwonder 失败，或环境变量 TONYPI_DRY_RUN=1 时，只打印动作序列，
      不接触任何舵机（无树莓派/SDK 的机器也能跑通协议逻辑）。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, TypeAlias

# ================== 配置区（与课程 TCP_connect.py 一致） ==================
HOST = os.environ.get("TONYPI_HOST", "0.0.0.0")
PORT = int(os.environ.get("TONYPI_PORT", "5075"))
UDP_COLOR_HOST = os.environ.get("TONYPI_UDP_COLOR_HOST", "127.0.0.1")
UDP_COLOR_PORT = int(os.environ.get("TONYPI_UDP_COLOR_PORT", "6001"))
DIST_INTERVAL_S = float(os.environ.get("TONYPI_DIST_INTERVAL", "0.3"))

DEADZONE = 0.20        # 摇杆死区
WATCHDOG_S = 0.60      # 通讯超时 -> 站立
STEP_INTERVAL_S = 0.30  # 连续步态节拍
ON_STAND_ONCE = True    # 切到站立时执行一次站立动作
CMD_COOLDOWN_S = 0.50   # 同名命令冷却（防抖）
PRINT_FWD = True        # 转发颜色时是否打印

# 动作组名（与 ActionGroups 下文件名一致；不要写后缀）
ACTION_FORWARD = "go_forward"
ACTION_BACK = "back"
ACTION_TURN_R = "turn_right"
ACTION_TURN_L = "turn_left"
ACTION_STAND = "stand"

# 课程原按钮映射（保持不变）
CMD_MAP = {
    "right_grip": "outfire",
    "right_trigger": "stand_up_back",
    "left_trigger": "stand_up_front",
}

# ================== 头部 PWM 舵机参数（已对照课程 SDK 核实） ==================
HEAD_PITCH_ID = 1    # ColorTrack.py: Board.setPWMServoPulse(1, y_dis, ...) 俯仰
HEAD_YAW_ID = 2      # ColorTrack.py: Board.setPWMServoPulse(2, x_dis, ...) 偏航
PULSE_MIN = 500      # Board.setPWMServoPulse 对 pulse 的硬钳位
PULSE_MAX = 2500
PITCH_CENTER = 1500  # ColorTrack.py: y_dis = 1500 中立位
PITCH_MIN = 1000     # ColorTrack.py 对俯仰的限幅
PITCH_MAX = 2000
YAW_CENTER = 1500    # 偏航中立位（课程从 yaml 读取，通常即 1500 附近）
YAW_MIN = 500        # ColorTrack.py 对偏航的限幅
YAW_MAX = 2500
NOD_AMPLITUDE = 150   # 点头摆幅（µs，±150 ≈ ±9°@500-2500 量程）
SHAKE_AMPLITUDE = 200  # 摇头摆幅（µs）
HEAD_STEP_MS = 250    # 每步转动时间（ms），位于 setPWMServoPulse 钳位范围内

Plan: TypeAlias = list[tuple[Any, ...]]


def dry_run_enabled() -> bool:
    return os.environ.get("TONYPI_DRY_RUN", "") == "1"


def _load_hardware() -> tuple[ModuleType | None, ModuleType | None]:
    """try-import hiwonder；dry-run 或导入失败时返回 (None, None)。"""
    if dry_run_enabled():
        return None, None
    try:
        from hiwonder import ActionGroupControl, Board
    except Exception as exc:  # noqa: BLE001 - 无 SDK 的环境必须能继续（dry-run）
        print("[WARN] hiwonder SDK not available, falling back to dry-run:", exc)
        return None, None
    return Board, ActionGroupControl


def _load_sonar() -> ModuleType | None:
    """try-import 超声波模块（hiwonder.Sonar）；dry-run 或导入失败时返回 None。

    与 _load_hardware 分开单独 try：有的镜像只缺 smbus2，舵机仍可用，
    距离遥测单独降级即可。
    """
    if dry_run_enabled():
        return None
    try:
        from hiwonder import Sonar
    except Exception as exc:  # noqa: BLE001 - 无 SDK 的环境必须能继续
        print("[WARN] hiwonder Sonar not available, DIST telemetry disabled:", exc)
        return None
    return Sonar


BOARD, AGC = _load_hardware()
SONAR_MODULE = _load_sonar()


def _hardware_present() -> bool:
    return BOARD is not None and not dry_run_enabled()


# ================== 纯逻辑（可单测，不依赖硬件） ==================
def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def vector_to_mode(v: float, steer: float, deadzone: float = DEADZONE) -> str:
    """连续控制向量 → 离散步态模式；转向优先（与课程 _handle_vector 一致）。"""
    if abs(steer) > deadzone:
        return "turn_r" if steer > 0 else "turn_l"
    if abs(v) > deadzone:
        return "forward" if v > 0 else "back"
    return "stand"


def head_swing(
    servo_id: int,
    center: int,
    amplitude: int,
    limits: tuple[int, int],
    step_ms: int = HEAD_STEP_MS,
) -> Plan:
    """小幅摆动序列：+amplitude → -amplitude → 回中立位（脉宽已按限幅钳位）。"""
    lo, hi = limits
    up = clamp(center + amplitude, lo, hi)
    down = clamp(center - amplitude, lo, hi)
    return [
        ("head", servo_id, up, step_ms),
        ("head", servo_id, down, step_ms),
        ("head", servo_id, clamp(center, lo, hi), step_ms),
    ]


def nod_plan() -> Plan:
    """点头：俯仰舵机（1 号）小幅摆动序列。"""
    return head_swing(HEAD_PITCH_ID, PITCH_CENTER, NOD_AMPLITUDE, (PITCH_MIN, PITCH_MAX))


def shake_plan() -> Plan:
    """摇头：偏航舵机（2 号）小幅摆动序列。"""
    return head_swing(HEAD_YAW_ID, YAW_CENTER, SHAKE_AMPLITUDE, (YAW_MIN, YAW_MAX))


@dataclass
class RobotSession:
    """单条连接的协议状态机；不碰 socket 与硬件，时间可注入便于单测。

    线程模型与课程服务一致：TCP 读线程与步态线程都会调用本对象，
    服务层（TonyPiService）负责加锁串行化。
    """

    now: Callable[[], float] = field(default=time.time)
    mode: str = "stand"
    last_rx_ts: float = 0.0
    unknown_cmds: int = 0
    last_cmd_ts: dict[str, float] = field(default_factory=dict)

    def handle_line(self, line: str) -> Plan:
        """处理一行 TCP 文本：优先 CMD，然后 JSON 控制（与课程 _process_line 一致）。"""
        text = (line or "").strip()
        if not text:
            return []
        if text.startswith("CMD:"):
            ts = self.now()
            self.last_rx_ts = ts
            return self.handle_cmd(text[4:], ts)
        try:
            msg = json.loads(text)
            if not isinstance(msg, dict):
                raise TypeError("payload is not a JSON object")
            v = float(msg.get("v", 0.0))
            steer = float(msg.get("steer", 0.0))
        except (TypeError, ValueError):
            return []  # 解析失败忽略（课程行为：打印 [PARSE] 后继续）
        self.last_rx_ts = self.now()
        return self._set_mode(vector_to_mode(v, steer))

    def handle_cmd(self, raw_cmd: str, ts: float | None = None) -> Plan:
        """处理 CMD:<name>；冷却防抖，未知命令忽略并计数。"""
        cmd = (raw_cmd or "").strip().lower()
        if not cmd:
            return []
        ts = self.now() if ts is None else ts
        # -inf 缺省：首条命令永远放行（课程用 time.time()，效果相同）
        if ts - self.last_cmd_ts.get(cmd, float("-inf")) < CMD_COOLDOWN_S:
            return []
        self.last_cmd_ts[cmd] = ts
        if cmd == "nod":
            return nod_plan()
        if cmd == "shake":
            return shake_plan()
        if cmd == "stand":
            # 新增语义：强制回到站立（停下连续步态并执行一次站立动作组）
            return self._set_mode("stand", force=True)
        action = CMD_MAP.get(cmd)
        if action is None:
            self.unknown_cmds += 1
            return [("unknown", cmd)]
        return [("group", action)]

    def watchdog_plan(self, ts: float | None = None) -> Plan:
        """看门狗：超过 WATCHDOG_S 未收到任何数据则回站立。"""
        ts = self.now() if ts is None else ts
        if (
            WATCHDOG_S > 0
            and self.last_rx_ts > 0
            and (ts - self.last_rx_ts) > WATCHDOG_S
            and self.mode != "stand"
        ):
            return self._set_mode("stand")
        return []

    def tick_group(self) -> str | None:
        """连续步态节拍：当前模式对应的动作组；站立态返回 None。"""
        return {
            "forward": ACTION_FORWARD,
            "back": ACTION_BACK,
            "turn_r": ACTION_TURN_R,
            "turn_l": ACTION_TURN_L,
        }.get(self.mode)

    def force_stand(self) -> Plan:
        """强制站立（force=True：即使已站立也执行一次站立动作组）。

        与课程一致：新连接接入时调用本方法；断开时用 :meth:`settle_stand`。
        """
        return self._set_mode("stand", force=True)

    def settle_stand(self) -> Plan:
        """非强制回站立（课程断开连接时的行为：已站立则不动）。"""
        return self._set_mode("stand")

    def _set_mode(self, new_mode: str, force: bool = False) -> Plan:
        if new_mode == self.mode and not force:
            return []
        self.mode = new_mode
        plan: Plan = [("mode", new_mode)]
        if new_mode == "stand" and ACTION_STAND and (ON_STAND_ONCE or force):
            plan.append(("group", ACTION_STAND))
        return plan


# ================== 执行器（dry-run 时只打印） ==================
def run_group(name: str, agc: ModuleType | None = None) -> None:
    """兼容 runActionGroup / runAction；无 SDK 或 dry-run 时打印。"""
    if not name:
        return
    if agc is None:
        agc = AGC
    if agc is None or dry_run_enabled():
        print(f"[DRY] run action group: {name}")
        return
    try:
        if hasattr(agc, "runActionGroup"):
            agc.runActionGroup(name)
        else:
            agc.runAction(name)
        print(f"[ACT] {name}")
    except Exception as exc:  # noqa: BLE001 - 动作失败不能拖垮服务
        print("[ERR] run action:", exc)


def move_head(servo_id: int, pulse: int, use_time_ms: int, board: ModuleType | None = None) -> None:
    """驱动头部 PWM 舵机；无 SDK 或 dry-run 时打印脉宽序列。"""
    if board is None:
        board = BOARD
    if board is None or dry_run_enabled():
        print(f"[DRY] head servo {servo_id} -> pulse {pulse} in {use_time_ms}ms")
        return
    try:
        board.setPWMServoPulse(servo_id, pulse, use_time_ms)
    except Exception as exc:  # noqa: BLE001 - 单次舵机故障不能拖垮服务
        print("[ERR] head servo:", exc)
        return
    time.sleep(use_time_ms / 1000.0 + 0.02)  # 等舵机到位（ColorTrack 同款做法）


def execute(plan: Plan, board: ModuleType | None = None, agc: ModuleType | None = None) -> None:
    """执行 RobotSession 产出的动作计划。"""
    for item in plan:
        kind = item[0]
        if kind == "group":
            run_group(item[1], agc=agc)
        elif kind == "head":
            move_head(item[1], item[2], item[3], board=board)
        elif kind == "mode":
            print(f"[MODE] {item[1]}")
        elif kind == "unknown":
            print(f"[CMD] {item[1]} (no mapping, ignored)")


# ================== 超声波距离遥测（本服务新增） ==================
# Sonar API 已对照课程 zip 原文（HiwonderSDK/hiwonder/Sonar.py）核实：
#   Sonar()            无构造参数（I2C bus 1, addr 0x77）
#   getDistance()      -> int 毫米；读数 >5000 钳位到 5000；
#                         I2C 异常（传感器未连接）时返回哨兵值 99999
def create_sonar() -> Any | None:
    """构造真实 Sonar 实例；无 SDK、dry-run 或初始化失败时返回 None。"""
    if SONAR_MODULE is None or dry_run_enabled():
        return None
    try:
        return SONAR_MODULE.Sonar()
    except Exception as exc:  # noqa: BLE001 - 传感器初始化失败不能拖垮服务
        print("[WARN] Sonar init failed, DIST telemetry disabled:", exc)
        return None


def parse_sonar_sim(spec: str) -> Callable[[float], int]:
    """解析 TONYPI_SONAR_SIM 模拟源；入参为相对连接建立时刻的秒数，返回毫米。

    支持的格式（确定性，便于测试）：
        flat:500                恒值 500
        sweep:100:800:2.0       min..max 三角波扫掠，period_s 为一个完整周期，
                                从 min 出发，半周期到达 max
        steps:500@1.0,200@3.5   分段恒值：t<1.0 → 500，[1.0,3.5) → 200，
                                ≥3.5 起最后一段无限延续
    """
    text = (spec or "").strip()
    parts = text.split(":")
    kind = parts[0].strip().lower()
    if kind == "flat" and len(parts) == 2:
        base = int(float(parts[1]))
        return lambda _t: base
    if kind == "sweep" and len(parts) == 4:
        lo, hi = int(float(parts[1])), int(float(parts[2]))
        period = float(parts[3])
        if period <= 0:
            raise ValueError("sweep period_s must be > 0")
        if hi < lo:
            raise ValueError("sweep needs min <= max")
        span = hi - lo

        def sweep(t: float) -> int:
            phase = (t % period) / period  # 0..1
            tri = 1.0 - abs(2.0 * phase - 1.0)  # 0 → 1 → 0 三角波
            return round(lo + span * tri)

        return sweep
    if kind == "steps" and len(parts) >= 2:
        # 语义：v1 生效于 [0, t1)，v2 生效于 [t1, t2)，依此类推；末段无限延续
        marks: list[tuple[float, int]] = []  # (值, 该值生效区间的结束时刻)
        for item in ":".join(parts[1:]).split(","):
            value_text, _, at_text = item.partition("@")
            marks.append((int(float(value_text)), float(at_text)))
        if not marks:
            raise ValueError("steps needs v1@t1[,v2@t2,...]")
        marks.sort(key=lambda mark: mark[1])

        def steps(t: float) -> int:
            value = marks[0][0]
            for idx in range(len(marks) - 1):
                if t >= marks[idx][1]:
                    value = marks[idx + 1][0]
                else:
                    break
            return value

        return steps
    raise ValueError(f"unsupported TONYPI_SONAR_SIM spec: {spec!r}")


def build_distance_source() -> Callable[[], int | None] | None:
    """选择距离遥测源：模拟源优先，其次真实 Sonar；都没有则 None（不发送）。

    dry-run（TONYPI_DRY_RUN=1）或导入失败时没有真实 Sonar，只有设置
    TONYPI_SONAR_SIM 才会发送 DIST。真实模式下 getDistance() 的异常由
    _distance_loop 捕获记日志并跳过该拍；99999 哨兵原样发送。
    """
    spec = os.environ.get("TONYPI_SONAR_SIM", "").strip()
    if spec:
        try:
            sample = parse_sonar_sim(spec)
        except ValueError as exc:
            print("[WARN] bad TONYPI_SONAR_SIM, DIST telemetry disabled:", exc)
            return None
        t0 = time.monotonic()  # steps/sweep 的时间原点 = 连接建立时刻
        return lambda: sample(time.monotonic() - t0)
    sonar = create_sonar()
    if sonar is not None:
        return lambda: int(sonar.getDistance())
    return None


# ================== 服务层（课程 TCP/UDP 结构保持不变） ==================
class TonyPiService:
    def __init__(self, host: str = HOST, port: int = PORT, session: RobotSession | None = None) -> None:
        self.host = host
        self.port = port
        self.session = session or RobotSession()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()  # 连接级写锁：串行化对当前连接的整行写
        self._stop = threading.Event()
        self._conn: socket.socket | None = None

    def stop(self) -> None:
        self._stop.set()

    def _send_line(self, conn: socket.socket, data: bytes, tag: str) -> bool:
        """在连接级写锁保护下发送一整行帧；返回是否发送成功。

        颜色转发与距离遥测两个线程可能并发写同一连接，统一走本函数
        串行化，避免 sendall 交叉破坏 "\\n" 行帧。
        """
        try:
            with self._write_lock:
                conn.sendall(data)
        except OSError as exc:
            print(f"[{tag}] send error:", exc)
            return False
        return True

    def _send_to_unity_text(self, text: str) -> None:
        """发送一行文本到当前客户端；若无连接则忽略（课程行为）。"""
        data = (text.strip() + "\n").encode("utf-8")
        with self._lock:
            conn = self._conn
        if not conn:
            return
        if self._send_line(conn, data, "FWD") and PRINT_FWD:
            print("[FWD] -> Unity:", text.strip())

    def _udp_color_listener(self) -> None:
        """监听本地 UDP 颜色信号并转发（课程行为，原样保留）。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((UDP_COLOR_HOST, UDP_COLOR_PORT))
        except OSError as exc:
            print("[UDP] bind error:", exc)
            return
        print(f"[UDP] Color listen on {UDP_COLOR_HOST}:{UDP_COLOR_PORT}")
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(1024)
            except TimeoutError:
                continue
            except OSError as exc:
                print("[UDP] error:", exc)
                continue
            tag = data.decode("utf-8", "ignore").strip().upper()
            if tag in ("RED", "GREEN"):
                self._send_to_unity_text(f"COLOR_SIGNAL:{tag}")
        sock.close()

    def _motion_loop(self) -> None:
        """后台循环：看门狗 + 按当前模式以节拍执行动作（课程行为）。"""
        while not self._stop.is_set():
            with self._lock:
                watchdog = self.session.watchdog_plan()
                group = self.session.tick_group()
            if watchdog:
                execute(watchdog)
            if group:
                run_group(group)
            self._stop.wait(STEP_INTERVAL_S)

    def _distance_loop(self, source: Callable[[], int | None], done: threading.Event) -> None:
        """连接期间每 DIST_INTERVAL_S 向当前客户端推送一拍 DIST:<毫米>。

        Sonar/模拟源抛异常记日志并跳过该拍；99999（课程 SDK"未连接"哨兵）
        原样发送，由 PC 端策略过滤。锁内取当前连接，发送统一经 :meth:`_send_line`
        的连接级写锁串行化，避免与颜色转发并发写交叉破坏行帧。
        """
        interval = DIST_INTERVAL_S if DIST_INTERVAL_S > 0 else 0.3
        while not self._stop.is_set() and not done.is_set():
            try:
                value = source()
            except Exception as exc:  # noqa: BLE001 - 传感器故障不能拖垮服务
                print("[DIST] read error:", exc)
                value = None
            if value is not None:
                data = f"DIST:{int(value)}\n".encode()
                with self._lock:
                    conn = self._conn
                if conn is not None and self._send_line(conn, data, "DIST"):
                    print("[DIST]", value, "mm")
            done.wait(interval)

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        buffer = b""
        with conn:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    continue
                except OSError as exc:
                    print("[TCP] conn error:", exc)
                    break
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", "ignore").strip()
                    if not text:
                        continue
                    with self._lock:
                        plan = self.session.handle_line(text)
                    execute(plan)

    def serve_forever(self) -> None:
        print(f"[TCP] Listening on {self.host}:{self.port}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(1)
            while not self._stop.is_set():
                srv.settimeout(1.0)
                try:
                    conn, addr = srv.accept()
                except TimeoutError:
                    continue
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    print("[TCP] accept error:", exc)
                    continue
                print("[TCP] Connected:", addr)
                with self._lock:
                    self._conn = conn
                    plan = self.session.force_stand()
                execute(plan)
                # 距离遥测：每个连接一个发送线程；模拟源时间原点 = 连接建立时刻
                distance_done = threading.Event()
                telemetry: threading.Thread | None = None
                source = build_distance_source()
                if source is not None:
                    telemetry = threading.Thread(
                        target=self._distance_loop,
                        args=(source, distance_done),
                        name="distance",
                        daemon=True,
                    )
                    telemetry.start()
                try:
                    self._handle_connection(conn)
                finally:
                    distance_done.set()
                    if telemetry is not None:
                        telemetry.join(timeout=1.0)
                print("[TCP] Disconnected:", addr)
                with self._lock:
                    self._conn = None
                    plan = self.session.settle_stand()
                execute(plan)

    def run(self) -> None:
        motion = threading.Thread(target=self._motion_loop, name="motion", daemon=True)
        motion.start()
        udp = threading.Thread(target=self._udp_color_listener, name="udp-color", daemon=True)
        udp.start()
        try:
            self.serve_forever()
        finally:
            self._stop.set()
            motion.join(timeout=1.0)
            udp.join(timeout=1.0)


def main() -> None:
    service = TonyPiService()

    def _on_sigint(signum: object, frame: object) -> None:
        print("\n[SYS] SIGINT, shutting down...")
        service.stop()

    signal.signal(signal.SIGINT, _on_sigint)
    if _hardware_present():
        print("[SYS] hardware mode (hiwonder SDK loaded)")
    else:
        print("[SYS] DRY-RUN: actions/head moves are printed only, servos untouched")
    service.run()
    print("[SYS] bye")


if __name__ == "__main__":
    main()
