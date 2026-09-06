# robot_side：TonyPi 机器人端扩展服务

`tonypi_server.py` 是课程机器人端服务 `Example/TCP_connect.py` 的**超集**：协议完全兼容，
并在其基础上新增了头部动作（点头/摇头）与 `stand` 停步命令。部署它即可替换课程原服务，
让 PC 端 HCI 项目的 `send_action("nod" / "shake" / "stand")` 真正生效。

## 与课程 TCP_connect.py 的差异

完全保留的原有行为：

- JSONL 控制帧 `{"v":float,"steer":float,"grab":bool,"t":...}\n`，死区 0.20 → 离散步态，
  **转向优先**；`v<0` 为后退。
- 0.60s 看门狗：超时未收到任何数据自动回站立（`stand` 动作组）。
- 0.30s 连续步态节拍（`go_forward` / `back` / `turn_right` / `turn_left` 循环执行）。
- 按钮命令原映射：`CMD:right_grip→outfire`、`CMD:right_trigger→stand_up_back`、
  `CMD:left_trigger→stand_up_front`，同名命令 0.50s 冷却防抖。
- 新连接/断开时的站立处理（接入强制站立、断开非强制回站立）。
- 本地 UDP(127.0.0.1:6001) 颜色信号 `RED`/`GREEN` → 转发 `COLOR_SIGNAL:<TAG>` 给客户端。

新增/改变的部分：

| 项目 | 课程原版 | 本服务 |
| --- | --- | --- |
| `CMD:nod` | 未映射，忽略 | 头部**俯仰**舵机（1 号）小幅摆动序列：+150µs → −150µs → 回 1500µs |
| `CMD:shake` | 未映射，忽略 | 头部**偏航**舵机（2 号）小幅摆动序列：±200µs → 回 1500µs |
| `CMD:stand` | 未映射，忽略 | 强制切回站立模式（停下连续步态）并执行一次 `stand` 动作组 |
| 未知 CMD | 打印后忽略 | 打印后忽略，并累计到 `session.unknown_cmds` 计数 |
| 无 SDK / 无硬件 | 打印 `[WARN]` 后动作组 dry-run | try-import hiwonder；导入失败或 `TONYPI_DRY_RUN=1` 时全部只打印，不动任何舵机 |
| 端口/地址 | 硬编码 | 可用环境变量 `TONYPI_HOST` / `TONYPI_PORT` / `TONYPI_UDP_COLOR_PORT` 覆盖（默认不变：0.0.0.0:5075、6001） |

注意：nod/shake 是**一次性头部动作**，与课程按钮一致，不改变连续步态模式
（正在行走时收到 `nod`，机器人边走边点头）；要让机器人停下请发 `CMD:stand`。

## 与仓库 main 分支 `TCP_connect.py` 的关系

团队仓库 `main` 分支只含一个 `TCP_connect.py`，是课程版的整理增强版（滞回
`ENTER_DZ=0.20`/`EXIT_DZ=0.30`、**运动中拒绝所有 CMD**、无 SDK 时内置 `[DRY]` dry-run）。
经逐项核对，PC 端本项目与它**完全兼容**：JSONL 控制帧、0.60s 看门狗、0.30s 节拍、
死区 0.20（本项目的 `search_steer=0.35`、`minimum_active_steer=0.25`、`near_speed=0.21`
均在其上）、CMD 行也会刷新看门狗，直接运行即可配合 GUI/CLI 使用。

| 能力 | main 分支版 | 本服务（robot_side） |
| --- | --- | --- |
| JSON 连续控制 + 看门狗 + 步态 | ✓ | ✓（无滞回，纯 0.20 死区） |
| 三个原 CMD 映射 | ✓ | ✓ |
| `CMD:nod` / `CMD:shake` / `CMD:stand` | ✗（no mapping，忽略） | ✓ 头部舵机序列 |
| 行走中收到 CMD | 拒绝（更安全） | 允许（边走边点头） |
| 无 SDK 时 dry-run | ✓ 自动 `[DRY]` | ✓ 需 `TONYPI_DRY_RUN=1` 或导入失败 |
| 端口等可配置 | 硬编码 | 环境变量可覆盖 |

结论：**只做闭环控制/点动/停止验证时，直接用 main 分支版即可**；要用 GUI 的
点头/摇头按钮，需部署本服务（二者都用 5075，注意先停旧服务）。本服务基于课程
zip 版而非 main 版；如希望合并两者的能力（滞回 + CMD 拒绝 + 头部动作），可把
main 版的 `_handle_vector` 滞回与 `_handle_cmd` 拒绝逻辑移植进 `tonypi_server.py`，
头部序列代码可直接复用。

## 头部舵机 API（已对照课程 SDK 核实）

来自 `HiwonderSDK/hiwonder/Board.py`：

```python
Board.setPWMServoPulse(servo_id, pulse=1500, use_time=1000)
# servo_id 仅接受 1、2；pulse 钳位到 [500, 2500] µs；use_time 钳位到 [20, 30000] ms
```

来自 `Functions/ColorTrack.py` 的实际用法：

- 舵机 **1 = 头部俯仰**（`y_dis`，中立 1500，ColorTrack 限幅 [1000, 2000]）
- 舵机 **2 = 头部偏航**（`x_dis`，ColorTrack 限幅 [500, 2500]）

本服务据此限制：俯仰摆动 ±150µs（1500±150，落在 [1000, 2000] 内），
偏航摆动 ±200µs，每步 250ms，序列结束回到中立位 1500。

## 部署到 TonyPi

```bash
# 1. 上传（PC 端执行；<ip> 换成机器人地址）
scp robot_side/tonypi_server.py pi@<ip>:/home/pi/TonyPi/

# 2. 停掉课程原服务（端口冲突：两者都用 5075）
ssh pi@<ip> 'pkill -f TCP_connect.py'

# 3. 无荷测试（强烈建议先跑一次，只打印不动舵机）
ssh pi@<ip> 'TONYPI_DRY_RUN=1 PYTHONPATH=/home/pi/TonyPi/HiwonderSDK \
  nohup python3 /home/pi/TonyPi/tonypi_server.py > /tmp/tonypi_server.log 2>&1 &'

# 4. 确认日志无异常后，去掉 TONYPI_DRY_RUN 正式运行
ssh pi@<ip> 'PYTHONPATH=/home/pi/TonyPi/HiwonderSDK \
  nohup python3 /home/pi/TonyPi/tonypi_server.py > /tmp/tonypi_server.log 2>&1 &'
```

说明：

- `PYTHONPATH` 指向包含 `hiwonder` 包的目录（课程镜像通常为
  `/home/pi/TonyPi/HiwonderSDK`；以机器人上 `hiwonder/` 实际父目录为准）。
  不设置且导入失败时服务仍会以 dry-run 启动，日志出现 `[WARN] hiwonder SDK not available`。
- PC 端无需改动：`TcpRobotClient.send_action("nod"/"shake"/"stand")` 发送
  `CMD:<name>\n`，与本服务的新增映射对接。
- 回滚：`pkill -f tonypi_server.py` 后重新启动课程的 `TCP_connect.py` 即可。

## 安全注意事项

- **先 dry-run**：第一次部署务必用 `TONYPI_DRY_RUN=1` 验证协议与日志，再上真舵机。
- 机器人会**真实迈步行走**：放在地面或防跌落平台上运行，清空周边障碍，远离桌沿与楼梯。
- 紧急停止：直接断开机器人电源是最可靠的急停；断开 TCP 后看门狗会在 0.6s 内
  自动回站立，但这不能替代物理急停。
- 头部舵机动作幅度虽小（±9° 左右），但头板上装有摄像头，手指勿伸入头部转动范围。
- nod/shake 期间步态不会自动停止，测试头部动作时建议机器人处于站立状态（先发 `stand`）。
- 不要在充电或手持机器人时运行动作组；舵机堵转发热很快，异常响动立即断电。
- 服务只接受单连接（与课程一致）；换客户端前先断开旧连接。
