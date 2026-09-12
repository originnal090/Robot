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
| `grab` | boolean | - | 兼容保留；仿真运动驱动不实现抓取机构 |
| `t` | string | - | 可选兼容字段；服务器不按客户端时间排序 |

默认离散兼容模式：

1. `abs(steer) > 0.20` 时转向优先；正为 `TurnRight`，负为 `TurnLeft`。
2. 否则 `abs(v) > 0.20` 时前进或后退。
3. 两轴都不越死区则停车。
4. 阈值使用严格大于，所以恰好 `0.20` 仍在死区内。

在 `VirtualRobotTcpServer` 和 `TonyPiMotionDriver` 同时选择 continuous 后，两轴分别应用死区并保留 `[-1,1]` 连续幅值；可同时线速度和角速度运动。

### CMD 帧

```text
CMD:nod\n
CMD:shake\n
CMD:stand\n
```

动作名仅允许 1-32 个 ASCII 字母、数字或下划线。`TonyPiMotionDriver` 发布 `ActionReceived` 事件并记录 `LastAction`；`stand` 另外立即停车。`nod`、`shake` 和课程按钮动作不模拟具体舵机动画，场景脚本可订阅事件自行表现。

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
