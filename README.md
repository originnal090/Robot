# hcirobot · TonyPi/Unity“搜寻—对准—接近”控制台

TonyPi 人形机器人闭环基线：机器人或 Unity 虚拟摄像头画面 → 红球检测 →
有限状态机决策 → 兼容课程协议的 TCP 命令 → 实体或虚拟机器人动作，并配套
反应式避障、桌面操作台、真机帧调参和 Unity 客观试验工具。

范围边界：这是**单目视觉伺服/目标趋近 + 反应式局部避障**。Unity 在本项目中是
虚拟机器人和试验场，不是第二套控制器。当前仍不含地图、定位、SLAM、NavMesh
或 A*/全局路径规划；像素半径只是距离代理。

## 功能特性

- **视觉闭环**：LAB 红球检测（3 帧确认/释放）+ `SEARCHING → ALIGNING →
  APPROACHING → ARRIVED` 状态机，搜索自动换向、对准滞回、到达锁存；
  丢失目标立即停止前进，搜索超时/视频断流/异常一律进入安全态。
- **分步接近模式**：真机行走颠簸导致边走边识别不可靠（场地实测行走中检出率仅
  29%），提供四种接近策略——`fast_then_slow`（远距 fast 冲、近距慢）/ `normal` /
  `normal_then_slow`（默认）/ `slow_realtime`（慢速连续实时识别）。前三种按
  **走→停稳→识别** 相位循环，走段时长随球变大自动缩短（越近识别越频繁）；
  速度值映射机器人端三档步态动作组（≤0.45 one_step / ≤0.75 常速 / >0.75 fast，
  `go_forward_one_step`/`go_forward_fast`/`turn_*_small_step` 等随库附带）。
  到达阈值按真机采集帧标定（贴脸半径比封顶 ~0.13，`arrival_radius_ratio=0.10`）。
- **反应式避障**：机器人端超声波距离回传（`DIST` 遥测）为主、视觉启发式为
  降级兜底，受阻自动“停车—后退—转向—重找球”；连续受阻锁存 `BLOCKED`。
  这是反应式局部避障，不含地图与全局规划。
- **默认不武装**：CLI 需显式 `--arm`，GUI 需显式点“武装自治”；所有停止路径
  （停止、急停、断流、崩溃、关窗）都会尽力补发零向量。
- **桌面操作台**：实时画面与遥测、运行中热调检测/控制参数、手动行为按钮
  （点头/摇头/点动）、有界日志；视频用纯 Python MJPEG 解析，FFmpeg 崩溃免疫。
- **手柄遥操作**：PC 端直接读游戏手柄——Windows 下优先走原生 XInput（ctypes，
  零依赖，与 joy.cpl 同通路），pygame 为备选/跨平台后端；左摇杆经 TCP JSONL
  驾驶机器人；**B 键抢断**——自主寻路运行中按 B 立即切入手动控制，再按 B 恢复
  自主寻路；LT/RT 触发前后爬起、RB 触发蹲下灭火（课程 CMD 按钮名）；自治
  ARRIVED 后自动下发灭火动作（`[controller] arrival_action`，默认开，可置空关闭）。
  课程基线把读手柄放在 Unity 里，本仓库把这一角色移到了 Python 侧。
- **真机帧调优**：`tools/eval_detector.py` 用录制帧离线评估检测（检出率/
  连续段/中心抖动），默认阈值已按 200 张真机帧重新标定（检出 0% → 100%）。
- **样本采集**：GUI 底栏"采集样本"开关，点击后从采集线程直接保存**原始帧**
  PNG（不经控制循环、不丢帧、后台写盘不卡画面），再点停止或会话结束自动收尾；
  默认存到 `artifacts/captures/capture-<时间戳>/`，适合训模型/标阈值采数据。
- **命令镜像（Unity 孪生）**：控制真机的同时把每条命令镜像一份发给本机
  Unity 仿真（GUI 侧栏"镜像地址"填 `127.0.0.1:5075`，CLI `--robot-mirror`，
  可重复）。镜像是尽力而为的：断线记日志并按 5 s 退避重连，每会话最多重试 5 次；
  达到上限后停用镜像重连，主机器人会话继续；超声波遥测始终以主后端为准。
- **协议兼容**：输出与课程 `TCP_connect.py` 的 JSONL `{"v","steer","grab","t"}`
  和 `CMD:<name>` 完全兼容，且按其离散动作语义输出（转向优先、死区之上）。

## 系统架构

```text
视频源（synthetic / 摄像头 / 文件 / TonyPi MJPEG :8080）
        │  MjpegHttpSource（纯 Python 解析）
        ▼
RedBallDetector（LAB + 轮廓 + 强红核门控 + 帧确认）
        ▼
VisualApproachController（IDLE/SEARCHING/ALIGNING/APPROACHING/ARRIVED/LOST_SAFE）
        ▼  SessionControl（武装/停止/急停/手动注入，跨线程）
RobotBackend（RecordingRobot 记录 | TcpRobotClient → TonyPi/Unity :5075）

Unity 仿真链路：
Unity Camera :8080 --MJPEG--> Python
Python --JSONL/CMD :5075--> Unity Rigidbody 虚拟机器人
Unity --DIST:<mm> :5075--> Python 反应式避障
Python --autonomy_status UDP :6102--> Unity recorder/状态面板
```

**唯一控制权**：同一时刻只能有一个程序写入运动端口 `5075`。跑 Unity 时关闭
真机服务、课程 `RobotSyncManager` 和手动控制；跑真机时停止 Unity Play Mode。

## 快速开始

核心视觉程序要求 **64 位系统和 Python 3.11+**；开发环境推荐由 `uv` 管理：

```bash
uv sync --dev
uv run hcirobot --check-config
```

`--check-config` 只校验配置，不打开视频、TCP 或 Unity UDP。TonyPi 端的单文件扩展服务面向旧厂商镜像，兼容 Python 3.8+；两端的版本边界不同。

桌面操作台（推荐从合成演示开始）：

```bash
uv run hcirobot-gui        # 视频源 synthetic、后端 recording、未武装
```

命令行闭环演示：

```bash
uv run hcirobot --arm --source synthetic --backend recording
# 预期：SEARCHING -> ALIGNING -> APPROACHING -> ARRIVED
```

测试与检查：

```bash
uv run pytest -W error
uv run ruff check src tests tools robot_side
```

## Unity 虚拟机器人快速开始

安装 Unity 2022.3 LTS 或更新兼容版本并按 `unity/README.md` 导入本地包后，通过
Unity 菜单生成 Quick Start 场景，进入 Play Mode；Unity 不属于 Python 依赖，
`uv sync` 不会安装它。随后：

```bash
# 1. 只检查模拟器端点，不要求本机安装 Unity Editor，只要求端点已经在运行
uv run python tools/check_unity_simulator.py

# 2. 使用完整 Unity 模板运行；先不加 --arm 检查图传和零输出
uv run hcirobot --config config.unity.toml

# 3. 确认唯一控制权和安全边界后，再武装虚拟机器人
uv run hcirobot --config config.unity.toml --arm

# 4. 汇总 Unity recorder JSONL；JSON 默认写 stdout，也可同时写 CSV
uv run python tools/eval_unity_trials.py artifacts/unity-trials \
  --json-out artifacts/unity-report.json \
  --csv-out artifacts/unity-trials.csv
```

### 可视化一键演示

```bash
uv run python tools/run_unity_demo.py
```

该脚本会依次：启动 Unity 编辑器（窗口模式）打开仓库自带的 `unity/demo-project`
（完整 Quick Start 场景：地面、虚拟 TonyPi、红球、障碍、观察相机、状态 HUD、
试验记录器）并进入 Play Mode → 等待 `5075/8080` 端点就绪 → 启动
`hcirobot-gui --config config.unity.toml --demo`。Unity 已在运行时自动跳过
编辑器启动；`--unity-only` / `--gui-only` / `--no-autoarm` 可拆步执行；
`--rebuild-scene` 丢弃已存场景并按当前包代码重建（会清掉手动摆放的障碍）；
`--project` 可指向任何导入了本包的 Unity 工程。

看什么：

- **Unity Game 视图**：全局第三人称视角看机器人追球，右上角画中画是机器人
  第一视角（MJPEG 同源画面），左上角 HUD 实时显示控制状态、目标检测、实际
  输出、当前动作和障碍距离；
- **Python GUI**：同一画面上叠加绿圈球识别、状态机状态、`intent`（控制器
  意图）与 `actual`（最终下发命令及其来源）对比、`DIST` 距离。

`--demo` 的自动武装只在控制后端为本机（loopback）TCP 时生效，指向真机或
非回环地址时只自动预览、仍需手动点击“武装自治”。

### 手柄遥操作（自主 ↔ 手动抢断）

安装可选依赖后，GUI 启动即自动检测手柄（XInput 布局，Xbox 手柄/兼容接收器
均可；面板显示连接状态与当前模式）：

```bash
uv sync --extra gamepad        # 或: uv pip install "pygame>=2.5"
uv run hcirobot-gui
```

无头 CLI 同样支持：`uv run hcirobot --gamepad ...`（`[gamepad]` 日志走 stderr）。

- **左摇杆**驾驶：上下 = 前进/后退，左右 = 转向，0.20 死区，与机器人端
  `TCP_connect.py` 的死区语义一致；只在**手动模式**生效，误碰不会打断自主。
- **B 键抢断**：自主寻路（已武装）时按 B → 自治解除、立即切入手动控制；
  再按 B → 恢复自主寻路。0.25 s 防抖避免双击误切换。
- **动作键**（手动模式生效，发课程 CMD 按钮名，真机两端服务都认）：

  | 按键 | CMD | 真机动作 |
  |---|---|---|
  | LT | `left_trigger` | 前倒爬起（stand_up_front） |
  | RT | `right_trigger` | 后倒爬起（stand_up_back） |
  | RB | `right_grip` | 蹲下灭火（outfire） |

- **到达自动灭火**：自治 ARRIVED 后自动下发 `arrival_action`（默认
  `right_grip`），在 `[controller]` 配置；Unity 仿真对 CMD 无动作副作用，
  真机/robot_side 生效。
- 手动模式下松杆即停（0.3 s 脉冲续期，GUI 卡顿时自动超时停车）；拔掉手柄
  自动零输出并重新检测，插回自动恢复；避障 `BLOCKED` 锁存、急停等安全语义
  对手柄模式同样生效。
- 未安装 pygame 时程序照常运行，手柄面板显示安装提示。


首批场景矩阵：无障碍左/中/右目标；近且居中、近但偏心、远处大球和遮挡球；
正前/左右偏置单障碍与 `150/250/350 mm` 边界；持续受阻到 `BLOCKED`；视频冻结、
TCP 断开、DIST 过期；窄通道、U 型障碍和死胡同作为当前能力边界案例。

“通过”不能只看 Python 报 `ARRIVED`：还必须满足真实最终距离、朝向误差、稳定
停车、无碰撞/跌倒/出界和时间上限。Unity recorder 默认还要求稳定停车 `0.5 s`；
评估工具默认真值阈值是 `0.35 m`、`15°`、`30 s`，均可用命令行覆盖。当前控制器
到达计数存在已知语义：它不要求目标同时
居中，因此“近但偏心”可能产生 false arrival；试验工具会保留并统计该问题。

## 机器人端服务怎么选

| 能力 | main 分支 `TCP_connect.py` | `robot_side/tonypi_server.py` |
|---|---|---|
| JSON 连续控制 + 0.60s 看门狗 | ✓（含滞回） | ✓ |
| GUI 点动/停止/闭环控制 | ✓ | ✓ |
| `CMD:nod` / `CMD:shake`（头部动作） | ✗ 忽略 | ✓ |
| 超声波距离遥测（`DIST` 行，避障必需） | ✗ | ✓ |
| 行走中收到 CMD | 拒绝（更安全） | 默认拒绝，可显式调整 |
| 硬件 SDK 缺失 | 可 dry-run | hardware 模式拒绝启动；dry-run 必须显式选择 |

**团队 main 分支的 `TCP_connect.py` 可直接用上**：本项目的输出值全部在其死区
`0.20` 之上，协议逐项兼容。点头/摇头和避障需要部署 `robot_side` 扩展版
（部署步骤见 [robot_side/README.md](robot_side/README.md)，两服务都用 5075，
注意先停旧服务）。

## Orange Pi / TonyPi 部署准备

两次 2026-09-14 采集的 LAB 调参、2.35 KB 实验 SVM、训练复现和板端离线基准，
见 [端侧红球识别说明](docs/edge-recognition.md)。CLI 可用 `--edge-model PATH`
选择学习模型；默认仍走 LAB。OrangePi 实板性能尚未测量。

仓库提供了可替换路径和用户名的模板：

- `deploy/config/config.orangepi.toml`：Orange Pi 真机配置起点；
- `deploy/systemd/*.service`：TonyPi 与 Orange Pi systemd 模板；Orange Pi unit 故意不带 `--arm`；
- `deploy/env/*.example`：生产环境变量示例；
- `tools/preflight_orangepi.py`：默认无动作的依赖/配置预检；
- `deploy/offline/README.md`：AArch64 离线 wheelhouse 与 SHA-256 清单流程。

```bash
uv run python tools/preflight_orangepi.py --config deploy/config/config.orangepi.toml
# 配好相机后才加 --probe-video；机器人架空后才加 --probe-robot
```

机器人主 TCP 断线会终止当前会话，不自动重连。GUI 检测到图传停帧或冻结时会立即停车并暂停自治；收到新画面后清空旧识别状态，重新确认目标再继续。等待期间的手柄输入、解除武装、停止或急停会取消自动继续。实时视频中断后最多重试 5 次，稳定输出 5 秒后重置重试额度；镜像连接每会话最多重试 5 次。旧会话清理结束前不能重新开始或复位。

## 参数调优

- 识别器：GUI 可勾选“使用端侧模型”并选择 `.npz` 权重；关闭时使用 LAB，
  选项在下次会话启动时生效；
- 运行中：GUI“参数调优”面板（LAB 阈值、面积、圆度、确认帧数、对准/到达
  阈值），点“应用参数”立即生效；
- 离线：`uv run python tools/eval_detector.py --frames artifacts/tonypi-video
  --out artifacts/eval-output`，输出逐帧标注与 `summary.json` 指标；
- 完整标定流程见 [GUIDE.md](GUIDE.md)。

## 目录结构

```text
src/hcirobot/
  detector.py      红球检测（真机帧标定阈值）
  controller.py    搜寻/对准/接近状态机
  navigation.py    反应式避障策略与视觉启发式
  app.py           会话运行循环（安全看门狗、事件、手动注入）
  robot.py         TCP/记录后端（可取消、CMD 动作、距离遥测读取）
  video.py         MJPEG/摄像头/文件/合成视频源
  gui.py           Tk 桌面操作台
robot_side/        机器人端扩展服务（点头/摇头/stand/DIST/硬件预检）
deploy/            Orange Pi/TonyPi systemd、环境、配置和离线安装模板
tools/             预检、检测评估、Unity 端点检查和 trial 真值汇总工具
tests/             自动化测试（含真机帧 fixture 与 Unity 工具测试）
config.unity.toml  本机 Unity 虚拟机器人完整配置模板
unity/             Unity 本地包、协议和安装后步骤（由 Unity 侧实现维护）
GUIDE.md           完整指南：部署、仿真、标定、安全流程、排障
```

## 安全须知

软件“站立/零向量”**不是机械急停**。真机测试请遵守：
架空先行 → 一人值守物理断电 → 唯一控制权（停掉 Unity/厂商跟随脚本）→
先 `recording` 后端验证视觉 → 再低速地面测试。网络端口无鉴权，仅在隔离
局域网使用。完整安全流程见 [GUIDE.md](GUIDE.md) 第 12 节。
