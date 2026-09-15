# HCIRobot Unity 仿真协议

本文描述 `com.hcirobot.simulator` 默认暴露的三条网络接口。除 HTTP 头之外，文本编码均为 UTF-8。

## 1. 机器人控制 TCP 5075

### 连接模型

- 默认监听 `0.0.0.0:5075`。
- 只保留一个当前客户端；新客户端接入时关闭旧客户端并先停车。
- 输入为以换行 `\n` 结束的帧；支持半包、粘包和跨多次 `Read` 的帧。
- 空行、无效 UTF-8 后无法解析的行、非法 JSON、缺少字段、越界数值和非法 CMD 均静默忽略。
- 单个未完成帧/缓冲默认上限 64 KiB，超限时丢弃当前缓冲。
- 合法控制帧只在网络线程入队；`TonyPiMotionDriver.ApplyCommand` 在 Unity `Update` 主线程调用。
- 收到最后一个合法控制帧后默认 0.60 s 没有新合法帧即入队停车；断开和组件禁用也停车。

### JSONL 控制帧

与仓库 PC 客户端兼容：

```json
{"v":0.25,"steer":-0.35,"grab":false,"t":"2026-09-12T12:00:00Z"}
```

随后必须发送 `\n`。

| 字段 | 类型 | 范围 | 说明 |
|---|---|---:|---|
| `v` | number | `[-1,1]` | 正前进，负后退 |
| `steer` | number | `[-1,1]` | 负左转，正右转 |
| `lateral` | number | `[-1,1]` | 可选，默认 0；相对机身负左移、正右移 |
| `grab` | boolean | - | 兼容保留；仿真运动驱动不实现抓取机构 |
| `t` | string | - | 可选兼容字段；服务器不按客户端时间排序 |

默认离散兼容模式：

1. `abs(steer) > 0.20` 时转向优先；正为 `TurnRight`，负为 `TurnLeft`。
2. 否则 `abs(lateral) > 0.20` 时左右横移；否则检查 `abs(v) > 0.20` 前进或后退。
3. 三轴都不越死区则停车。
4. 阈值使用严格大于，所以恰好 `0.20` 仍在死区内。

在 `VirtualRobotTcpServer` 和 `TonyPiMotionDriver` 同时选择 continuous 后，两轴分别应用死区并保留 `[-1,1]` 连续幅值；可同时线速度和角速度运动。

横移扩展在两种模式下均按 `转向 > 横移 > 前后移动` 互斥选择；continuous 中横移轴
越过死区时，若转向轴也激活则只转向，否则只横移。没有横移请求时保留原有连续双轴行为。
横移沿 `transform.right`，不改变朝向；停止帧、断开与看门狗停车同时清零横移。
`maximumLateralSpeed` 独立于前进速度，默认 `0.2` 仅为仿真占位值，尚未真机标定；
continuous 保留幅值、离散模式使用满幅，不模拟实体机器人的三档动作时长与步长。
轨迹记录新增 `actual_lateral`（归一化输出），世界速度仍记录在 `velocity`。
自治策略可通过 `near_lateral_enabled` 选择近距离横移；默认关闭。

### 真机单步镜像（显示同步，不执行真机 STEP）

PC 发起真机 STEP 时，同步向 Unity 镜像连接发送开始事件；等待期间同步心跳，
收到真机最终状态后同步结束事件。Unity 不等待完成回执才开始运动，也不产生真机 ACK。

```text
MIRROR_STEP:{"id":"step_1","phase":"start","steer":-0.35,"lateral":0}
MIRROR_STEP:{"id":"step_1","phase":"heartbeat"}
MIRROR_STEP:{"id":"step_1","phase":"done"}
```

- `phase` 支持 `start`、`heartbeat`、`done`、`cancelled`、`error`。
- ID 为 1–64 位 ASCII 字母、数字、下划线或短横线，直接沿用真机 STEP 的 ID。
- 开始事件恰有一个轴越过死区，另一个为零；保留原始幅值用于选择小步/普通/快速档。
- 动作事件进入原有主线程队列；心跳只维持当前 ID 的网络看门狗，不重启动作。
  其他 ID 的心跳不会延长当前动作的连接期限。
- 开始时立即启动**刚体的平滑转向/横移**，用 smoothstep 曲线把一次有限位移分布到
  多个 `FixedUpdate`。这是目前模拟器的动作表现，不是舵机或骨骼级动画。
- Inspector 的 `Single-step mirror preview` 区域提供每档步幅与播放时长。默认角度
  8° / 20° / 30°、横移 0.02 / 0.05 / 0.08 m 均是未标定的显示占位值。
  小步左右转时长 1.35 / 1.45 s、小步左右横移 1 / 1.1 s 来自课程包动作文件，
  实际安装的动作文件或自定义档位可能不同，需要按真机修正。
- 早到的最终状态使模拟运动从当前位置停止，不跳到估计终点；迟到的回执不会增加步幅。
  动画走完预算后原地等待 ACK。步间保留真机停稳、识别的真实停顿。
- 同 ID 开始事件不重播，旧 ID 的回执不影响新动作；先收到终态再收到开始也不会启动。
  组件保存最多 2048 个见过的 ID，满后拒绝新动作而不淘汰历史；重新启动场景可清空历史。
- STOP、手动指令、断线、组件禁用和 0.60 s 网络看门狗都能停止镜像动作。
  另有默认 10 s 的绝对等待上限，即使持续收到心跳也不能无限等待或移动。
- 轨迹记录额外包含 `mirrored_step_id` 和 `mirrored_step_phase`（`playing`、`waiting_ack`
  或最终状态），用于与 PC/真机事件对照。

PC 镜像发送采用有界后台队列，从首次单步镜像起后续 STOP/手动指令也共用同一队列，
保持顺序。超过 0.5 s 的待发消息丢弃，连接恢复后不重放过期动作；队列过载退化为 STOP。
镜像故障不参与真机动作完成判断，也不阻塞真机心跳；断线期间漏掉的步不会补演。

此扩展仅用于真机旁的 Unity 镜像。Unity 仍不声明 `STEP_V1`，独立仿真继续使用
`config.unity.toml` 的 `single_step_turns = false`。旧 Unity 会忽略 `MIRROR_STEP:` 前缀，
不会把它误解释成无限持续转向。精确复现真机姿态仍需要额外的舵机/IMU 遥测。

### CMD 帧

```text
CMD:nod\n
CMD:shake\n
CMD:stand\n
CMD:head_down\n
CMD:head_center\n
CMD:head_up\n
```

动作名仅允许 1-32 个 ASCII 字母、数字或下划线。`TonyPiMotionDriver` 发布 `ActionReceived` 事件并记录 `LastAction`；`stand` 另外立即停车。三个 `head_*` 动作把机器人相机保持在低头、回正、抬头三档；转身和横移不会改变相机相对机身的俯仰档位。`nod`、`shake` 和课程按钮动作不模拟具体舵机动画，场景脚本可订阅事件自行表现。

### 单动作 ID 与完成回执

Unity 包 0.2.0 声明 `CAPS:ACTION_V1`，也能作为主机器人后端接收带 ID 的动作：

```text
ACTION:{"id":"action_1","name":"head_down"}
ACTION_STATUS:{"id":"action_1","name":"head_down","status":"accepted","detail":""}
ACTION_STATUS:{"id":"action_1","name":"head_down","status":"done","detail":""}
```

ID 为 1–64 位 ASCII 字母、数字、下划线或短横线。每条新动作先回 `accepted`，主线程应用后回 `done`；同一连接内重发相同 ID 只返回已有状态，不重复入队。会话最多记住 1024 个 ID，重连后清空。Unity 的完成表示命令已在主线程应用，不代表模拟了实体舵机的完整耗时。

PC 同时控制真机并镜像 Unity 时，仍先用兼容 `CMD` 立即显示动作，随后把真机回执原样旁路给 Unity：

```text
MIRROR_ACTION:{"id":"action_1","name":"head_down","status":"accepted"}
MIRROR_ACTION:{"id":"action_1","name":"head_down","status":"done"}
```

`status` 支持 `accepted`、`done`、`ignored`、`error`。这条旁路只更新 `LastActionReceiptId/Status`，不会再次执行动作，因此晚到或重复回执不会造成头部重复转动。

### 距离遥测

服务器到当前控制客户端：

```text
DIST:317\n
```

- 数值为非负整数毫米。
- `ForwardDistanceSensor` 默认每 0.30 s 采样一次 ray/sphere cast。
- 未命中默认发送 `5000`，可在 Inspector 修改。
- 所有服务器出站行共用一个连接级写锁，防止未来增加其他遥测线程后与 `DIST` 字节交叉。

## 2. MJPEG HTTP 8080

URL：

```text
http://<Unity主机>:8080/?action=stream
```

返回：

```text
HTTP/1.1 200 OK
Content-Type: multipart/x-mixed-replace; boundary=hcirobot-frame
```

每一帧：

```text
--hcirobot-frame
Content-Type: image/jpeg
Content-Length: <bytes>

<JPEG bytes>
```

实现线程约束：

- Unity 主线程执行 `Camera.Render`、`RenderTexture`、`Texture2D.ReadPixels` 与 `EncodeToJPG`。
- 网络客户端线程仅等待和发送最近缓存的 JPEG 字节，不调用 Unity API。
- 慢客户端会跳过中间帧，读取最新缓存帧，不建立无限帧队列。
- 默认 640x480、10 FPS、JPEG quality 75。

根路径 `/` 也作为流别名；其他路径返回 404。

## 3. 自治状态 UDP 6102

每个 UDP datagram 放一个 JSON 对象，默认硬上限 4096 字节。接收器与仓库 Python `AutonomyStatusProjector` 的 schema v1 对齐：

```json
{
  "type":"autonomy_status",
  "schema_version":1,
  "source":"hcirobot",
  "session_id":"12345678-1234-5678-1234-567812345678",
  "seq":42,
  "sent_at_ms":1700000000123,
  "mode":"autonomy",
  "armed":true,
  "control_state":"APPROACHING",
  "video_status":"streaming",
  "frame_count":120,
  "target":{"candidate":true,"confirmed":true,"fresh":true,"age_ms":0,"center_x":320.0,"center_y":240.0,"radius":48.0,"frame_width":640,"frame_height":480},
  "output":{"v":0.25,"steer":0.0,"grab":false,"source":"autonomy"},
  "obstacle":{"state":"CLEAR","distance_mm":740.0,"avoid_count":1},
  "estop":false,
  "fault":null,
  "termination":null,
  "event":{"kind":"frame","message":"approaching"}
}
```

### 必填验证

- `type` 必须精确等于 `autonomy_status`；
- `schema_version` 必须为 `1`；
- `session_id` 必须为非空字符串；
- `seq` 必须为正整数；
- `sent_at_ms` 必须非负；
- `control_state` 必须为非空字符串；
- `output` 必须存在，且 `output.v` / `output.steer` 必须为有限值；
- 为兼容 Unity `JsonUtility`，不存在的目标数值、`target.age_ms` 和距离使用 `-1` 哨兵，不使用 JSON `null`。

### 顺序与 latest-only

- 同一 `session_id` 只接受 `seq` 严格递增的消息；重复或倒序消息忽略。
- `session_id` 改变时接受新会话首条消息，即使其 `seq` 从 1 重新开始；最近 32 个已切出的旧会话会被记为 retired，迟到包不能重新占用当前状态。
- UDP 线程只保存一个 `pendingLatest` 槽；Unity 主线程每帧最多发布最新一条，旧的尚未消费消息会被覆盖。
- 默认超过 1.0 s 没有主线程接收到新有效状态会把 `IsFresh` 置 false，并触发一次 `StatusTimedOut`；下次有效消息会恢复并重新武装 watchdog。

## 4. 试验 JSONL

`SimulationTrialRecorder` 每行一个 JSON 对象，记录类型包括：

- `trial_start`：trial id、UTC、场景、Unity 版本；
- `trajectory`：时间、机器人/目标世界位置、真实距离、真实朝向误差、四元数、刚体线/角速度、实际输出、运动模式、前向间隙和最新自治状态；
- `collision`：碰撞对象、接触点、法线、相对速度；
- `autonomy_status`：接收到的状态、原因、动作、session/seq、输出和距离；
- `event`：如自治状态 watchdog 超时；
- `trial_summary`：按真实终点距离、朝向、稳定停车、碰撞和时限计算的 success，以及持续时间、路径长度、最小间隙、避障次数和最终状态。

默认输出位置由 Unity 平台决定：

```text
<Application.persistentDataPath>/HCIRobotTrials/trial-<UTC id>.jsonl
```
