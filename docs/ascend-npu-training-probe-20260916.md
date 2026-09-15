# Ascend 310B1 YOLO training probe (2026-09-16)

## Outcome

YOLO11n fine-tuning did not complete an optimizer step on the tested Orange Pi
AI Pro 20T software stack. The supported deployment path remains offline
training followed by ONNX-to-OM conversion and pure NPU inference.

No system package, CANN component, driver, service, or active robot model was
changed during the probe.

## Environment and data

- Ascend 310B1 with 23673 MB reported NPU memory
- CANN 8.0.0
- Python 3.10.12
- PyTorch 2.1.0
- torch_npu 2.1.0.post10
- Ultralytics 8.4.152
- YOLO11n initialized from the existing v1 red-ball weights
- 95 training images: 71 positive and 24 background
- 640 px input, batch size 2, one planned epoch (48 batches)
- Model layers 0 through 10 frozen; neck and detection head trainable
- FP16 autocast enabled

## Unsupported training operations

The FP16 model forward path works, and a smaller frozen-backbone test completed
forward and backward. The real Ultralytics training loop then exposed missing
310B1 kernels in the installed CANN/OPS stack:

1. `MaxPoolWithArgmaxV1` in Ultralytics' generic FP32 AMP check.
2. `Cumsum` in no-gradient target packing.
3. `GatherElements` in Distribution Focal Loss.
4. `NPUClearFloatStatusV2` in dynamic gradient scaling.
5. `SelectV2` in CIoU minimum/maximum backward.
6. `SigmoidCrossEntropyWithLogitsGradV2` in classification-loss backward.

Target packing and task-aligned assignment were safely moved to CPU because
they are no-gradient metadata operations. DFL gather and CIoU min/max were
replaced with mathematically equivalent differentiable expressions that kept
their gradients on NPU. A static scaler avoided the unsupported overflow-status
operator. The classification backward kernel remained unavailable.

Two bounded alternative-loss probes were attempted. `softplus` backward did
not return within 180 seconds; a basic sigmoid/log expansion was then run under
a remote 120-second hard timeout and exited with status 124. No probe process
remained afterward.

## Inference fallback verification

After the training probes, the existing `yolo11n_640.om` was loaded again with
ACLLite and run for three iterations on `frame-00001.png`. It returned one
`red_ball` detection at `[359.0, 166.375, 384.0, 191.625]` with confidence
`0.953125`. Pure NPU inference averaged 8.824 ms (8.720–8.983 ms), confirming
that the unsuccessful training attempts did not affect the isolated inference
deployment.

The board continued to report the pre-existing `npu-smi` Health `Alarm`; no
restart or driver intervention was attempted.
