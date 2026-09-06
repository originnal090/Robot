# TonyPi 纯视觉“搜寻—对准—接近”基线完整指南

> 本项目从课程压缩包中的 Orange Pi 红球检测、TonyPi TCP 控制和厂商 `Follow.py` / `KickBall.py` 提炼而来。当前版本不包含 Unity/PICO、障碍物检测、地图、定位、SLAM 或全局路径规划。

## 1. 先说结论

课程包提供了三条可运行的基础链路：

1. Unity/PICO 人工输入通过 TCP `5075` 控制 TonyPi；
2. TonyPi 的 MJPEG 图传由 Orange Pi 读取并识别红色球形目标；
3. Orange Pi 把视频和识别结果发送给 Unity。

但课程 `Example` 没有把识别结果接回机器人运动，因此原始材料不是“感知—决策—动作—再感知”的自主闭环。本项目补上的正是这个缺口：Orange Pi 根据目标在图像中的横向位置和像素半径，控制机器人搜寻目标、对准目标、向目标接近并在近处停止。

这属于**单目视觉伺服/目标趋近**，不是自主寻路。像素半径只是距离代理，无法替代真实深度；系统也不知道障碍物、地图和自身世界坐标。

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
| 加入障碍物避让 | 高 | 需要额外传感器输入和局部避障策略 |
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
| `config.toml` | 视频、检测、控制和机器人参数 |
| `tests/` | 检测、状态机、TCP 和端到端测试 |

## 5. 状态机与动作

| 状态 | 进入条件 | 输出 |
|---|---|---|
| `IDLE` | 默认状态，未显式 `--arm` | 全停 |
| `SEARCHING` | 武装后尚无目标，或连续丢失目标 | 原地小步转向，每 2 秒换向 |
| `ALIGNING` | 目标连续 3 帧确认 | 停止前进，按横向误差转向 |
| `APPROACHING` | 连续 3 帧进入对准区 | 每帧只选择低速前进或原地小幅修正方向 |
| `ARRIVED` | 居中目标连续 3 帧达到像素半径阈值 | 锁存全停 |
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

- `uv`；
- Python 3.11 或更高；
- Windows、Linux 或 Orange Pi Linux 均可运行核心项目；
- TonyPi 真机端仍需课程镜像自带的 `hiwonder` 和动作组。

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

#### 参数调优面板

右侧“参数调优”区可运行时热更新参数，初值来自 `config.toml`：

- 检测：LAB 六通道上下限、最小面积、圆度、长宽比上下限、确认/释放帧数；
- 控制：对准进入/退出误差、到达半径比例。

点击“应用参数”立即对当前会话生效（非法值只写日志不崩溃）；“恢复默认”重新读取 `config.toml`。调参时建议先用 `recording` 后端，观察遥测区的目标确认与误差变化。

#### 手动测试面板

“手动测试”区用于连通性与动作验证：

| 按钮 | 行为 |
|---|---|
| 点头 / 摇头 | 发送 `CMD:nod` / `CMD:shake`，需要机器人端运行 `robot_side/tonypi_server.py`；课程原版 `TCP_connect.py` 会忽略 |
| 前进 / 后退 / 左转 / 右转 | 发送 0.35 秒点动脉冲（复用课程 JSON 协议，`v` 可为负实现后退） |
| 停止点动 | 立即发送零向量脉冲 |

手动按钮仅在会话运行中、未武装、无故障时可用；武装自治时禁用，避免手动与自主打架。软件停止不能替代物理断电。

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
uv run ruff check src tests
uv run python -m compileall -q src tests
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
- 视频结束进入安全态。

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

默认值直接参考课程 Orange Pi 配置：

```toml
[detection]
lab_min = [55, 145, 118]
lab_max = [190, 195, 150]
processing_width = 640
processing_height = 480
minimum_contour_area = 50.0
minimum_circularity = 0.65
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

两种可选服务：

**课程原版** `Example/TCP_connect.py`：只支持 JSON 运动、三个固定 CMD 映射和看门狗，收到 `CMD:nod`/`CMD:shake` 会忽略。

**本项目扩展版** `robot_side/tonypi_server.py`（推荐）：在课程行为基础上新增：

- `CMD:nod` / `CMD:shake`：头部 PWM 舵机小幅摆动（1 号俯仰 / 2 号偏航，脉宽限制与课程 SDK 一致），用于连通性验证；
- `CMD:stand`：强制回到站立模式；
- `TONYPI_DRY_RUN=1` 或缺少 `hiwonder` 时只打印动作不驱动舵机；
- 其余协议（死区 `0.20`、看门狗 `0.60 s`、动作节拍、原 CMD 映射、UDP 颜色转发）与课程版保持一致。

部署到 TonyPi：

```bash
scp robot_side/tonypi_server.py pi@<TONYPI_IP>:~/
ssh pi@<TONYPI_IP> 'python3 ~/tonypi_server.py'
```

细节与差异清单见 `robot_side/README.md`。

无论哪种服务：

1. 确认动作组 `go_forward`、`back`、`turn_left`、`turn_right` 和 `stand` 存在；
2. 从 Orange Pi 验证 `5075` 端口可达；
3. 不要同时启动厂商 `Follow.py`、`KickBall.py` 或另一个控制程序。

课程示例把 `right_grip` 映射到 `outfire`，但课程归档动作组中没有确认到 `outfire.d6a`。本基线不发送该离散命令，因此不受此缺口影响。

### 9.3 Orange Pi 端

将项目复制到 Orange Pi，安装 `uv` 后：

```bash
uv sync --no-dev
uv run hcirobot \
  --source "http://<TONYPI_IP>:8080/?action=stream" \
  --backend tcp \
  --robot-host <TONYPI_IP> \
  --robot-port 5075
```

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

## 10. 现场标定

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

## 11. 真机安全流程

1. **静态检查**：确认电池、舵机、动作组、网络和相机；清理线缆和易碎物。
2. **唯一控制权**：停止 Unity 手动控制、厂商跟随脚本和其他 `5075` 客户端。
3. **无武装图传**：使用 `recording` 后端和无 `--arm` 模式验证检测。
4. **架空测试**：机器人置于稳定支架，只验证站立和左右单步。
5. **低速地面测试**：一人观察软件，一人手持物理断电；目标从画面中心开始。
6. **故障测试**：拔掉视频网络、停止 Orange Pi 进程、关闭 TCP，确认 TonyPi 的 `0.60 s` 看门狗回到 `stand`。
7. **扩大范围**：确认停止距离和转向方向后，再测试完整搜索。

不要把 `stand` 当作机械急停。动作组调用可能阻塞，软件停止响应受当前动作帧时长影响。1.8 kg 左右的人形机器人跌倒仍可能伤人或损坏物品。

网络端口没有认证和加密：

- TCP `5075`；
- MJPEG HTTP `8080`；
- 课程 Unity 感知 UDP `6101`。

只在隔离可信局域网使用，并用防火墙限制来源地址，不要映射到公网。

## 12. 故障排查

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
- 不要照抄课程中的某一个固定 IP；
- 如果使用 Orange Pi 网关，检查白名单是否允许本机 IP；
- 网关最多服务一个活动客户端，先关闭 Unity 或旧控制器。

### 视频中断后机器人没有立刻静止

- 本程序在读到视频结束或异常时发送零向量；
- 若底层 OpenCV/网络调用本身长时间阻塞，应用层无法在阻塞期间调度；
- TonyPi 端必须保留独立的 `0.60 s` 控制看门狗；
- 实测动作组最大不可中断时间，并据此设置物理安全边界。

## 13. 验收清单

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

## 14. 后续扩展

### 障碍物约束

后续可以在控制器输出与 TCP 后端之间增加 `SafetyConstraint`：输入前向超声波或深度信息，危险时把前进命令覆盖为停止。它必须是独立安全层，而不是把一个距离判断散落到检测代码里。

只有前向超声波时，最多适合做“停—退—转—再观察”的反应式避障，仍不应称为地图寻路。

### Unity/PICO

保留课程现有边界：

- 视频继续使用 MJPEG `8080`；
- 感知显示可继续使用 UDP `6101` schema 1；
- 人工和自主控制必须增加显式模式仲裁，不能同时写 TCP `5075`；
- 自主状态应新增版本化 `autonomy_status`，至少包含模式、状态、目标新鲜度、输出命令、故障和急停；
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
