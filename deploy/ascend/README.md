# Ascend YOLO smoke test

This deployment is isolated from the existing robot services. It uses the CANN
8.0 installation and ACLLite bundled with the Orange Pi AI Pro 20T.

The validated model path on the board is:

```text
~/hcirobot-ascend/models/yolo11n_640.om
```

Run a single-image benchmark after sourcing CANN and adding the isolated Python
dependencies plus ACLLite to `PYTHONPATH`:

```bash
base="$HOME/hcirobot-ascend"
export PYTHONPATH="$base/pydeps:/usr/local/Ascend/thirdpart/aarch64/acllite${PYTHONPATH:+:$PYTHONPATH}"
. /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 "$base/ascend_yolo_infer.py" \
  --model "$base/models/yolo11n_640.om" \
  --image "$base/testdata/frame-00001.png" \
  --annotated "$base/testdata/frame-00001-result.png"
```

The OM was compiled from a static ONNX graph with:

```bash
atc --model=model.onnx --framework=5 --output=yolo11n_640 \
  --input_format=NCHW --input_shape="images:1,3,640,640" \
  --soc_version=Ascend310B1
```

No system package, service, or driver change is required. The board's system
Python is missing several CANN compiler/runtime dependencies, so compatible
ARM64 wheels are unpacked under `~/hcirobot-ascend/pydeps` instead of being
installed globally.

## Training support

Treat this deployment as NPU inference-only on the tested software stack. A
YOLO11n fine-tuning probe using PyTorch 2.1.0, torch_npu 2.1.0.post10,
Ultralytics 8.4.152, and CANN 8.0 reached the loss backward pass but could not
complete an optimizer step on Ascend 310B1. The installed OPS package lacks
several required training kernels, ending at
`SigmoidCrossEntropyWithLogitsGradV2` after earlier compatibility workarounds.

The failed probe did not replace this OM, install system packages, or modify
any robot service. See `docs/ascend-npu-training-probe-20260916.md` for the
exact scope and result.
