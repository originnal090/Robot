# Web 控制台

Python 视觉、决策、手柄读取和机器人通信运行在香橙派；电脑或手机浏览器显示控制台。
桌面版 `hcirobot-gui` 仍可使用，与 Web 服务共用 `session_runtime.py` 的启动和清理逻辑。
静态页面随 Python wheel 打包，无 Node.js、CDN 或前端构建步骤。

## 本机试用

在项目目录运行：

```bash
uv sync --extra web --extra gamepad
uv run hcirobot-web
```

打开 [本机控制台](http://127.0.0.1:8090)，点击“取得控制权”，选择
`synthetic` 视频源和“模拟记录”后端，然后开始预览。启动服务本身不会连接机器人；
开始会话后默认未武装，必须显式点击“武装自治”。

## 香橙派局域网运行

先按 `deploy/README.md` 准备 64 位 Linux、Python 3.10+ 和目标架构的依赖。
修改 `deploy/config/config.orangepi.toml` 中的机器人地址。运行：

```bash
uv sync --extra web --extra gamepad
export HCIROBOT_WEB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
# 将生成的令牌保存到自己的密码管理器；在浏览器登录页填写同一令牌。
./deploy/start_webui.sh
```

浏览器访问 `http://香橙派IP:8090`。非回环监听必须设置 `HCIROBOT_WEB_TOKEN`。
令牌可通过登录页建立 HttpOnly、SameSite=Strict 会话 Cookie；程序客户端也可发送
`Authorization: Bearer <token>`。令牌不会放进 URL。HTTP 适用于受信任的隔离局域网；
需要保护传输或使用远端浏览器手柄时，配置 HTTPS。

Web 服务进程独占机器人连接。不要同时运行桌面 GUI、CLI 或另一 Web 实例来控制同一机器人。
不要使用多 worker 或自动 reload 启动硬件服务。应用入口固定使用一个 worker。

标注后的只读 MJPEG 流默认单独监听 8091，根路径就是视频流：
`http://香橙派IP:8091`。Unity 等客户端直接填写该地址，不需要附加
`/unity-stream.mjpg`、`/?action=stream` 或其他 path。该端口不要求 WebUI 令牌，
只应暴露在可信局域网；可用 `HCIROBOT_MJPEG_PORT` 修改端口。

## 两种手柄位置

`deploy/start_webui.sh` 中默认的 `HCIROBOT_GAMEPAD=1` 指的是**物理手柄连接香橙派**，
由香橙派上的 Python/pygame 读取。若手柄连接操作电脑，由网页通过浏览器 Gamepad API
读取，应将环境文件中的 `HCIROBOT_GAMEPAD=0`；浏览器页面再勾选“浏览器手柄”。

### 手柄连接香橙派

用 `--gamepad` 启动，并安装 `gamepad` extra。Linux 使用既有 pygame/SDL 后端，
服务用户需具有相应 `/dev/input` 设备的读取权限。
取得网页控制权后，勾选“控制权交给香橙派手柄”再开始会话。

- 本机手柄独占运动控制；B 键武装/抢断，LS 前后/横移，RS 旋转，十字键俯仰。
- 网页进入观察角色，可以停止、急停；网页退出不终止本机手柄会话。
- 切回网页控制需先停止会话，待清理完成后重新取得控制权。
- 无相机时可以选择“仅手动控制”；该模式不能武装自治。
- 不假定任何特定型号手柄在香橙派上已经验证；可先运行 `tools/gamepad_probe.py`。

### 手柄连接操作电脑

保持浏览器持有控制权，勾选“使用浏览器手柄”。标准映射如下：

| 输入 | 操作 |
| --- | --- |
| LS Y / X | 前后 / 横移 |
| RS X | 左右转向 |
| B | 武装 / 解除自治 |
| 十字键上 / 下 | 逐档抬头 / 低头 |
| LT / RT / RB | 前爬起 / 后爬起 / 蹲下灭火 |

浏览器 Gamepad API 要求安全上下文：`localhost` 或 HTTPS。
直接访问 `http://香橙派IP` 时，页面的手动按钮可以使用，浏览器手柄可能不可用，
页面会显示原因。参见 [MDN Gamepad API 说明](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/getGamepads)。
仅接受浏览器的 `standard` 映射；设备按一下按钮后才可能出现在页面中。

提供浏览器信任的证书和私钥可直接启用 TLS：

```bash
uv run hcirobot-web --host 0.0.0.0 --config deploy/config/config.orangepi.toml \
  --ssl-certfile /etc/hcirobot/web.crt --ssl-keyfile /etc/hcirobot/web.key
```

## 控制、视频和参数

- 桌面使用固定高度工作台：画面与运动按钮常驻，侧栏通过“状态 / 连接 / 调参 / 采集”
  标签切换，日志从顶栏弹出。1366×768 下所有常用操作与各侧栏面板无需滚动。
  手机端使用“画面 / 遥控 / 状态与设置”分页，底部会话操作和顶部急停始终保留。
- 只有持有租约的浏览器可以开始、武装、点动、发送动作、调参和采集。
  其他已登录页面可观看相同视频及状态，也能停止和急停。
- 手动指令每次最多生效 **0.30 秒**，持续输入需要续期。浏览器约每 0.50 秒发送心跳，
  服务端租约在 **3.0 秒**未续期后失效，并请求停止整个会话。
  这些是软件期限，物理停止还受控制周期、网络和动作组执行影响。
- 页面关闭、隐藏到后台会释放浏览器控制；回来或重连后不会自动取得控制权、武装或继续运动。
  页面失去焦点时停止手动输入。每条运动消息绑定会话 ID、租约 ID 和递增序号，
  旧会话、过期租约和重复运动包均被拒绝。
- 停止/急停优先于队列中的未执行动作；故障或急停复位必须等旧工作线程退出。
- 相机重连、画面冻结、恢复后的目标重新确认、TCP 断开处理继承桌面版策略。
- 视频采集和控制线程独立于网页。JPEG 编码在线程中按最多约 10 Hz 执行，
  最新帧覆盖旧帧，多个浏览器共享已编码图像，慢客户端不会堵住机器人控制。
- LAB、SVM、Hybrid 识别器仍可使用；WebUI 会分别保存 Ascend `.om` 路径与
  SVM/Hybrid 版本选择，切换类型时不会复用另一类型的路径。内置版本列表会检查
  `artifacts/` 下对应的 profile/权重是否已部署，缺失版本显示为不可用。Ascend NPU
  模式填写香橙派文件系统上的
  `.om` 路径。CANN 环境与 ACLLite 在启动脚本中加载，模型在每次会话内初始化一次，
  每帧走 NPU 推理并在会话结束时释放。`.pt` 不能直接用于板端闭环。
- `[startup]` 的起步直行是 WebUI 每次会话的一次性可选项，默认关闭；开始会话后该勾选
  自动清除，断线重建会话不会意外再直行。停稳后机器人持续右转，只有目标连续确认达到
  `acquire_frames`（香橙派默认 6 帧）才交给视觉闭环。
- 到达判定以球框尺寸为主，但同时要求目标居中，并在停车后的连续帧中尺寸稳定；
  `arrival_radius_ratio` 可在页面热调，另外两项约束在 TOML 中配置。
- 调参仅影响当前会话，不写回 TOML。会话启动后读取 profile 的实际参数，
  热更新保留未修改字段。接近策略、识别器和避障开关在运行期间锁定。
- 采集的原始 PNG 保存在服务工作目录的 `artifacts/captures/`；网页显示路径和帧数，
  会话结束会停止采集。不把服务器路径当作浏览器下载地址。

## systemd

新增 `deploy/systemd/hcirobot-web@.service`、`deploy/env/hcirobot-web.env.example`。
把环境示例复制为 `/etc/hcirobot/web.env`，设置自己的令牌，并限制该文件读取权限。
示例配置 `/etc/hcirobot/robot.toml` 对应实例 `hcirobot-web@robot.service`。
模板与同名 `hcirobot@robot.service` 冲突，避免两者同时启动。
服务启动或重启只提供网页，不自动创建会话或武装。

需要本机手柄时，安装 gamepad extra，确认设备权限，在 systemd override 中
给 `ExecStart` 追加 `--gamepad`；例如设备属于 `input` 组时可添加
`SupplementaryGroups=input`。按实际系统确认组名。

香橙派以用户服务运行且桌面 seat 属于 LightDM 时，`uaccess` 可能只把手柄 ACL
授予 LightDM，导致 USB 可见但 WebUI 显示“无读取权限”。可安装项目提供的持久规则：

```bash
sudo ./deploy/install_gamepad_udev.sh HwHiAiUser
```

规则只匹配 `ID_INPUT_JOYSTICK=1` 的 event 节点，并在 USB 重新插拔后继续生效。

## API

| 路径 | 用途 |
| --- | --- |
| `POST /api/login` | 登录 |
| `GET /api/defaults`, `GET /api/state` | 默认配置和状态快照 |
| `POST /api/control/claim`, `/release` | 取得/释放控制租约 |
| `POST /api/session/start`, `/stop`, `/arm`, `/disarm`, `/estop`, `/reset` | 会话操作 |
| `POST /api/action/{name}` | 白名单动作 |
| `PATCH /api/tuning` | 当前会话参数 |
| `POST /api/capture` | 原始帧采集开关 |
| `WS /ws/events` | 10 Hz 状态快照、心跳和手动运动输入 |
| `GET /stream.mjpg` | 共享 JPEG 预览 |

状态通道连接后获得 `client_id`；claim 返回 `lease`。改变会话状态的请求包含
`client_id`、`lease`、`session_id`（开始会话除外）。停止和急停不要求租约。
心跳和运动消息示例：

```json
{"type":"heartbeat","lease":"<claim 返回值>"}
{"type":"manual","lease":"<claim 返回值>","session_id":1,"seq":1,"velocity":0.35,"steer":0.0,"lateral":0.0}
```

## 验证

```bash
uv run --extra web pytest
uv run ruff check src/hcirobot/service.py src/hcirobot/session_runtime.py src/hcirobot/web.py tests/test_web.py
# 另一个终端已启动本机 hcirobot-web 后；默认使用已安装的无头 Edge
uv run --extra web --with playwright python tools/smoke_web_ui.py
```

浏览器检查仅连接回环地址，并显式选择 synthetic + recording，覆盖画面、点动松开、
标准映射手柄输入、参数更新、采集、多页面控制权、观察端急停、关闭页面停车，
以及 1366×768 桌面、390×844 手机截图与双向页面溢出检查；桌面还验证各标签面板
无需滚动、主要运动和停止按钮均在视口内。输出位于 `artifacts/webui-smoke/`。
香橙派实际推理帧率、USB/蓝牙兼容性、设备权限与真机停车时延需要板端实测。
