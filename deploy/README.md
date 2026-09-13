# Orange Pi + TonyPi deployment checklist

These files are templates, not universal image-specific installers. Replace users, paths, IP addresses, Python paths and action-group directories for the actual boards.

## Support baseline

- Orange Pi controller: 64-bit Linux, Python 3.11+, compatible AArch64 NumPy/OpenCV/Pillow wheels.
- TonyPi service: Python 3.8+, vendor `hiwonder` modules and matching action groups.
- Network: isolated LAN; TCP 5075 and MJPEG HTTP 8080 have no authentication.
- Only one process may own robot motion control.

## 1. Prepare TonyPi without motion

```bash
python3 --version
PYTHONPATH=/home/pi/TonyPi/HiwonderSDK \
  TONYPI_MODE=hardware TONYPI_REQUIRE_SONAR=1 \
  python3 /opt/hcirobot/robot_side/tonypi_server.py --check
```

`--check` must not execute an actuator action. It validates configuration, SDK/API availability, action-group files, Sonar policy and bindable ports. Hardware mode must fail instead of silently becoming dry-run.

For a computer-only protocol check, use explicit dry-run mode and simulated distance. Never leave simulation enabled in a production hardware environment.

## 2. Install TonyPi service

1. Copy `robot_side/tonypi_server.py` to `/opt/hcirobot/robot_side/`.
2. Copy `env/tonypi.env.example` to `/etc/hcirobot/tonypi.env` and replace all image-specific values. `TONYPI_ACTION_GROUP_CHECK_DIR` must be the directory the installed Hiwonder SDK already uses; it verifies files but does not reconfigure the SDK search path.
3. Copy `systemd/tonypi-server.service` to `/etc/systemd/system/` and replace `User`, `Group`, and paths when necessary.
4. Stop the old `TCP_connect.py` service before binding port 5075.
5. Run the preflight again, then enable the unit.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tonypi-server.service
journalctl -u tonypi-server.service -f
```

## 3. Prepare Orange Pi

1. Build or copy an offline environment as described in `offline/README.md`.
2. Install the project at `/opt/hcirobot` with its virtual environment at `/opt/hcirobot/.venv`.
3. Copy `config/config.orangepi.toml` to `/etc/hcirobot/robot.toml`; replace `TONYPI_IP` and calibrate the camera/detector.
4. Validate without opening resources:

```bash
/opt/hcirobot/.venv/bin/python -m hcirobot \
  --config /etc/hcirobot/robot.toml --check-config
/opt/hcirobot/.venv/bin/python /opt/hcirobot/tools/preflight_orangepi.py \
  --config /etc/hcirobot/robot.toml
```

5. With only the camera ready, repeat the preflight with `--probe-video`.
6. Only with TonyPi supported off the ground, repeat with `--probe-robot`. Connecting may run TonyPi's stand action.

The `hcirobot@.service` template starts an **unarmed** controller and deliberately omits `--arm`. Do not add automatic armed restart. Run armed trials under direct operator control.

## 4. First physical trial

1. Put TonyPi on a stand and keep physical power cutoff within reach.
2. Confirm all competing services and vendor demos are stopped.
3. Start with `TONYPI_MODE=dry-run` and protocol smoke tests.
4. Switch to hardware mode; test stand and small head movements.
5. Test one short forward/back/turn action and verify direction.
6. Block the Sonar by hand and verify fresh `DIST` readings and stopping thresholds.
7. Run Orange Pi preview without `--arm` and calibrate LAB/radius thresholds.
8. Arm only for a short, low-speed floor trial with one operator guarding the robot.
9. Disconnect video and TCP deliberately; verify the TonyPi watchdog reaches stand.
10. Restore the network and verify motion does **not** resume automatically.

## Still requires the actual hardware

- Hiwonder module path and callable API names.
- Action-group names, files, duration and physical stability.
- Servo IDs, neutral pulses and safe limits.
- Sonar I2C bus/address and readings (`bus 1`, `0x77` are current SDK assumptions).
- TonyPi MJPEG response headers and long-duration stability.
- CSI/libcamera/V4L2 behavior on the chosen Orange Pi image.
- AArch64 wheel compatibility with the chosen Python and glibc.
- systemd device permissions, groups and worst-case stop timeout.
