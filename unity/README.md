# HCIRobot Unity 本地仿真包

`unity/com.hcirobot.simulator` 0.2.6 是面向 Unity 2022.3 LTS 及更新版本的轻量本地包。它只使用 Unity 与 .NET 自带 API，不依赖 PICO、XR、URP 或其他第三方包。

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

## 绑定现有消防员 Camera（不生成红球）

`Build Quick Start Scene` 会清空当前场景并生成一套完整演示物体，其中包含红球。已有消防员场景不要执行它：

1. 确认消防员运动根节点带有 `Rigidbody`，Camera 位于这个节点的子层级中。
2. 在 Hierarchy 选中消防员的 Camera，或选中包含该 Camera 的父节点。
3. 执行 `HCIRobot > Bind Selected Camera (No Target)`。
4. 保存场景并进入 Play Mode。

该命令保留当前场景，不创建红球、地面、障碍、灯光或观察相机，也不添加依赖目标球的 `SimulationTrialRecorder`。它在 Camera 最近的父级 `Rigidbody` 上添加/复用 `TonyPiMotionDriver` 和 `VirtualRobotTcpServer`，把抬头/回正/低头绑定到所选 Camera；在 Camera 上添加/复用 `MjpegCameraServer`；在 Camera 高度建立一个保持水平朝前的测距点；并添加状态 UDP、HUD 和后台运行服务。首次添加运动组件时，Camera 当前局部俯仰角会作为“回正”，上下各偏移 20 度。

如果场景中已有另一套 TCP 或 MJPEG 服务，绑定命令会提示先处理端口冲突，而不会创建第二套监听器。组件已存在时可重复执行绑定，命令会复用组件，并保留已有速度参数。

## 组件

| 组件 | 用途 | 默认值/行为 |
|---|---|---|
| `VirtualRobotTcpServer` | TonyPi 兼容 JSONL/CMD/ACTION 服务 | `0.0.0.0:5075`；声明 `ACTION_V1`；单客户端；跨 read 缓冲；非法帧忽略；0.60 s 看门狗停车；网络线程仅入队，主线程应用命令；所有回执与 `DIST` 经同一写锁串行化；Console 只在连接或有效运动方向变化时记录，不逐包刷屏 |
| `TonyPiMotionDriver` | Rigidbody 平面运动与头部预览 | 死区 0.20；转向优先；负值左转、正值右转；continuous 模式按真机三档映射前进速度，并按满舵 fast 动作标定转向；`Movement Speed Multiplier` 统一缩放前进/横移；左右转速倍率可独立调整；`head_down/center/up` 持久调整机器人相机俯仰 |
| `ForwardDistanceSensor` | 前向 ray/sphere cast | 默认 sphere cast；结果换算为整数 mm；默认每 0.30 s 发送 `DIST` |
| `MjpegCameraServer` | TonyPi 风格 MJPEG | `0.0.0.0:8080/?action=stream`；Camera/RenderTexture/ReadPixels/JPEG 全在主线程；网络线程只发送缓存 JPEG |
| `AutonomyStatusUdpReceiver` | 自治状态旁路接收 | `0.0.0.0:6102`；校验 `type/schema_version/session_id/seq`；同 session 只接受递增 seq，拒绝已退出会话迟到包；主线程 latest-only；默认 1.0 s watchdog |
| `AutonomyStatusHud` | IMGUI 状态面板 | 左上角显示控制状态、武装、目标检测、实际输出与来源、当前动作、障碍距离/避障计数、estop/fault/termination；状态超时标记 STALE；格式化逻辑在 `AutonomyStatusHudText`，可测试 |
| `ObserverCameraRig` | 第三人称跟随相机 | LateUpdate 平滑跟随机器人后上方并看向机器人前方；不随机身自转旋转，Game 视图保持世界稳定 |
| `SimulationTrialRecorder` | JSONL 试验记录 | 机器人/目标世界坐标、真实距离和朝向误差、轨迹、碰撞、最小间隙、实际输出、稳定停车时长和自治状态；按真值阈值写 `trial_summary` |

在机器人根对象的 `TonyPiMotionDriver > Scene scale > Movement Speed Multiplier`
调整场景位移比例。FactoryDay 课程场景量得消防员正前方墙距 `19.2806` Unity 单位，
对应实际约 `0.90 m`；默认火源沿消防员来向到外墙为 `32.4271` Unity 单位，对应实际
约 `1.50 m`。两组比例分别是 `21.423`、`21.618` Unity 单位/米（相差约 `0.9%`），
取中值并四舍五入后，本包默认使用 `21.5`。单步横移距离也使用同一倍率；原地转向
角速度不受该值影响。包内生成的米制 Quick Start 场景会显式覆盖为 `1.0`。

手柄右摇杆推到底时，Python 侧死区重映射输出 `steer = ±1.0`，扩展 TonyPi 服务会
选择 `turn_left_fast` / `turn_right_fast`。`TonyPiMotionDriver > Measured TonyPi
full-stick turn` 按这一实际链路标定：fast 动作基础转角 `15°`，左倍率默认 `1.0`、右
倍率默认 `0.5`，所以单个动作分别按 `15°` 和 `7.5°` 计算。课程动作文件中 fast
动作帧总时长为 `0.90 s`，服务每次动作后等待 `0.30 s`；Unity 的连续近似因此使用
`15° / (0.90 + 0.30) = 12.5°/s` 的左转满舵平均速度，右转为 `6.25°/s`。
`Left Turn Multiplier` / `Right Turn Multiplier` 可在 Inspector 独立调整。

这些实测参数只校准手柄满舵对应的 fast 档。small/normal 单步镜像仍保留原先的临时
`8° / 20°`，待真机分别测量后再替换；关闭 `Use Measured Full Stick Turn` 才会恢复
使用 `Maximum Turn Degrees Per Second` 的旧连续模型。

### 与课程控制参数的差异

完整课程 Unity 场景把 XR Origin 的满推移动速度覆盖为 `0.9` Unity 单位/秒，连续转向
速度覆盖为 `7.8°/s`。按上述 `21.5` 换算，移动速度约 `0.0419 m/s`，与真机实测
`go_forward` 的 `0.0375 m/s` 相差约 `11.6%`，基本对应 normal 档，而不是本包暂按
`2x` 推算的 fast 档 `0.075 m/s`。课程里的 `FireFighterMovementAnimator` 只把摇杆
幅值写入动画参数，不负责位移。

课程包 `RobotSyncManager` 的 `turnScale = 1` 只缩放发送前的摇杆 `steer`，并以
`sendHz = 30` 发送；没有单独的前进倍率。机器人端 `TCP_connect.py` 使用 `0.20`
死区，把输入离散为 `turn_left` / `turn_right` 动作组并每 `0.30 s` 重复一次，所以
越过死区后继续增大 `turnScale` 并不会可靠地改变单次动作角度或动作组速度。厂商
`Follow.py` 的细调方式是直接执行 `turn_left_small_step` / `turn_right_small_step`。

本包因此把满舵 fast 动作的实测转角和动作节拍换算成 continuous 平均角速度；左右
倍率表达真机不对称。STEP_V1 的 small/normal/fast 单步角度是另一组参数，不拿满舵
测量值冒充小步标定。前进速度则可使用已测三档速度与 `Movement Speed Multiplier`
调整。

## 场景矩阵

| 场景 | TCP 5075 | MJPEG 8080 | UDP 6102 | Rigidbody | 适用目的 |
|---|---:|---:|---:|---:|---|
| Quick Start 完整场景 | 开 | 开 | 开 | 开 | PC 闭环控制、红球视觉、距离避障、轨迹与碰撞评估 |
| 控制协议冒烟 | 开 | 可关 | 可关 | 开 | 测试 JSONL/CMD/ACTION、死区、左右符号、看门狗停车 |
| 视觉源测试 | 可关 | 开 | 可关 | 可选 | 用现有 Python 视频源验证 MJPEG 读取与红球检测 |
| 自治状态可视化/记录 | 可关 | 可关 | 开 | 可选 | 验证 schema/session/seq、乱序过滤、latest-only 和状态 watchdog |
| 纯离线场景编辑 | 关 | 关 | 关 | 可选 | 摆放目标和障碍，不占用端口；禁用对应组件即可 |

同一台主机同一时间只能有一个进程绑定各默认端口。若 Python mock server、真机代理或另一 Unity Player 已占用端口，请停掉它或在 Inspector 改端口。

## 测试

打开 `Window > General > Test Runner`，在 EditMode 运行 `HciRobot.Simulator.EditorTests`。测试覆盖：

- TonyPi JSON DTO 必填字段、越界和非法 JSON；
- 跨 socket read 的 JSONL/CMD 拼帧；
- `ACTION_V1` 请求、完成回执与真机回执镜像；
- `DIST:<mm>\n` 格式；
- 0.20 死区、转向优先、负左正右和 continuous 映射；
- 自治状态 `type=autonomy_status` / `schema_version=1` DTO；
- `session_id` 内严格递增 seq 与新 session 切换；
- HUD 文本格式化：状态/目标/输出/动作/障碍行、STALE、estop/fault/termination。

`VirtualRobotTcpServer > Diagnostics` 可分别关闭连接、命令变化和非法输入日志。Play Mode 下 `Client Status`、`Last Applied Command`、`Valid Commands Received`、`Invalid Lines Ignored` 会持续显示当前诊断值；连续收到同方向输入时只更新 Inspector 计数和最新值，不重复写 Console。

`TonyPiMotionDriver > Measured TonyPi Forward Tiers` 使用 2026-09-15 真机粗测：`go_forward` 16 秒约 0.60 m，对应基线 `0.0375 m/s`；`go_forward_one_step` 暂按老旧机器人偶发原地踏步后的有效速度 `0.4x` 处理；`go_forward_fast` 按 `2x` 处理。因此 continuous 输入 `|v| <= 0.45`、`0.45 < |v| <= 0.75`、`|v| > 0.75` 分别使用 `0.015`、`0.0375`、`0.075 m/s`。这些参数都可在 Inspector 修改；关闭 `Use Measured Forward Tiers` 后恢复 `v * Maximum Forward Speed` 的比例模型。

该包已在 Unity 2022.3.62f3c1 实际编译并全部通过 60 项 EditMode 测试。

## 安全与限制

- 这是物理近似仿真，不模拟 TonyPi 动作组、舵机动力学或足式步态。
- 软件零速度不是实体机器人急停；连接真机时仍需保留人工断电手段。
- 监听 `0.0.0.0` 会暴露到所在网络，非受信网络应改为 `127.0.0.1` 或使用主机防火墙。
- MJPEG 使用同步 GPU readback，适合课程级低帧率测试，不以高性能渲染为目标。
- `ForwardDistanceSensor` 默认排除 Ignore Raycast 层，并会过滤机器人父层级 Collider；Quick Start 同时把机器人和红球设为 Ignore Raycast，红球仅作为视觉目标与真值，不作为障碍。

完整线协议见 [PROTOCOL.md](PROTOCOL.md)。
