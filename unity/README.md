# HCIRobot Unity 本地仿真包

`unity/com.hcirobot.simulator` 是面向 Unity 2022.3 LTS 及更新版本的轻量本地包。它只使用 Unity 与 .NET 自带 API，不依赖 PICO、XR、URP 或其他第三方包。

## 导入

1. 新建或打开 Unity 2022.3 LTS+ 项目。
2. 打开 `Window > Package Manager`。
3. 点击 `+ > Add package from disk...`。
4. 选择本仓库的 `unity/com.hcirobot.simulator/package.json`。
5. 等待 Unity 导入 `HciRobot.Simulator.Runtime`、`HciRobot.Simulator.Editor` 和 Editor Tests。

也可在目标项目 `Packages/manifest.json` 的 `dependencies` 中加入本地路径：

```json
"com.hcirobot.simulator": "file:D:/Users/K2106/Code/RandomProjects/HCIRobot/unity/com.hcirobot.simulator"
```

路径需按实际仓库位置调整。

## 最快开始

1. 导入包后执行 `HCIRobot > Build Quick Start Scene`。
2. 将新场景保存到目标 Unity 项目的 `Assets/Scenes/`。
3. 进入 Play Mode。
4. 服务默认监听：控制 TCP `5075`、MJPEG HTTP `8080`、自治状态 UDP `6102`。
5. PC 控制端机器人地址填运行 Unity 的主机 IP；视频 URL 使用 `http://<Unity主机>:8080/?action=stream`。
6. 运行日志默认写入 `Application.persistentDataPath/HCIRobotTrials/trial-*.jsonl`。

菜单场景会创建地面、带 Rigidbody 的虚拟 TonyPi、机器人相机、前向测距点、目标球、两个侧方障碍物、方向光和所有服务组件。默认直线路径保持畅通，先用于验证协议和追球；做避障试验时再把障碍拖入路径。目标材质采用已落入当前 `config.toml` LAB 阈值的洋红红色，而非显示器纯红；不同渲染管线、曝光或后处理仍可能需要重新标定。进入 Play Mode 前可在 Inspector 调整端口、速度、测距和图像参数。

Game 视图自带三层可视化：`Observer Camera`（第三人称跟随，全局看机器人追球）、右上角画中画的机器人第一视角相机（MJPEG 同源，捕获时临时切到 RenderTexture，不影响画面）、左上角 `AutonomyStatusHud` 状态面板。

## 组件

| 组件 | 用途 | 默认值/行为 |
|---|---|---|
| `VirtualRobotTcpServer` | TonyPi 兼容 JSONL/CMD 服务 | `0.0.0.0:5075`；单客户端；跨 read 缓冲；非法帧忽略；0.60 s 看门狗停车；网络线程仅入队，主线程应用命令；所有 `DIST` 写经同一写锁串行化 |
| `TonyPiMotionDriver` | Rigidbody 平面运动 | 死区 0.20；转向优先；负值左转、正值右转；默认离散动作，可选 continuous |
| `ForwardDistanceSensor` | 前向 ray/sphere cast | 默认 sphere cast；结果换算为整数 mm；默认每 0.30 s 发送 `DIST` |
| `MjpegCameraServer` | TonyPi 风格 MJPEG | `0.0.0.0:8080/?action=stream`；Camera/RenderTexture/ReadPixels/JPEG 全在主线程；网络线程只发送缓存 JPEG |
| `AutonomyStatusUdpReceiver` | 自治状态旁路接收 | `0.0.0.0:6102`；校验 `type/schema_version/session_id/seq`；同 session 只接受递增 seq，拒绝已退出会话迟到包；主线程 latest-only；默认 1.0 s watchdog |
| `AutonomyStatusHud` | IMGUI 状态面板 | 左上角显示控制状态、武装、目标检测、实际输出与来源、当前动作、障碍距离/避障计数、estop/fault/termination；状态超时标记 STALE；格式化逻辑在 `AutonomyStatusHudText`，可测试 |
| `ObserverCameraRig` | 第三人称跟随相机 | LateUpdate 平滑跟随机器人后上方并看向机器人前方；不随机身自转旋转，Game 视图保持世界稳定 |
| `SimulationTrialRecorder` | JSONL 试验记录 | 机器人/目标世界坐标、真实距离和朝向误差、轨迹、碰撞、最小间隙、实际输出、稳定停车时长和自治状态；按真值阈值写 `trial_summary` |

## 场景矩阵

| 场景 | TCP 5075 | MJPEG 8080 | UDP 6102 | Rigidbody | 适用目的 |
|---|---:|---:|---:|---:|---|
| Quick Start 完整场景 | 开 | 开 | 开 | 开 | PC 闭环控制、红球视觉、距离避障、轨迹与碰撞评估 |
| 控制协议冒烟 | 开 | 可关 | 可关 | 开 | 测试 JSONL/CMD、死区、左右符号、看门狗停车 |
| 视觉源测试 | 可关 | 开 | 可关 | 可选 | 用现有 Python 视频源验证 MJPEG 读取与红球检测 |
| 自治状态可视化/记录 | 可关 | 可关 | 开 | 可选 | 验证 schema/session/seq、乱序过滤、latest-only 和状态 watchdog |
| 纯离线场景编辑 | 关 | 关 | 关 | 可选 | 摆放目标和障碍，不占用端口；禁用对应组件即可 |

同一台主机同一时间只能有一个进程绑定各默认端口。若 Python mock server、真机代理或另一 Unity Player 已占用端口，请停掉它或在 Inspector 改端口。

## 测试

打开 `Window > General > Test Runner`，在 EditMode 运行 `HciRobot.Simulator.EditorTests`。测试覆盖：

- TonyPi JSON DTO 必填字段、越界和非法 JSON；
- 跨 socket read 的 JSONL/CMD 拼帧；
- `DIST:<mm>\n` 格式；
- 0.20 死区、转向优先、负左正右和 continuous 映射；
- 自治状态 `type=autonomy_status` / `schema_version=1` DTO；
- `session_id` 内严格递增 seq 与新 session 切换；
- HUD 文本格式化：状态/目标/输出/动作/障碍行、STALE、estop/fault/termination。

该包已在 Unity 2022.3.62f3c1 实际编译并全部通过 EditMode 测试（协议、运动映射、
状态过滤与 HUD 共 18 项）。

## 安全与限制

- 这是物理近似仿真，不模拟 TonyPi 动作组、舵机动力学或足式步态。
- 软件零速度不是实体机器人急停；连接真机时仍需保留人工断电手段。
- 监听 `0.0.0.0` 会暴露到所在网络，非受信网络应改为 `127.0.0.1` 或使用主机防火墙。
- MJPEG 使用同步 GPU readback，适合课程级低帧率测试，不以高性能渲染为目标。
- `ForwardDistanceSensor` 默认排除 Ignore Raycast 层，并会过滤机器人父层级 Collider；Quick Start 同时把机器人和红球设为 Ignore Raycast，红球仅作为视觉目标与真值，不作为障碍。

完整线协议见 [PROTOCOL.md](PROTOCOL.md)。
