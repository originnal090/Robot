# TonyPi/Unity“搜寻—对准—接近”基线完整指南

> 本项目从课程压缩包中的 Orange Pi 红球检测、TonyPi TCP 控制和厂商 `Follow.py` / `KickBall.py` 提炼而来。当前版本包含超声波优先、视觉兜底的反应式局部避障，并可把 Unity 作为虚拟机器人和客观试验场；仍不包含地图、定位、SLAM、NavMesh 或全局路径规划。

## 1. 先说结论

课程包提供了三条可运行的基础链路：

1. Unity/PICO 人工输入通过 TCP `5075` 控制 TonyPi；
2. TonyPi 的 MJPEG 图传由 Orange Pi 读取并识别红色球形目标；
3. Orange Pi 把视频和识别结果发送给 Unity。

但课程 `Example` 没有把识别结果接回机器人运动，因此原始材料不是“感知—决策—动作—再感知”的自主闭环。本项目补上的正是这个缺口：Orange Pi 根据目标在图像中的横向位置和像素半径，控制机器人搜寻目标、对准目标、向目标接近并在近处停止。

这属于**单目视觉伺服/目标趋近 + 反应式局部避障**，不是地图式自主寻路。真机运行时像素半径只是距离代理；前向超声波只提供局部障碍距离。Unity 可提供世界真值用于试验评价，但这些真值不会回馈给当前控制器，因此不会把现有算法变成地图导航。

## 2. 课程材料要求解读

### 2.1 可以直接确认的内容

压缩包为 `感知与人机交互暑期学校课程文件.zip`。与系统集成直接相关的文件位于压缩包内：

```text
感知与人机交互暑期学校课程文件/
├── Example/
│   ├── TCP_connect.py
│   ├── TonyPi代码说明.md
│   ├── Unity依赖说明.md
│   ├── Unity/
│   │   ├── ganzhi.unitypackage
│   │   └── README.md
│   └── orangepi/
│       ├── README.md
│       ├── robot-edge-gateway/
│       └── robot-perception/
└── TonyPi智能视觉人形机器人/
    └── 6.附录/2.源码/TonyPi/TonyPi/Functions/
        ├── Follow.py
        └── KickBall.py
```

课程包顶层没有单独的任务书、评分表、提交命名规则或截止日期。因此本文将 `Example` 中能复现的工程链路视为“材料给出的基线”，不会把推断写成教师明确要求。正式提交形式和评分项仍应以课程群或教师通知为准。

### 2.2 原 `Example` 的数据流

```text
人工控制：
PICO/Unity -> TCP 5075 -> Orange Pi 透明网关 -> TonyPi TCP_connect.py -> 动作组

旁路感知：
TonyPi MJPEG 8080 -> Orange Pi 红球检测 -> UDP 6101 -> Unity

视频显示：
TonyPi MJPEG -> Orange Pi 视频代理 8080 -> Unity
```

关键事实：

- `Example/TCP_connect.py` 监听 `0.0.0.0:5075`；
- 连续控制使用一行一个 JSON：`v`、`steer`、`grab`、`t`；
- TonyPi 端死区为 `0.20`，通信看门狗为 `0.60 s`，动作节拍约 `0.30 s`；
- Orange Pi 网关只透明转发字节，配置明确禁止网关自行生成动作；
- 感知使用 LAB 阈值、轮廓面积、圆度和长宽比检测红球；
- 感知 UDP 默认发往 Unity `6101`，不会控制 TonyPi；
- 各文件中的 `.106`、`.157`、`.251`、`192.168.149.1` 并不一致，现场必须重新确认地址。

### 2.3 从厂商代码复用的思路

厂商 `Follow.py` 和 `KickBall.py` 已展示“目标偏左/偏右则转向、目标较小则前进、目标丢失则搜索”的视觉伺服思路。本项目复用这种决策结构，但没有直接运行这些脚本，原因是：

- 它们把视觉、头部舵机和全身动作耦合在同一进程；
- 导入时会启动动作线程；
- 它们依赖 TonyPi 的 `hiwonder`、相机标定和动作组；
- 若与 `TCP_connect.py` 或另一个控制进程并行，会争用动作控制权。

本项目将“检测、决策、TCP 输出”拆开，使 Orange Pi 能运行闭环，也能在 PC 上无硬件测试。

## 3. 难度评估

| 范围 | 难度 | 主要难点 |
|---|---|---|
| 本机合成视频 + 假机器人 | 中 | 状态机、检测消抖和可重复测试 |
| Orange Pi 读取 TonyPi 图传 | 中 | 网络地址、MJPEG 稳定性、OpenCV 环境 |
| TonyPi 真机闭环 | 中高 | 阈值现场标定、动作延迟、控制权、安全停机 |
| 反应式障碍物避让（已实现） | 高 | 传感器新鲜度、滞回、机动安全和场地标定 |
| 地图式自主导航 | 很高 | 需要定位、地图/建图、距离感知和路径规划 |

真正困难的部分不是画出红球轮廓，而是网络配置、现场光照、动作组阻塞、断流恢复、误检和实体机器人安全。

## 4. 本项目架构

```text
视频源
  ├── synthetic：确定性合成红球，用于验收
  ├── 摄像头编号：例如 0
  ├── 视频文件
  └── TonyPi MJPEG：http://<robot>:8080/?action=stream
          |
          v
RedBallDetector
  LAB 阈值 -> 形态学 -> 轮廓筛选 -> 3 帧确认/释放
          |
          v
VisualApproachController
  IDLE -> SEARCHING -> ALIGNING -> APPROACHING -> ARRIVED
                           \-> LOST_SAFE
          |
          v
RobotBackend
  ├── RecordingRobot：只记录动作
  └── TcpRobotClient：JSON Lines -> TonyPi/Orange Pi TCP 5075
```

代码职责：

| 路径 | 职责 |
|---|---|
| `src/hcirobot/detector.py` | 参考课程 `detector.py` 的 LAB 红球检测 |
| `src/hcirobot/controller.py` | 搜寻、对准、接近和安全状态机 |
| `src/hcirobot/robot.py` | 记录后端和 TonyPi 兼容 TCP 客户端 |
| `src/hcirobot/video.py` | MJPEG/摄像头/文件输入、合成源、调试标注 |
| `src/hcirobot/app.py` | 单一闭环运行入口和异常清理 |
| `src/hcirobot/cli.py` | 命令行参数与配置装配 |
| `src/hcirobot/mock_server.py` | 本机 TCP 假机器人 |
| `config.toml` | 默认视频、检测、控制、机器人和避障参数 |
| `config.unity.toml` | 本机 Unity MJPEG/TCP/DIST/状态 UDP 完整配置模板 |
| `tools/check_unity_simulator.py` | 不依赖 Unity 安装的 TCP、看门狗、DIST 和 MJPEG 端点诊断 |
| `tools/eval_unity_trials.py` | Unity recorder JSONL 真值指标汇总与可选 CSV 导出 |
| `unity/` | Unity 本地包、协议和 Quick Start 场景资料 |
| `tests/` | 检测、状态机、TCP、Unity 工具和端到端测试 |

### 4.1 Unity 作为虚拟机器人

```text
Unity 虚拟摄像头 :8080 ──MJPEG──> Python RedBallDetector/Controller
Python TcpRobotClient ──JSONL/CMD :5075──> Unity 虚拟机器人
Unity 前向测距 ──DIST:<mm> :5075──> Python ObstaclePolicy
Python 自治状态 ──UDP :6102──> Unity 状态接收器/试验记录器
Unity 世界真值、碰撞和轨迹 ──JSONL──> tools/eval_unity_trials.py
```

Unity 复用 TonyPi 协议语义：转向优先、`0.20` 死区、负转向为左、正转向为右、
`0.60 s` 无控制帧看门狗停车。`6101/UDP` 仍保留给课程感知协议，自治状态使用
`6102/UDP`。Unity 提供的世界坐标、真实距离和碰撞只进入 recorder，不进入控制器。

**唯一控制权是硬约束**：同一时刻只允许一个运动命令所有者连接 `5075`。Unity
Play Mode、TonyPi 服务、课程 `RobotSyncManager`、GUI 手动控制和其他自主程序之间
必须显式二选一，不能依赖“最后一条命令覆盖”来仲裁。

## 5. 状态机与动作

| 状态 | 进入条件 | 输出 |
|---|---|---|
| `IDLE` | 默认状态，未显式 `--arm` | 全停 |
| `SEARCHING` | 武装后尚无目标，或连续丢失目标 | 原地小步转向，每 2 秒换向 |
| `ALIGNING` | 目标连续 3 帧确认 | 停止前进，按横向误差转向 |
| `APPROACHING` | 连续 3 帧进入对准区 | 每帧只选择低速前进或原地小幅修正方向 |
| `ARRIVED` | 当前确认目标连续 3 帧达到像素半径阈值 | 锁存全停 |
| `LOST_SAFE` | 搜索超时、视频结束、运行异常或急停 | 锁存全停 |

横向误差归一化为：

```text
error_x = 2 * center_x / frame_width - 1
```

负值表示目标在画面左侧，正值表示在右侧。课程 `TCP_connect.py` 中 `steer < -deadzone` 对应左转，`steer > deadzone` 对应右转，因此符号直接兼容。

安全行为：

- 当前帧没有候选目标时，立即停止前进；
- 连续 3 帧失检后才回到搜索，避免单帧噪声造成状态抖动；
- 接近状态使用 `0.08` 的进入对准区和 `0.14` 的退出区，形成滞回；
- 目标像素半径达到 `0.18 * min(width, height)` 后立即全停，连续 3 帧才锁存到达；
- 连续搜索超过 12 秒进入 `LOST_SAFE`；
- 视频结束或异常时发送停止；
- 程序启动默认不武装，必须显式添加 `--arm`；
- 软件“站立/零向量”不是机械急停，不能替代人工断电。

## 6. 用 uv 安装与运行

要求：

- `uv`（开发机/联网安装）；
- Orange Pi/PC 核心视觉程序使用 Python 3.11 或更高，Orange Pi 建议 64 位 Linux；
- Windows、Linux 或 Orange Pi Linux 均可运行核心项目，但目标 AArch64 wheel 必须与实际 OS/glibc 匹配；
- TonyPi 单文件扩展服务兼容 Python 3.8+，真机端仍需课程镜像自带的 `hiwonder` 和动作组；
- Unity 仿真需另行安装 Unity 2022.3 LTS 或兼容的新版本；Unity 不是 Python/uv 依赖。

同步环境：

```bash
uv sync --dev
```

所有后续命令都通过 `uv run` 执行，不要另建手工 virtualenv，也不要裸用 `pip`。

### 6.1 桌面窗口

启动图形操作台：

```bash
uv run hcirobot-gui
```

窗口默认读取 `config.toml`，但不会自动打开视频或武装机器人。推荐按以下顺序操作：

1. `视频源` 保持 `synthetic`，`控制后端` 保持 `recording`；
2. 点击“开始预览”，等待状态显示“画面正常”；
3. 确认实时画面、目标框和遥测变化；
4. 点击“武装自治”，观察搜索、对准、接近和到达；
5. 点击“停止”结束会话；
6. “紧急停止”会锁存 `LOST_SAFE`，会话结束后需点击“复位”才能重新启动。

窗口控件语义：

| 控件 | 行为 |
|---|---|
| 开始预览 | 打开视频与后端，但保持 `IDLE`，不产生运动 |
| 武装自治 | 在画面正常且无故障时启动自主闭环 |
| 停止 | 请求零向量并正常结束当前会话；界面立即归零，`finished` 后才显示已停止 |
| 紧急停止 | 锁存急停，立即请求零向量并结束会话，同时打断卡住的 TCP 发送 |
| 复位 | 清除停止后的故障/急停锁存，不会自动重新武装 |

`视频源` 支持 `synthetic`、摄像头编号（如 `0`）、视频路径和 TonyPi MJPEG URL。`recording` 只计算和显示动作；`tcp` 会向所填地址的 `5075` 端口发送真机命令。首次测试必须使用 `recording`。

MJPEG 流使用项目自带的纯 Python 解析器（不再经过 OpenCV/FFmpeg 后端），单帧损坏会被跳过，流中断会进入安全态而不是崩溃。

#### 识别器选择

右侧勾选“使用端侧模型”并选择 `.npz` 权重后，下次会话使用轻量 SVM；关闭时使用
默认 LAB 检测。选项和路径在会话运行期间锁定，模型会在视频或机器人资源打开前校验。
训练得到的默认权重位于 `artifacts/edge-model-20260914/red_ball_svm.npz`；详细评估和
OrangePi 板端基准命令见 `docs/edge-recognition.md`。

#### 参数调优面板

右侧“参数调优”区可运行时热更新参数，初值来自 `config.toml`：

- 检测：LAB 六通道上下限、最小面积、圆度、长宽比上下限、确认/释放帧数；
- 控制：对准进入/退出误差、到达半径比例。

点击“应用参数”立即对当前会话生效（非法值只写日志不崩溃）；“恢复默认”重新读取 `config.toml`。调参时建议先用 `recording` 后端，观察遥测区的目标确认与误差变化。

#### 接近模式（走走停停）

避障面板新增"接近模式"下拉框（会话启动时读入，运行中切换需重开会话），CLI 对应
`--approach-mode`：

| 模式 | 行为 |
|---|---|
| `fast_then_slow` | 远处用 fast 档冲刺，靠近后降慢速 |
| `normal` | 全程常速 |
| `normal_then_slow`（默认） | 常速接近，临近减速 |
| `slow_realtime` | 慢速连续，实时识别（无走走停停） |

前三种按 **走(walk)→停稳(settle)→识别(sense)** 循环：walk 段重复上次识别出的
意图（时长 `walk_seconds_far`→`walk_seconds_near` 随球变大线性缩短）、settle 段
停车丢帧 `settle_seconds`、sense 段逐帧检测 `sense_seconds`。手柄/点动绕过节拍，
超声波避障随时打断 walk。机器人端 `tonypi_server.py` 按速度幅值三档选动作组
（阈值 `tier_slow_max=0.45`、`tier_fast_min=0.75`，动作组名可配，
`--check` 会校验存在性）。

#### 手动测试面板

“手动测试”区用于连通性与动作验证：

| 按钮 | 行为 |
|---|---|
| 点头 / 摇头 | 发送 `CMD:nod` / `CMD:shake`，需要机器人端运行 `robot_side/tonypi_server.py`；课程原版 `TCP_connect.py` 会忽略 |
| 低头 / 回正 / 抬头 | 发送保持姿态的 `head_down` / `head_center` / `head_up`；扩展服务用动作 ID 回传 accepted/done，Unity 0.2.0 保持相机俯仰档位 |
| 前爬起 / 后爬起 | 发送 `CMD:left_trigger` / `CMD:right_trigger`（课程按钮名，映射 stand_up_front/back 动作组，真机两端服务都支持） |
| 蹲下灭火 | 发送 `CMD:right_grip`（映射 outfire 动作组） |
| 前进 / 后退 / 左转 / 右转 | 发送 0.35 秒点动脉冲（复用课程 JSON 协议，`v` 可为负实现后退） |
| 停止点动 | 立即发送零向量脉冲 |

手动按钮仅在会话运行中、未武装、无故障时可用；武装自治时禁用，避免手动与自主打架。软件停止不能替代物理断电。

#### 样本采集（原始帧）

底栏"采集样本"按钮：会话运行中点击即开始把**原始相机帧**（未叠加标注）逐帧存为
PNG，按钮实时显示已存张数；再点一次停止。数据落在
`artifacts/captures/capture-<时间戳>/`（相对 GUI 启动目录）。采集直接挂在视频
采集线程上，检测/控制慢于相机时不丢帧；写盘在后台线程，磁盘慢只会计入"丢弃"
计数不会卡画面。会话结束或关窗会自动停止采集并记录最终数量。武装自治时也可以
采集——追球过程中的距离/视角变化正是有价值的样本。采完可直接用
`tools/eval_detector.py --frames <目录>` 评估当前检测参数。

#### 手柄遥操作（B 键抢断）

“手动测试”区下方的手柄块显示连接状态、当前模式（自主寻路/手动控制）和实时
摇杆读数。Windows 下优先使用原生 XInput 读取（零依赖），pygame（`uv sync
--extra gamepad`）为备选与其他系统的后端；接上 XInput 布局手柄（Xbox 手柄或
兼容接收器）后：

- **B 键**：自主寻路运行中按下 → 抢断，自治解除并切入手动控制；再按一次 →
  恢复自主寻路。0.25 s 防抖，急停/故障锁存未复位时拒绝恢复并提示。
- **左摇杆（LS）**（仅手动模式）：上下前进/后退、左右横移，0.20 死区，输出走
  与点动按钮相同的 `request_manual` 通道（来源标记 `gamepad`），松杆 0.3 s
  内自动停车。
- **右摇杆（RS）**（仅手动模式）：左右让机身原地旋转，不改变头部；上下不映射动作。
- **十字键（D-Pad）**（仅手动模式）：上/下按低/中/高三档逐级调整相机俯仰。
  每次按下只走一档，松开后才能触发下一档。
- **动作键**（仅手动模式，发课程 CMD 按钮名）：LT = 前倒爬起
  （`left_trigger`→stand_up_front）、RT = 后倒爬起（`right_trigger`→
  stand_up_back）、RB = 蹲下灭火（`right_grip`→outfire）；自主模式下按下会被
  忽略并提示先抢断。扳机是模拟量，超过阈值才算按下。
- **到达自动动作**：自治 ARRIVED 后自动下发 `[controller] arrival_action`
  （默认 `right_grip` 蹲下灭火，置空关闭）；动作下发失败只记警告，不影响
  到达结果。
- 摇杆输入在自主模式下被忽略（每 4 s 提示一次“按 B 抢断”），误碰不会打断
  自主；拔线自动零输出并重新探测，插回自动重连；监视线程任何异常都不会
  影响控制回路。
- 无 pygame 或无手柄时面板显示原因，其余功能不受影响。CLI 侧等价开关为
  `--gamepad`。手柄输入可用 `uv run python tools/gamepad_probe.py` 独立诊断
  （`--backend xinput|pygame` 可指定通路；`raw` 全 0 而 joy.cpl 有输入说明
  该通路读不到设备）。

课程基线（zip 内 `Example`）由 Unity 读手柄/PICO 输入再经 TCP 下发；本仓库将
读手柄移到 Python 客户端，协议不变，Unity 仿真与真机后端都适用。

Linux/Orange Pi 若提示缺少 Tkinter，需要使用系统包管理器安装 Tk，例如 Debian 系统执行 `sudo apt install python3-tk`。Tkinter 属于系统 Python 组件，不由 PyPI/`uv` 安装。

### 6.2 真机帧检测调优

`tools/eval_detector.py` 用录制的真机帧离线评估检测质量：

```bash
uv run python tools/eval_detector.py --frames artifacts/tonypi-video --out artifacts/eval-output
# 可选覆盖：--lab-min "30,132,100" --lab-max "220,215,150" --min-area 50
#           --circularity 0.60 --aspect-min 0.6 --aspect-max 1.67 --confirm 3 --release 3
```

输出逐帧标注 PNG 和 `summary.json`（检出率、连续命中段分布、中心抖动）。调参流程：

1. 先跑一遍当前参数得到基线指标；
2. 在真机帧上找不稳定的帧（标注图里候选框闪烁或缺失）；
3. 调整参数后重跑对比，检出率和连续段越长越好、中心抖动越小越好；
4. 把确认的参数写回 `config.toml`，并保证 `uv run pytest` 仍然通过。

当前默认阈值已按 200 张真机帧重新标定：`lab_min=[30,132,100]`、`lab_max=[220,215,150]`、圆度 `0.60`，并新增强红核门控（候选轮廓内至少 15% 像素 A 通道 ≥160）防止放宽后误检背景。课程原始阈值 `[55,145,118]-[190,195,150]` 在真机帧上检出率为 0，仅适合当时的光照与目标。

### 6.3 安全的无武装 CLI 检查

```bash
uv run hcirobot --source synthetic --backend recording
```

因为没有 `--arm`，状态保持 `IDLE`，所有命令均为零。

### 6.4 无硬件完整闭环 CLI 演示

```bash
uv run hcirobot --arm --source synthetic --backend recording
```

预期状态序列：

```text
SEARCHING -> ALIGNING -> APPROACHING -> ARRIVED
```

保存逐帧标注图：

```bash
uv run hcirobot --arm --source synthetic --backend recording --output-dir artifacts/demo
```

标注包含状态、原因、速度、转向、检测确认状态和候选圆框。CLI 不创建窗口；桌面版由 Tkinter 显示同样的标注画面。

### 6.5 运行测试和检查

```bash
uv run pytest
uv run ruff check src tests tools robot_side
uv run python -m compileall -q src tests tools robot_side
```

测试覆盖：

- LAB 测试颜色落在课程阈值内；
- 连续 3 帧确认和释放；
- 小轮廓拒绝；
- 搜索换向和超时；
- 对准、接近减速和到达锁存；
- 当前帧失检立即停止前进；
- 对准滞回；
- TCP JSON 格式；
- TCP 分包、粘包和关闭时最终停止；
- 合成图像到动作输出的完整闭环；
- GUI 动态武装、停止和急停会话；
- 窗口按钮状态、日志上限和 Tk 创建/销毁；
- 视频结束进入安全态；
- Unity 端点检查器的 TCP JSONL/CMD/零向量/看门狗/DIST/MJPEG 行为；
- Unity recorder JSONL 的真值计算、false arrival、目录输入、CSV 和错误校验。

### 6.6 Unity 安装后联调与试验

1. 按 `unity/README.md` 把仓库中的 Unity 本地包导入 Unity 2022.3 LTS 或兼容版本；
2. 用包提供的 Quick Start Scene Builder 生成地面、虚拟机器人、摄像头、红球、障碍、网络桥和 recorder；
3. 确认真机 `5075` 服务、课程 `RobotSyncManager` 和其他控制器均已停止，再进入 Play Mode；
4. 先运行端点检查：

```bash
uv run python tools/check_unity_simulator.py
```

检查器只使用 Python 标准库，不调用 Unity CLI，也不要求 Unity 安装在运行检查器的
机器上；它只要求 `127.0.0.1:5075` 和 MJPEG URL 可达。它会发送合法 JSONL、
`CMD:stand` 和零向量，等待新鲜 `DIST`，静默等待 `0.60 s` 看门狗，并读取一个带
完整 SOI/EOI 和尺寸段的 JPEG。如果 Unity 额外回报 watchdog/state 行会自动验证；
没有可观察状态时，连接和 DIST 持续存活只能证明看门狗观察窗口已过去，仍需在
Scene/Game 视图确认虚拟机器人已经停车。退出码：`0` 全部端点通过，`1` 检查失败，
`2` 参数非法。

5. 使用仿真模板启动 Python。第一次不武装：

```bash
uv run hcirobot --config config.unity.toml
```

确认 MJPEG 连续、TCP 已连、DIST 新鲜、Unity 收到 `6102/UDP` 状态且虚拟机器人不动；
之后再运行：

```bash
uv run hcirobot --config config.unity.toml --arm
```

`config.unity.toml` 是 `config.toml` 的完整副本，只把视频改到本机 `8080`、机器人
后端改成 `tcp 127.0.0.1:5075`，保持避障启用，并增加 `[unity]` 状态 UDP
`127.0.0.1:6102`。CLI 原生支持 `--config`，模板作为 Unity 联调的单一参数基准；
也可用 `--unity-status/--no-unity-status`、`--unity-host`、`--unity-port` 显式覆盖状态通道。

#### 可视化一键演示（直观看效果）

最直接的闭环观察方式：

```bash
uv run python tools/run_unity_demo.py
```

脚本按顺序做三件事：启动 Unity 编辑器（窗口模式）打开仓库自带的
`unity/demo-project`（完整 Quick Start 场景），经包内
`HciRobot.Simulator.Editor.DemoLauncher.LaunchDemo` 进入 Play Mode；轮询
`5075/8080` 直到仿真端点就绪（默认超时 180 s）；最后启动 `hcirobot-gui
--config config.unity.toml --demo`。Unity 端点已在线时跳过编辑器启动；
`--unity-only`、`--gui-only`、`--no-autoarm` 支持拆步。`--unity` 可覆盖
Unity.exe 路径，`--project` 可指向任何导入了本包的工程（含
`artifacts/unity-validation-project`）；
`--rebuild-scene` 丢弃已存场景按当前包代码重建，升级包后场景缺新组件时用它
（注意会清掉手动摆放的障碍位置）。

Unity Game 视图包含三层可视化：全局第三人称 `Observer Camera`（跟随虚拟机器人，
直接看到追球和避障全过程）；右上角画中画为机器人第一视角相机（与 MJPEG 同源）；
左上角 `AutonomyStatusHud` 订阅 `6102/UDP`，实时显示控制状态、武装、目标
检测（confirmed/fresh/中心/半径）、实际输出 v/steer 与来源、当前动作模式、
障碍距离和避障计数，状态超时会标记 STALE。

Python GUI 侧：`hcirobot-gui` 现在接受 `--config` 与 `--demo`。视频叠加层新增
`intent v/steer`（控制器意图）与 `actual v/steer [来源]`（最终下发命令，含
`autonomy/manual/obstacle_hold/obstacle_maneuver/unarmed_hold` 等来源）两行，
有距离时追加 `dist=<mm> obstacle=<state>`；遥测条新增“来源”字段。`--demo`
自动开始预览并在画面正常后自动武装——自动武装仅当控制后端为本机（loopback）
TCP 时生效，指向真机或非回环地址时保持只预览。

安全提醒：演示脚本和 `--demo` 都不绕过唯一控制权约束；运行前同样要确认没有
其他程序占用 `5075/8080/6102`。

6. Unity recorder 产生 JSONL 后评估：

```bash
uv run python tools/eval_unity_trials.py artifacts/unity-trials \
  --json-out artifacts/unity-report.json \
  --csv-out artifacts/unity-trials.csv
```

工具接受一个 JSONL 文件或递归扫描目录，按 `trial_id` 分组。它优先使用 recorder
直接给出的真值指标，也能从 robot/target 世界坐标和 yaw 推导最终距离、朝向误差、
轨迹长度与直线效率。输出包含成功率、碰撞率、真实最终距离/朝向、耗时、轨迹长度、
直线效率、最小 clearance、avoid count 和 false arrival。缺失关键真值、NaN、非法
布尔值、冲突场景或坏 JSON 默认立即报错；`--allow-invalid` 可跳过并把诊断写入报告。

默认通过阈值为真实最终距离 `<=0.35 m`、朝向误差 `<=15°`、耗时 `<=30 s`、无碰撞，
且控制器已到达；Unity recorder 还要求稳定停车至少 `0.5 s`。可用
`--max-final-distance-m`、`--max-heading-error-deg`、
`--max-duration-s` 覆盖。直线效率是“初始直线距离/实际轨迹长度”，不是 SPL；没有
可行路径基准时不应把它称为 SPL。

### 6.7 Unity 场景矩阵与通过定义

| 场景组 | 最少案例 | 主要判定 |
|---|---|---|
| 无障碍 | 目标初始左/中/右 | 符号、搜索、对准、接近、真实停车 |
| 到达边界 | 近且居中、近但偏左/右、远处大球、遮挡球 | false arrival 与视觉代理局限 |
| 单障碍 | 正前方、左偏、右偏；覆盖 150/250/350mm | 停车、后退、转向、恢复 |
| 持续受阻 | 多次避障后仍受阻 | `BLOCKED` 锁存和停止 |
| 故障 | 视频冻结、TCP 断开、DIST 过期 | 看门狗/安全态和零输出 |
| 能力边界 | 窄通道、U 型障碍、死胡同 | 记录失败，不强行标记为当前能力通过 |

单次 trial 的“通过”必须同时满足：控制状态到达、真实距离阈值、真实朝向阈值、稳定
停车、无碰撞、未跌倒/出界、时间上限。仅在 Python 日志看到 `ARRIVED` 不算通过。
当前控制器存在已知到达偏心语义：到达计数基于目标半径，不要求该帧同时保持居中；
所以“近但偏心”可能进入 `ARRIVED`。本轮不偷偷改变控制算法，使用 false arrival 指标
把它客观暴露，待 Unity 真值试验证据充分后再单独修复。

## 7. 配置说明

默认配置在 `config.toml`。

### 7.1 视频

```toml
[video]
source = "synthetic"
width = 640
height = 480
fps = 10.0
frame_timeout_seconds = 0.75
```

`source` 可在命令行覆盖：

```bash
# 本机摄像头 0
uv run hcirobot --source 0 --backend recording

# 视频文件
uv run hcirobot --source ./sample.mp4 --backend recording

# TonyPi MJPEG
uv run hcirobot --source "http://192.168.149.1:8080/?action=stream" --backend recording
```

先不加 `--arm` 验证视频能稳定读取，再进行动作测试。

### 7.2 检测参数

当前默认值已经根据 200 张真机帧重新标定，不再使用课程旧阈值：

```toml
[detection]
lab_min = [30, 132, 100]
lab_max = [220, 215, 150]
processing_width = 640
processing_height = 480
gaussian_blur_kernel = 3
morphology_kernel = 3
minimum_contour_area = 50.0
minimum_circularity = 0.60
core_a_min = 160
minimum_core_fraction = 0.15
minimum_aspect_ratio = 0.60
maximum_aspect_ratio = 1.67
confirmation_frames = 3
release_frames = 3
```

目标选择得分为 `轮廓面积 * 圆度`，从合格候选中选择得分最高者。检测结果中的 `center_x`、`center_y` 和 `radius` 都是原始图像像素，不是机器人坐标或世界坐标。

### 7.3 控制参数

```toml
[controller]
align_enter_error = 0.08
align_exit_error = 0.14
arrival_radius_ratio = 0.18
slow_radius_ratio = 0.10
search_steer = 0.35
search_reverse_seconds = 2.0
max_search_seconds = 12.0
far_speed = 0.25
near_speed = 0.21
minimum_active_steer = 0.25
```

注意：课程 `TCP_connect.py` 最终把连续数值离散为动作组，并且转向优先于前进，不支持弧线组合控制。只有 `v` 或 `steer` 严格超过 TonyPi 死区 `0.20` 才会运动。因此本控制器每帧只输出一种可执行模式：偏差超出对准区时原地转向，进入对准区后才发送纯前进；本配置也把最小有效前进和转向输出设在死区之上。数值从 `0.21` 变到 `0.25` 并不代表原动作组会真正变速；若需要近距离微步，应准备较小步长动作组或修改 TonyPi 端映射。

### 7.4 TCP 后端

```toml
[robot]
backend = "recording"
host = "127.0.0.1"
port = 5075
connect_timeout_seconds = 3.0
send_interval_seconds = 0.10
```

发送格式与课程 `TCP_connect.py` 兼容：

```json
{"v":0.25,"steer":0.0,"grab":false,"t":"2026-09-06T12:34:56+00:00"}
```

`t` 保持为 ISO 8601 时间字符串，与 Unity 示例一致。当前 TonyPi 示例并不使用该时间戳做防重放。

## 8. 本机 TCP 联调

终端 A：

```bash
uv run python -m hcirobot.mock_server
```

终端 B：

```bash
uv run hcirobot \
  --arm \
  --source synthetic \
  --backend tcp \
  --robot-host 127.0.0.1 \
  --robot-port 5075
```

假服务支持 JSONL 的 TCP 分包/粘包，并记录合法控制帧。它只用于协议测试，不模拟 TonyPi 的真实动力学、动作时长或跌倒。

## 9. 接入 Orange Pi 与 TonyPi

### 9.1 推荐部署形态

最短链路是直接在 Orange Pi 运行本项目：

```text
Orange Pi 本项目
  ├── 读取 TonyPi :8080 MJPEG
  └── 连接 TonyPi :5075 TCP
```

如果必须保留课程透明网关，可让本项目连接 Orange Pi 网关的 `5075`，网关再转发到 TonyPi。无论哪种方式，**只允许一个运动命令所有者**。

### 9.2 TonyPi 端

三种可选服务，按需选择：

| 能力 | ① main 分支 `TCP_connect.py` | ② 课程 zip 原版 | ③ `robot_side/tonypi_server.py` |
|---|---|---|---|
| JSON 连续控制 + 0.60s 看门狗 | ✓（含滞回） | ✓ | ✓ |
| `CMD:nod` / `CMD:shake` / `CMD:stand` | ✗ 忽略 | ✗ 忽略 | ✓ 头部舵机序列 |
| 行走中收到 CMD | 拒绝 | 允许 | 默认拒绝，可配置 |
| 无 SDK 时 dry-run | ✓ 自动 | ✗ | 必须显式 `TONYPI_MODE=dry-run` |
| 端口可配置 | ✗ | ✗ | ✓ 环境变量 |
| 超声波 `DIST` | ✗ | ✗ | ✓ |

**结论：main 分支版可以直接用于基础闭环、点动和停止**——本项目输出值
`0.35/0.25/0.21` 都在其死区 `0.20` 之上。需要 GUI 点头/摇头或超声波避障时，
必须使用 ③ 扩展版。① 和 ③ 都监听 `5075`，二选一，先停旧服务。

部署 ③ 前先做无动作预检；hardware 模式中 SDK/API、必需动作组或 required Sonar 缺失都会拒绝启动，不会静默变成 dry-run：

```bash
scp robot_side/tonypi_server.py pi@<TONYPI_IP>:~/
ssh pi@<TONYPI_IP> 'PYTHONPATH=/home/pi/TonyPi/HiwonderSDK \
  TONYPI_MODE=hardware TONYPI_REQUIRE_SONAR=1 python3 ~/tonypi_server.py --check'
ssh pi@<TONYPI_IP> 'pkill -f TCP_connect.py; PYTHONPATH=/home/pi/TonyPi/HiwonderSDK \
  TONYPI_MODE=hardware TONYPI_REQUIRE_SONAR=1 python3 ~/tonypi_server.py'
```

长期运行建议使用 `deploy/systemd/tonypi-server.service` 和 `deploy/env/tonypi.env.example`，并按实际镜像替换用户、目录和 SDK 路径。

无论哪种服务：

1. 确认动作组 `go_forward`、`back`、`turn_left`、`turn_right` 和 `stand` 存在；
2. 从 Orange Pi 验证 `5075` 端口可达；
3. 不要同时启动厂商 `Follow.py`、`KickBall.py` 或另一个控制程序。

课程示例把 `right_grip` 映射到 `outfire`，但课程归档动作组中没有确认到 `outfire.d6a`。本基线不发送该离散命令，因此不受此缺口影响。

### 9.3 Orange Pi 端

将项目复制到 64 位 Orange Pi 后，以 `deploy/config/config.orangepi.toml` 为起点替换 `TONYPI_IP`。先校验配置和依赖；默认预检不打开相机，也不连接机器人：

```bash
uv sync --no-dev
uv run hcirobot --config deploy/config/config.orangepi.toml --check-config
uv run python tools/preflight_orangepi.py --config deploy/config/config.orangepi.toml
# 配好相机后：追加 --probe-video
# 机器人架空后：才允许追加 --probe-robot（连接可能触发 stand）
uv run hcirobot --config deploy/config/config.orangepi.toml
```

离线安装和 AArch64 wheelhouse 流程见 `deploy/offline/README.md`。目标 Orange Pi 型号/OS 未固定前，不能用 Windows/x86-64 上下载的 wheel 代替目标验证。

第一次保持**不加 `--arm`**，确认：

- MJPEG 可读；
- 红球能连续识别；
- 日志保持 `IDLE`；
- TonyPi 没有动作。

在架空机器人、现场人员就位并确认物理断电手段后，再加：

```bash
--arm
```

若经过课程网关，则 `--robot-host` 写 Orange Pi 网关地址，而不是 TonyPi 地址。

## 10. 障碍检测与反应式避障

### 10.1 这是什么、不是什么

本项目实现的是**反应式局部避障**：接近目标途中检测前方障碍，自动“停车—后退—转向—重新找球”。它没有地图、没有里程计、没有全局路径规划；单目前向超声波有侧面和后方盲区。文档不使用“寻路/导航规划”描述这套能力。

### 10.2 架构与数据流

```text
TonyPi robot_side/tonypi_server.py
  └─ Sonar.getDistance() 每 0.3s → TCP 回传 "DIST:<毫米>\n"

Orange Pi / PC
  ├─ TcpRobotClient 读取线程 → latest_distance()（毫米, 时间戳）
  ├─ VisionObstacleHeuristic（实验性）：下半帧网格占用比
  └─ ObstaclePolicy 融合决策 → 覆盖自主命令
        UNKNOWN → CLEAR → CAUTION(停车) → BACKUP → TURN → COOLDOWN → …
        连续避障仍受阻 → BLOCKED 锁存 → LOST_SAFE
```

融合语义：超声波数据新鲜（1.0s 内、20–5000mm）时**独裁决策**；数据过期或缺失时才降级用视觉启发式（输出标注 `degraded`）；视觉**永远不会推翻**超声波的“畅通”判断。传感器都无数据时策略放行自主（不因缺传感器而卡死），状态显示 `UNKNOWN`。

### 10.3 部署要求

- 避障**必须使用 `robot_side/tonypi_server.py`**：团队 main 分支版 `TCP_connect.py` 不回传距离（`DIST` 行是本扩展新增的）。
- PC 上无硬件联调：`TONYPI_MODE=dry-run TONYPI_SONAR_SIM=sweep:100:800:2.0 python3 tonypi_server.py`，模拟源支持 `flat:` / `sweep:` / `steps:` 三种确定性模式。
- 正式硬件建议 `TONYPI_MODE=hardware TONYPI_REQUIRE_SONAR=1`；hardware 模式默认拒绝与 `TONYPI_SONAR_SIM` 混用，避免实体机器人拿模拟“畅通”距离运动。
- 99999 是“传感器未连接”哨兵值，策略会按无效数据处理；如果 GUI 一直显示 `UNKNOWN`，先检查超声波模块的 I²C 接线。

### 10.4 参数标定

```toml
[obstacle]
stop_mm = 250.0    # 进入注意：停车压制前进
avoid_mm = 150.0   # 深度受阻：立即触发避障机动
clear_mm = 350.0   # 滞回：必须重新看到这么远才恢复
```

标定方法：机器人放在平地，把障碍物摆在正前方，用 GUI 面板读数对照卷尺实测距离，然后把 `stop_mm` 设为“希望开始停车的真实距离”。注意超声波装在机身前部，读数是传感器到障碍的距离，不是脚尖到障碍的距离；留出动作组一步的余量（约 10–15cm）。`max_avoids=3` 防止无限绕障：连续 3 次避障后仍受阻就锁存 `BLOCKED`，需要人工复位。

### 10.5 使用方式

- GUI：勾选“启用避障”（**仅在会话启动时读取**，运行中不可更改）；面板显示距离、视觉受阻、策略状态、避障次数。未武装时避障只执行“停车”保护，倒退/转向机动必须在武装后才允许执行，事件中会标注 `suppressed`。
- CLI：tcp 后端默认启用；`--no-obstacle` 关闭。recording 后端没有遥测，避障自动不可用。

### 10.6 局限

- 视觉启发式是**实验性兜底**：双证据（边缘密度 + 相对地面色偏）在测试场景可靠，但新场地必须先用 `tests/fixtures` 思路自测，误报时直接关闭 `vision_enabled`。
- 转向机动按固定时长执行，传感器不能打断；真机首测务必架空确认转向方向与幅度。
- 避障只响应正前方：侧面和后方障碍不在防护范围。

## 11. 现场标定

### 10.1 LAB 阈值

不同光照和相机白平衡会显著改变 LAB 值。建议：

1. 在实际场地、实际灯光下获取图像；
2. 用课程包的 LAB 阈值工具选取红球区域；
3. 保证球体亮面和暗面都被覆盖；
4. 检查红色衣物、消防设施或背景是否误入；
5. 调整 `minimum_contour_area`、圆度和长宽比；
6. 先用 `recording` 后端保存标注图，不要边调色边驱动真机。

### 10.2 对准区

- 如果机器人左右频繁抖动，增大 `align_enter_error` 和 `align_exit_error`；
- `align_exit_error` 必须大于 `align_enter_error`；
- 若转向过冲，降低 `align_steer_gain` 或使用更小的转向动作组；
- 若方向完全相反，先确认视频是否镜像，再确认 TonyPi 左右动作组映射。

### 10.3 到达阈值

`arrival_radius_ratio` 依赖球的真实尺寸、镜头、分辨率和相机安装高度。标定步骤：

1. 把机器人固定在安全位置；
2. 将球放在希望停止的距离；
3. 用 `recording` 后端读取日志/标注帧；
4. 计算 `radius / min(frame_width, frame_height)`；
5. 将该值设为 `arrival_radius_ratio`；
6. 在更远和更近的位置各复测一次；
7. 真机首次测试使用偏保守、较远的停止阈值。

单目像素半径不是可靠测距。球尺寸变化、遮挡或相机焦距变化都会使阈值失效。

## 12. 真机安全流程

1. **静态检查**：确认电池、舵机、动作组、网络和相机；清理线缆和易碎物。
2. **唯一控制权**：停止 Unity 手动控制、厂商跟随脚本和其他 `5075` 客户端。
3. **无武装图传**：使用 `recording` 后端和无 `--arm` 模式验证检测。
4. **架空测试**：机器人置于稳定支架，只验证站立和左右单步。
5. **低速地面测试**：一人观察软件，一人手持物理断电；目标从画面中心开始。
6. **故障测试**：拔掉视频网络，确认 GUI 约 `0.35 s` 后停车并显示 `VIDEO_HOLD`；图传恢复后应从目标重新确认开始继续。等待期间操作手柄或解除武装会取消自动继续。关闭机器人 TCP 时，确认 TonyPi 的 `0.60 s` 看门狗回到 `stand`，会话终止且不会自动重连。
7. **扩大范围**：确认停止距离和转向方向后，再测试完整搜索。

不要把 `stand` 当作机械急停。动作组调用可能阻塞，软件停止响应受当前动作帧时长影响。1.8 kg 左右的人形机器人跌倒仍可能伤人或损坏物品。

网络端口没有认证和加密：

- TCP `5075`；
- MJPEG HTTP `8080`；
- 课程 Unity 感知 UDP `6101`。

只在隔离可信局域网使用，并用防火墙限制来源地址，不要映射到公网。

## 13. 故障排查

### `cannot open video source`

- 检查 URL 是否为 `http://<IP>:8080/?action=stream`；
- 在 Orange Pi 上先用浏览器或 `curl` 检查端口；
- 确认不是把 Orange Pi 视频代理地址和 TonyPi 原始图传地址混淆；
- 检查 TonyPi、Orange Pi 是否同网段并互相可达。

### 一直 `SEARCHING`

- 保存标注帧检查是否有候选框；
- 重做 LAB 阈值；
- 降低最小面积但不要低到把噪点当目标；
- 检查红球是否因高光导致轮廓破碎；
- 确认连续 3 帧都能检测，而不是偶发命中。

### 检测到了但不前进

- 目标必须先连续进入对准区；
- 检查 `radius_ratio` 是否已达到到达阈值；
- TonyPi 端死区为 `0.20`，低于它的 `v` 会被解释为站立；
- 检查是否同时有另一个控制进程争用动作组。

### 左右转反了

- 检查图像是否经过镜像；
- `center_x` 左侧应产生负 `steer`，课程服务将其映射为 `turn_left`；
- 在架空状态单独发送左右命令验证动作组名称与实际方向。

### TCP 连接失败

- 确认 TonyPi `TCP_connect.py` 已启动并监听 `5075`；
- GUI 日志会显示实际尝试的 `host:port`；确认“控制后端”为 `tcp`，地址栏只填主机名或 IP，端口单独填在端口栏；
- “镜像地址”应留空或指向另一个服务。填写后 GUI 会先连接主机器人，镜像在首次发送命令时按需连接；
- 不要照抄课程中的某一个固定 IP；
- 如果使用 Orange Pi 网关，检查白名单是否允许本机 IP；
- 网关最多服务一个活动客户端，先关闭 Unity 或旧控制器。

### 视频中断后机器人没有立刻静止
- 本程序在读到视频结束或异常时发送零向量；
- 若底层 OpenCV/网络调用本身长时间阻塞，应用层无法在阻塞期间调度；
- TonyPi 端必须保留独立的 `0.60 s` 控制看门狗；
- 实测动作组最大不可中断时间，并据此设置物理安全边界。

### 避障面板一直显示 UNKNOWN
- 距离遥测只有 `robot_side/tonypi_server.py` 提供，main 分支版 `TCP_connect.py` 不回传 `DIST`；
- 超声波未接线时 Sonar 返回 99999，策略按无效数据处理；检查 I²C 连接后重启服务；
- 读取线程停止后 `latest_distance` 不再更新，看时间戳是否停滞。

### 障碍物已经移开但机器人不动
- 策略滞回要求重新看到 `clear_mm`（350mm）以外才恢复，紧贴的障碍会让它保持停车；
- `BLOCKED` 是锁存状态，连续 `max_avoids` 次避障仍受阻后需要人工复位；
- 视觉启发式处于降级判定时，确认 `vision_enabled` 与视野内是否有类似障碍的色块。

## 14. 验收清单

### 无硬件验收

- [ ] `uv sync --dev` 成功；
- [ ] `uv run pytest` 全部通过；
- [ ] `uv run ruff check src tests` 通过；
- [ ] 不加 `--arm` 时只输出零命令；
- [ ] 合成演示依次进入搜索、对准、接近、到达；
- [ ] 标注图能看到目标框、状态和输出命令；
- [ ] 视频提前结束时最终状态为 `LOST_SAFE`；
- [ ] TCP 假服务收到最后一条零向量。

### 真机验收

- [ ] 机器人架空时左右方向正确；
- [ ] 当前帧失检后不继续前进；
- [ ] 连续丢失后进入搜索；
- [ ] 搜索超时后停止；
- [ ] 到达阈值触发后保持停止；
- [ ] 控制进程崩溃或断网后 TonyPi 看门狗站立；
- [ ] 避障：`TONYPI_SONAR_SIM` 联调后，障碍物逼近时先停后绕，移开后恢复；
- [ ] 避障方向在架空状态确认不会朝障碍转向；
- [ ] 全程只有一个动作命令所有者；
- [ ] 有可触达的人工物理断电方式。

测试记录建议：

```text
日期/场地：
TonyPi IP：
Orange Pi IP：
视频 URL：
Git 版本：
LAB 阈值：
停止半径比例：
对准进入/退出阈值：
断流停止实测：
到达距离实测：
误检场景：
异常与处理：
测试人员：
```

## 15. 后续扩展

### 障碍物约束（已实现反应式部分）

超声波 + 视觉兜底的反应式避障已落地（第 10 节）。真正的局部绕障升级方向：转向步数计数作粗略航向记忆，原地扫描多个方向建局部方向直方图（VFH 简化版），属实验性增强。

只有前向超声波时，最多适合做“停—退—转—再观察”的反应式避障，仍不应称为地图寻路。

### Unity/PICO

Unity 虚拟机器人、`6102/UDP` 版本化 `autonomy_status`、试验记录器和联调工具已
加入（见第 4.1、6.6、6.7 节）。后续仍可扩展 PICO 显示和人工模式仲裁，但必须保持：

- 视频使用 MJPEG `8080`；课程感知显示可继续使用 UDP `6101` schema 1；
- 人工和自主控制显式互斥，不能同时写 TCP `5075`；
- `autonomy_status` 保留 session/seq、目标新鲜度、实际输出、故障和急停；
- 不要用 `COLOR_SIGNAL:RED` 表达完整自主状态。

### 真正路径规划

要实现“判定目标位置和障碍物寻路”，至少还需：

- 可用的距离/深度传感器；
- 相机标定与机器人坐标转换；
- 机器人位姿或定位来源；
- 环境地图或在线建图；
- 全局规划和局部避障；
- 可跟踪的速度/步态控制接口。

这些都不在当前课程 `Example` 的闭环实现里，也不应由单目红球像素坐标假装替代。
