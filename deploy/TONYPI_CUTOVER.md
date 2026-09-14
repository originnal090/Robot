# TonyPi 厂商服务切换手册

本方案使用两个 HCIRobot 服务替换厂商 `tonypi.service`：

- `tonypi-camera.service`：独占物理摄像头，提供 `8080` MJPEG；
- `tonypi-server.service`：提供机器人控制 TCP `5075` 和当前构建支持的遥测。

切换后不再提供厂商的 `9030` JSON-RPC 和手机 App 功能。不要删除厂商文件，以便回滚。

## 1. 从 Windows 电脑暂存文件

在项目根目录打开 PowerShell，把 `TONYPI_IP` 替换成机器人 IP：

```powershell
ssh pi@TONYPI_IP "mkdir -p ~/hcirobot-stage"
scp robot_side/tonypi_camera.py robot_side/tonypi_server.py pi@TONYPI_IP:~/hcirobot-stage/
scp deploy/systemd/tonypi-camera.service deploy/systemd/tonypi-server.service pi@TONYPI_IP:~/hcirobot-stage/
scp deploy/env/tonypi-camera.env.example deploy/env/tonypi.env.example pi@TONYPI_IP:~/hcirobot-stage/
```

## 2. 停服务前先确认占用者

在 TonyPi 上执行：

```bash
systemctl status tonypi.service --no-pager -l
systemctl list-units --type=service --all --no-pager | grep -Ei 'tony|robot|camera|mjpg|tcp|vr'
sudo ss -ltnp | grep -E ':8080|:5075|:9030|:9031'
pgrep -af 'TonyPi.py|TCP_connect.py|robot_vr_server.py|tonypi_server.py|tonypi_camera.py'
python3 -m py_compile ~/hcirobot-stage/tonypi_camera.py ~/hcirobot-stage/tonypi_server.py
python3 -c 'import cv2; print(cv2.__version__)'
```

记录占用 `5075` 的服务名。它可能不是 `tonypi.service`。后续只停止确认过的单元，不使用
范围过大的 `pkill`。

## 3. 停止厂商服务并安装

源码注释确认摄像头单元为 `tonypi.service`。把下面的 `OLD_5075.service` 替换成上一步
找到的单元；如果 `5075` 也是 `tonypi.service` 提供的，省略对应命令。

```bash
sudo systemctl stop tonypi.service
sudo systemctl stop OLD_5075.service
sudo ss -ltnp | grep -E ':8080|:5075' || true

sudo install -d -o pi -g pi /opt/hcirobot/robot_side
sudo install -d -m 0755 /etc/hcirobot
sudo install -m 0755 ~/hcirobot-stage/tonypi_camera.py /opt/hcirobot/robot_side/
sudo install -m 0755 ~/hcirobot-stage/tonypi_server.py /opt/hcirobot/robot_side/
sudo install -m 0644 ~/hcirobot-stage/tonypi-camera.env.example /etc/hcirobot/tonypi-camera.env
sudo install -m 0644 ~/hcirobot-stage/tonypi.env.example /etc/hcirobot/tonypi.env
sudo install -m 0644 ~/hcirobot-stage/tonypi-camera.service /etc/systemd/system/
sudo install -m 0644 ~/hcirobot-stage/tonypi-server.service /etc/systemd/system/
sudo systemctl daemon-reload
```

启动前检查 `/etc/hcirobot/tonypi.env`。如果当前构建启用了强制超声波策略，确认硬件存在且
可以读取。下面两个无动作预检都必须通过：

```bash
sudo -u pi bash -lc 'set -a; source /etc/hcirobot/tonypi-camera.env; set +a; \
  python3 /opt/hcirobot/robot_side/tonypi_camera.py --check'
sudo -u pi bash -lc 'set -a; source /etc/hcirobot/tonypi.env; set +a; \
  python3 /opt/hcirobot/robot_side/tonypi_server.py --check'
```

## 4. 暂时启动，先不设置开机启动

启动控制服务前架空机器人。逐个启动、逐个验证：

```bash
sudo systemctl start tonypi-camera.service
systemctl status tonypi-camera.service --no-pager -l
curl -fsS http://127.0.0.1:8080/healthz

sudo systemctl start tonypi-server.service
systemctl status tonypi-server.service --no-pager -l
sudo ss -ltnp | grep -E ':8080|:5075'
```

保持自治未武装，先从 Orange Pi/PC 检查预览。两个服务和一次短距离架空测试都通过后，再让
切换在重启后继续生效：

```bash
sudo systemctl disable tonypi.service
sudo systemctl disable OLD_5075.service
sudo systemctl enable tonypi-camera.service tonypi-server.service
```

## 5. 观察摄像头恢复

```bash
journalctl -u tonypi-camera.service -f
```

新服务会先释放失效的 OpenCV 句柄，再尝试重新打开；重试带退避和次数上限，也不会发送旧帧。
摄像头持续无帧或采集线程卡死时，主进程以故障状态退出，由 systemd 重启整个摄像头进程。
没有新帧时 `/healthz` 返回 `503`。

该机制可以恢复短暂 USB reset，不能修复容量不足的电池、供电模块、破损线缆或松动接头。

## 6. 回滚

如果原来还有独立的 `5075` 服务，把 `OLD_5075.service` 替换成已记录的原服务名：

```bash
sudo systemctl disable --now tonypi-camera.service tonypi-server.service
sudo systemctl enable --now tonypi.service
sudo systemctl enable --now OLD_5075.service
```

如果原来的 `5075` 程序是手动启动的，使用先前记录的原命令恢复，不执行最后一行。
