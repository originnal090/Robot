# Orange Pi offline bundle

Build the bundle on Linux for the **same architecture, Python minor version, OS and glibc generation** as the target Orange Pi. Wheels produced on Windows or x86-64 are not valid for an AArch64 board.

Recommended production baseline: 64-bit Linux and Python 3.11 or newer.

## Prepare while online

```bash
python3.11 -m venv build-venv
build-venv/bin/pip install --upgrade pip wheel
mkdir -p bundle/wheelhouse
build-venv/bin/pip download --only-binary=:all: --dest bundle/wheelhouse .
cp -a deploy bundle/
cp config.toml pyproject.toml uv.lock bundle/
python tools/sha256_manifest.py bundle
```

If `pip download` reports that no compatible binary exists, stop and resolve that dependency on a matching target system. Do not silently fall back to a long source build during field installation.

Also copy the repository source or a locally built `hcirobot` wheel into the bundle. For a complete air-gapped setup, download required Debian packages such as Python, `venv`, and optional `python3-tk` with the target distribution's package tools.

## Install on the Orange Pi

```bash
python3 tools/sha256_manifest.py bundle --verify
python3.11 -m venv /opt/hcirobot/.venv
/opt/hcirobot/.venv/bin/pip install --no-index --find-links bundle/wheelhouse hcirobot
/opt/hcirobot/.venv/bin/python tools/preflight_orangepi.py \
  --config /etc/hcirobot/robot.toml
```

Only add `--probe-video` after the camera URL/device is configured. Only add `--probe-robot` while TonyPi is supported off the ground: opening its TCP service may execute the `stand` action.

## Target verification record

Record these values with the bundle:

```bash
uname -a
python3.11 --version
ldd --version | head -n 1
/opt/hcirobot/.venv/bin/python -c 'import cv2,numpy,PIL; print(cv2.__version__, numpy.__version__, PIL.__version__)'
```

The repository does not ship a prebuilt AArch64 wheelhouse because the final Orange Pi model and OS image are not yet fixed.
