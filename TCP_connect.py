#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机器人端：TCP 连续步态 + 按钮命令 + 颜色转发
Unity 每帧发送：{"v":float, "steer":float, "grab":bool, "t":"ISO8601"}\n
Unity 按钮发送：CMD:<name>\n   例如 CMD:chest
ColorDetect 本地 UDP(127.0.0.1:6001) 发送："RED"/"GREEN" → 转发到 Unity：COLOR_SIGNAL:RED/GREEN
"""

import socket
import json
import time
import threading
import signal
from typing import Optional

# ================== 配置区 ==================
HOST = "0.0.0.0"
PORT = 5075                  # 和 Unity 端一致
UDP_COLOR_HOST = "127.0.0.1"
UDP_COLOR_PORT = 6001        # ColorDetect.py 发来的本地 UDP 端口

DEADZONE        = 0.20       # 摇杆死区
ENTER_DZ        = 0.20
EXIT_DZ         = 0.30
WATCHDOG_S      = 0.60       # 通讯超时 -> 站立
# = 0.18       # 连续步态节拍（依动作时长微调）
STEP_INTERVAL_S = 0.30       # 连续步态节拍（依动作时长微调）
ON_STAND_ONCE   = True       # 切到站立是否执行一次站立动作
CMD_COOLDOWN_S  = 0.50       # 同名命令冷却（防抖）
PRINT_FWD       = True      # 转发颜色时是否打印

# 动作组名（与 ActionGroups 下文件名一致；不要写后缀）
ACTION_FORWARD = "go_forward"
ACTION_BACK    = "back"
ACTION_TURN_R  = "turn_right"
ACTION_TURN_L  = "turn_left"
ACTION_STAND   = "stand"

# 按钮命令映射：收到 CMD:<key> -> 执行对应动作
CMD_MAP = {
    "right_grip": "outfire",        # 可自行扩展： "wave":"wave", "kick":"kick"
    "right_trigger": "stand_up_back",
    "left_trigger": "stand_up_front",
}
# ===========================================


# ============== 兼容动作调用 ==============
AGC = None
try:
    import hiwonder.ActionGroupControl as AGC  # type: ignore
except Exception as e:
    print("[WARN] hiwonder.ActionGroupControl not found:", e)

def _run_group(name: Optional[str]) -> None:
    """兼容 runActionGroup / runAction；若 AGC 缺失则 dry-run 打印"""
    if not name:
        return
    if AGC is None:
        print(f"[DRY] would run: {name}")
        return
    try:
        if hasattr(AGC, "runActionGroup"):
            AGC.runActionGroup(name)
        else:
            AGC.runAction(name)
        print(f"[ACT] {name}")
    except Exception as e:
        print("[ERR] run action:", e)
# =========================================


# ============== 共享状态 ==============
_state_lock = threading.Lock()
_mode = "stand"               # 'forward'/'back'/'turn_r'/'turn_l'/'stand'
_last_rx_ts = 0.0
_last_cmd_ts = {}             # {cmd_name: last_time}
_stop_event = threading.Event()
_current_conn = None          # 当前 Unity 的 TCP 连接（socket 对象）
# =====================================


# ============== 核心逻辑 ==============
def _set_mode(new_mode: str, force: bool = False) -> None:
    """切换当前步态模式；进入 stand 时可选执行一次站立动作"""
    global _mode
    with _state_lock:
        if new_mode == _mode and not force:
            return
        _mode = new_mode
    print("[MODE]", _mode)
    if _mode == "stand" and ACTION_STAND and (ON_STAND_ONCE or force):
        _run_group(ACTION_STAND)

def _handle_vector(v: float, steer: float) -> None:
    global _mode
    """解析连续控制向量：转向优先（避免同时输入时只前进）"""
    av, as_ = abs(v), abs(steer)

    with _state_lock:
        current_m = _mode
    
    if current_m in ("turn_r", "turn_l"):
        if as_ > EXIT_DZ:
            _set_mode("turn_r" if steer > 0 else "turn_l")
            return
    
    if current_m in ("forward", "back"):
        if as_ < ENTER_DZ and av > EXIT_DZ:
            _set_mode("forward" if v > 0 else "back")
            return

    if as_ > ENTER_DZ:
        _set_mode("turn_r" if steer > 0 else "turn_l")
    elif av > ENTER_DZ:
        _set_mode("forward" if v > 0 else "back")
    elif av < EXIT_DZ and as_ < EXIT_DZ:
        _set_mode("stand")

def _motion_loop() -> None:
    """后台循环：根据当前模式以节拍执行动作"""
    while not _stop_event.is_set():
        with _state_lock:
            m = _mode
            rx = _last_rx_ts

        # 看门狗：超时回站立
        if WATCHDOG_S > 0 and rx > 0 and (time.time() - rx) > WATCHDOG_S and m != "stand":
            _set_mode("stand")

        # 按模式执行
        if m == "forward":
            _run_group(ACTION_FORWARD)
        elif m == "back":
            _run_group(ACTION_BACK)
        elif m == "turn_r":
            _run_group(ACTION_TURN_R)
        elif m == "turn_l":
            _run_group(ACTION_TURN_L)

        time.sleep(STEP_INTERVAL_S)

def _handle_cmd(cmd_raw: str) -> None:
    """处理 CMD:<name>；带冷却防抖，只执行一次动作"""
    cmd = (cmd_raw or "").strip().lower()
    if not cmd:
        return
    now = time.time()
    with _state_lock:

        if _mode != "stand":
            print(f"[CMD REJECTED] {cmd} -> Current mode is '{_mode}', not 'stand'. Dropped.")
            return
        
        last = _last_cmd_ts.get(cmd, 0.0)
        if now - last < CMD_COOLDOWN_S:
            return
        _last_cmd_ts[cmd] = now

    act = CMD_MAP.get(cmd)
    if not act:
        print(f"[CMD] {cmd} (no mapping, ignored)")
        return

    print(f"[CMD] {cmd} -> action:{act}")
    _run_group(act)
    # 注意：按钮动作是一次性的，不改变连续步态 _mode

def _process_line(line: bytes) -> None:
    """处理一行 TCP 文本：优先 CMD，然后 JSON 控制"""
    global _last_rx_ts
    s = line.decode("utf-8", "ignore").strip()
    if not s:
        return

    if s.startswith("CMD:"):
        _handle_cmd(s[4:])
        with _state_lock:
            _last_rx_ts = time.time()
        return

    try:
        msg = json.loads(s)
        v = float(msg.get("v", 0.0))
        steer = float(msg.get("steer", 0.0))
        _handle_vector(v, steer)
        with _state_lock:
            _last_rx_ts = time.time()
    except Exception as e:
        print("[PARSE] ignore:", s[:160], "err:", e)
# ======================================


# ============== 颜色转发（UDP→Unity TCP） ==============
def _send_to_unity_text(text: str) -> None:
    """发送一行文本到当前 Unity 客户端；若无连接则忽略"""
    data = (text.strip() + "\n").encode("utf-8")
    with _state_lock:
        conn = _current_conn
    if not conn:
        return
    try:
        conn.sendall(data)
        if PRINT_FWD:
            print("[FWD] -> Unity:", text.strip())
    except Exception as e:
        print("[FWD] send error:", e)

def _udp_color_listener() -> None:
    """监听本地 UDP 颜色信号，并转发到 Unity"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_COLOR_HOST, UDP_COLOR_PORT))
    print(f"[UDP] Color listen on {UDP_COLOR_HOST}:{UDP_COLOR_PORT}")
    sock.settimeout(1.0)
    while not _stop_event.is_set():
        try:
            data, _ = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except Exception as e:
            print("[UDP] error:", e)
            continue
        
        raw = data.decode("utf-8", "ignore").strip()
        print("[UDP] recv =", raw)   # 新增
        
        tag = (data.decode("utf-8", "ignore").strip().upper())
        if tag in ("RED", "GREEN"):
            _send_to_unity_text(f"COLOR_SIGNAL:{tag}")
# ======================================================


# ============== TCP 服务端 ==============
def _tcp_server() -> None:
    global _current_conn
    print(f"[TCP] Listening on {HOST}:{PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen(1)

        while not _stop_event.is_set():
            try:
                srv.settimeout(1.0)
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception as e:
                print("[TCP] accept error:", e)
                continue

            print("[TCP] Connected:", addr)
            with _state_lock:
                _current_conn = conn
            _set_mode("stand", force=True)

            with conn:
                conn.settimeout(1.0)
                buf = b""
                while not _stop_event.is_set():
                    try:
                        data = conn.recv(4096)
                        if not data:
                            break
                        buf += data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            _process_line(line)
                    except socket.timeout:
                        continue
                    except Exception as e:
                        print("[TCP] conn error:", e)
                        break

            print("[TCP] Disconnected:", addr)
            with _state_lock:
                _current_conn = None
            _set_mode("stand")
# =======================================


# ============== 启动/退出 ==============
def _on_sigint(signum, frame):
    print("\n[SYS] SIGINT, shutting down...")
    _stop_event.set()

def main() -> None:
    signal.signal(signal.SIGINT, _on_sigint)

    t_motion = threading.Thread(target=_motion_loop, daemon=True)
    t_motion.start()

    t_udp = threading.Thread(target=_udp_color_listener, daemon=True)
    t_udp.start()

    try:
        _tcp_server()
    finally:
        _stop_event.set()
        t_motion.join(timeout=1.0)
        t_udp.join(timeout=1.0)
        print("[SYS] bye")

if __name__ == "__main__":
    main()
