# robot_side：TonyPi 机器人端服务

`tonypi_server.py` 保留课程 `Example/TCP_connect.py` 的 JSONL/CMD 协议，并增加
`CMD:nod`、`CMD:shake`、`CMD:stand` 与 `DIST:<毫米>` 遥测。文件可直接复制到 TonyPi，
运行时兼容 Python 3.8，不依赖仓库其余 Python 包。

## 协议保持不变

- JSONL：`{"v":float,"steer":float,"grab":bool,"t":...}\n`；可选 `lateral`，转向优先，负 `v` 后退。
- CMD：保留 `right_grip`、`right_trigger`、`left_trigger`，新增 `nod`、`shake`、`stand`。
- DIST：连接期间按间隔发送 `DIST:<毫米>\n`；课程 SDK 的未连接哨兵 `99999` 原样发送。
- 仍只服务一个 TCP 客户端，不做认证、自动重连或控制权仲裁。

默认值仍为 TCP `0.0.0.0:5075`、颜色 UDP `127.0.0.1:6001`、死区 `0.20`、
看门狗 `0.60s`、步态节拍 `0.30s`、命令冷却 `0.50s`、距离节拍 `0.30s`。

## 横移协议扩展（暂未接入控制策略）

可选数值 `lateral` 范围为 `[-1,1]`，相对机器人身体正向：负值左移、正值右移。
省略或为零时保持旧行为；旧式全零帧也会结束横移。优先级为
`转向 > 横移 > 前后移动`，不会混合两个步态。非法横移值被忽略且不刷新看门狗。

| 幅值 | 左移 | 右移 |
| --- | --- | --- |
| ≤ 0.20 | 横移轴死区 | 横移轴死区 |
| > 0.20 且 ≤ 0.45 | `left_move_10` | `right_move_10` |
| > 0.45 且 ≤ 0.75 | `left_move` | `right_move` |
| > 0.75 | `left_move_fast` | `right_move_fast` |

这些文件名已与课程 ZIP 核对，后缀 `_10` 不是已测得的位移承诺。
可用 `TONYPI_ACTION_LATERAL_L/R`、`TONYPI_ACTION_LATERAL_L/R_SLOW` 和
`TONYPI_ACTION_LATERAL_L/R_FAST` 分别覆盖动作组。启用动作目录检查时，这六个文件也会检查。

PC 底层可构造 `RobotCommand(lateral=...)` 或通过 `send_raw` 传入该字段，镜像会原样转发。
默认 `lateral=0`，旧命令编码不会新增字段。手柄 LS 左右会生成横移请求；GUI 点动、追球与避障策略暂不生成横移请求。
使用前需更新机器人端；课程原版服务不会执行这个扩展。横移步长、周期与稳定性尚未真机标定。

当前手柄会由 LS 左右生成横移请求；RS 左右生成原地旋转，RS 上下通过
`head_down` / `head_center` / `head_up` 逐档保持俯仰位置。扩展服务同时声明
`ACTION_V1`，PC 为每个动作附加唯一 ID，并接收 `accepted` 与执行完成后的 `done`；
重复 ID 只重发状态，不会重复驱动舵机。

## 必须显式选择运行模式

推荐始终设置 `TONYPI_MODE`：

```bash
# 不访问硬件，只打印动作
TONYPI_MODE=dry-run python3 tonypi_server.py

# 真机；SDK/API 缺失或动作组目录校验失败时拒绝启动
TONYPI_MODE=hardware PYTHONPATH=/home/pi/TonyPi/HiwonderSDK python3 tonypi_server.py
```

未设置模式时安全地默认 `hardware`，因此不会因缺 SDK 自动降级。旧变量
`TONYPI_DRY_RUN=1` 仍兼容；若同时设置并与 `TONYPI_MODE` 冲突，启动失败。

硬件模式启动时要求：

- `hiwonder.Board.setPWMServoPulse` 存在；
- `hiwonder.ActionGroupControl.runActionGroup` 或 `runAction` 存在；
- 若设置 `TONYPI_ACTION_GROUP_CHECK_DIR`，目录及配置的全部步态、站立动作组 `.d6a` 文件必须存在；该变量只校验文件，必须指向已安装 Hiwonder SDK 实际使用的目录，不会改变 SDK 的查找路径；旧名 `TONYPI_ACTION_GROUP_DIR` 仍兼容；
- 若 `TONYPI_REQUIRE_SONAR=1`，Sonar 模块、构造和 `getDistance` 必须可用。

## Sonar 策略

正式部署建议：

```bash
TONYPI_MODE=hardware
TONYPI_REQUIRE_SONAR=1
TONYPI_ALLOW_SIM_WITH_HARDWARE=0
```

- `TONYPI_REQUIRE_SONAR=1`：连续失败达到 `TONYPI_SONAR_FAILURE_LIMIT`（默认 3）后锁存
  故障并安全退出；`99999`、异常、`None` 和越界值都计为失败。
- Sonar 可选时：服务继续运行，但用 `TONYPI_SONAR_WARN_INTERVAL`（默认 5s）限频告警。
- 硬件模式默认禁止 `TONYPI_SONAR_SIM`，避免真动作配假距离。只有明确设置
  `TONYPI_ALLOW_SIM_WITH_HARDWARE=1` 才允许混用，不建议生产使用。
- dry-run 若要求 Sonar，必须同时配置模拟源。

模拟格式：

| 格式 | 含义 | 示例 |
| --- | --- | --- |
| `flat:<v>` | 恒值 | `flat:500` |
| `sweep:<min>:<max>:<period_s>` | 三角波 | `sweep:100:800:2.0` |
| `steps:<v1>@<t1>,<v2>@<t2>,...` | 分段恒值，时间严格递增 | `steps:600@1.0,200@3.5` |

## 无动作预检

`--check` 只做配置、依赖/API 和 TCP/UDP 绑定检查，不启动线程、不监听、不执行动作：

```bash
TONYPI_MODE=dry-run TONYPI_SONAR_SIM=flat:500 \
  python3 tonypi_server.py --check

TONYPI_MODE=hardware TONYPI_REQUIRE_SONAR=1 \
  PYTHONPATH=/home/pi/TonyPi/HiwonderSDK \
  python3 tonypi_server.py --check
```

systemd 可将它放在 `ExecStartPre`；仓库模板见 `deploy/systemd/tonypi-server.service`。

## 主要配置

配置集中在 `ServiceConfig`，环境变量与 CLI 覆盖会在启动前做严格范围/组合校验。

| 类别 | 环境变量 |
| --- | --- |
| 模式 | `TONYPI_MODE`、兼容项 `TONYPI_DRY_RUN` |
| 网络 | `TONYPI_HOST`、`TONYPI_PORT`、`TONYPI_UDP_COLOR_HOST`、`TONYPI_UDP_COLOR_PORT` |
| 时序/命令 | `TONYPI_DIST_INTERVAL`、`TONYPI_WATCHDOG`、`TONYPI_STEP_INTERVAL`、`TONYPI_CMD_COOLDOWN`、`TONYPI_ALLOW_CMD_WHILE_MOVING`（默认 0） |
| 动作组 | `TONYPI_ACTION_GROUP_CHECK_DIR`（只校验 SDK 实际目录）、`TONYPI_ACTION_FORWARD/BACK/TURN_RIGHT/TURN_LEFT/STAND` |
| 舵机 | `TONYPI_HEAD_PITCH_ID`、`TONYPI_HEAD_YAW_ID`、各轴 min/center/max、摆幅和 `TONYPI_HEAD_STEP_MS` |
| Sonar | `TONYPI_REQUIRE_SONAR`、`TONYPI_SONAR_SIM`、`TONYPI_ALLOW_SIM_WITH_HARDWARE`、失败次数/告警间隔 |

Board API 的硬范围为舵机 ID 1/2、脉宽 `[500,2500]µs`、用时 `[20,30000]ms`；
俯仰默认 `[1000,2000]`、中立 1500、摆幅 150，偏航默认 `[500,2500]`、中立 1500、
摆幅 200。非法范围或互相矛盾的组合会在监听端口前失败。

## 生命周期和故障行为

- `SIGINT`、`SIGTERM`（Windows 测试还支持 `SIGBREAK`）触发停止。
- `stop()` 会关闭活动连接、TCP 监听和 UDP socket，以解除阻塞调用。
- 所有动作组和头部舵机操作使用同一个 actuator lock 串行执行。
- 硬件动作首次抛错即锁存故障、关闭服务，不再继续发送余下动作；退出路径最终尝试一次
  `stand`。若硬件总线已经故障，最终 stand 可能失败，因此软件停机不能替代物理断电。
- 正常断开仍回站立；看门狗超时仍回站立。

## dry-run 协议冒烟

仓库提供完全不碰硬件的可执行检查。它找空闲端口，启动显式 dry-run 子进程和模拟
DIST，验证连接、JSONL、CMD、看门狗及信号退出：

```bash
uv run python tools/smoke_tonypi_protocol.py
```

也可直接运行目标测试：

```bash
uv run pytest tests/test_robot_side.py -q
uv run ruff check --isolated --line-length 100 --target-version py38 --select E,F,W,I robot_side/tonypi_server.py
uv run python -m py_compile robot_side/tonypi_server.py
```

## 部署建议

```bash
scp robot_side/tonypi_server.py pi@<ip>:/opt/hcirobot/robot_side/
ssh pi@<ip> 'sudo install -m 0644 deploy/env/tonypi.env.example /etc/hcirobot/tonypi.env'
```

按真机路径修改 `PYTHONPATH`、`TONYPI_ACTION_GROUP_CHECK_DIR` 和监听地址，先运行 `--check`，
再先用 dry-run 冒烟，最后才切到 hardware。启动前停掉课程 `TCP_connect.py`、厂商
`Follow.py`/`KickBall.py` 和其他 5075 控制者。

## 真机安全

- 首次动作必须架空机器人，并确认动作组名称、左右方向、站立和头部舵机方向。
- 确认 Sonar 实测距离、99999/拔线故障和 required-sonar 安全退出。
- 测试 SIGTERM、TCP 断开、客户端崩溃和动作 SDK/I²C 异常后的最终姿态。
- 清空桌沿、楼梯和人员活动区，安排可立即物理断电的人员。
- `stand`、看门狗和进程退出都不是机械急停；动作组调用本身可能阻塞。
- 5075 无认证/加密，只能在隔离可信局域网使用，不要映射公网。
