# hcirobot · TonyPi 纯视觉“搜寻—对准—接近”控制台

不依赖 Unity 的 TonyPi 人形机器人闭环基线：机器人摄像头画面 → 红球检测 →
有限状态机决策 → 兼容课程协议的 TCP 命令 → 机器人动作，并配套桌面操作台、
真机帧调参工具和可部署到机器人的扩展服务。

范围边界：这是**单目视觉伺服/目标趋近**，不含 Unity/PICO、障碍物避让、地图、
定位或路径规划；像素半径只是距离代理。

## 功能特性

- **视觉闭环**：LAB 红球检测（3 帧确认/释放）+ `SEARCHING → ALIGNING →
  APPROACHING → ARRIVED` 状态机，搜索自动换向、对准滞回、到达锁存；
  丢失目标立即停止前进，搜索超时/视频断流/异常一律进入安全态。
- **默认不武装**：CLI 需显式 `--arm`，GUI 需显式点“武装自治”；所有停止路径
  （停止、急停、断流、崩溃、关窗）都会尽力补发零向量。
- **桌面操作台**：实时画面与遥测、运行中热调检测/控制参数、手动行为按钮
  （点头/摇头/点动）、有界日志；视频用纯 Python MJPEG 解析，FFmpeg 崩溃免疫。
- **真机帧调优**：`tools/eval_detector.py` 用录制帧离线评估检测（检出率/
  连续段/中心抖动），默认阈值已按 200 张真机帧重新标定（检出 0% → 100%）。
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
RobotBackend（RecordingRobot 记录 | TcpRobotClient → TonyPi :5075）
```

## 快速开始

要求 `uv`（Python 环境全部由 uv 管理）：

```bash
uv sync --dev
```

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

## 机器人端服务怎么选

| 能力 | main 分支 `TCP_connect.py` | `robot_side/tonypi_server.py` |
|---|---|---|
| JSON 连续控制 + 0.60s 看门狗 | ✓（含滞回） | ✓ |
| GUI 点动/停止/闭环控制 | ✓ | ✓ |
| `CMD:nod` / `CMD:shake`（头部动作） | ✗ 忽略 | ✓ |
| 行走中收到 CMD | 拒绝（更安全） | 允许 |

**团队 main 分支的 `TCP_connect.py` 可直接用上**：本项目的输出值全部在其死区
`0.20` 之上，协议逐项兼容。只有点头/摇头按钮需要部署 `robot_side` 扩展版
（部署步骤见 [robot_side/README.md](robot_side/README.md)，两服务都用 5075，
注意先停旧服务）。

## 参数调优

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
  app.py           会话运行循环（安全看门狗、事件、手动注入）
  robot.py         TCP/记录后端（可取消、CMD 动作）
  video.py         MJPEG/摄像头/文件/合成视频源
  gui.py           Tk 桌面操作台
robot_side/        机器人端扩展服务（点头/摇头/stand）
tools/             真机帧检测评估工具
tests/             73 项自动化测试（含真机帧 fixture）
GUIDE.md           完整指南：部署、标定、安全流程、排障
```

## 安全须知

软件“站立/零向量”**不是机械急停**。真机测试请遵守：
架空先行 → 一人值守物理断电 → 唯一控制权（停掉 Unity/厂商跟随脚本）→
先 `recording` 后端验证视觉 → 再低速地面测试。网络端口无鉴权，仅在隔离
局域网使用。完整安全流程见 [GUIDE.md](GUIDE.md) 第 11 节。
