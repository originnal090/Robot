# 红球 LAB 调参与 OrangePi 轻量模型

后续 LAB+SVM/ROI 与 feature-v2 并行实验已完成，详见
[实验对照报告](edge-experiments-20260914.md)。混合 v1 为优先继续方向；删除分位数的
v2 有召回退步，暂不推荐替换。实验没有改变默认检测流程。

2026-09-14，检查 `capture-20260914-091540`（1672 帧）和
`capture-20260914-093059`（568 帧）。两条路线已实现并离线验证；未连接实板。

## 当前交付

- 默认 LAB：A 下限从 132 提高到 **136**，其余 LAB 范围、圆度、宽高比、
  强红核心和连续确认门控保持。同步 `config.toml`、OrangePi 部署配置和代码默认值。
  核心像素计数改为候选 ROI，测试验证与全图计数一致。
- 实验学习模型：宽松 LAB/HSV 多阈值候选 + 32×32 HOG/颜色/形状特征 +
  线性 SVM。**406 个特征，实际压缩权重 2349 字节（2.35 KB）**。
  `artifacts/edge-model-20260914/red_ball_svm.npz` 已生成。
  本机 OpenCV 5 wheel 缺少 HOGDescriptor 和 ml，使用 OpenCV Sobel + NumPy HOG，
  以及 NumPy 对偶坐标下降训练平方 hinge 损失的线性 SVM，无新增依赖。
- CLI `--edge-model PATH` 显式选用模型；默认及 GUI 仍使用 LAB。
  模型文件在打开视频/机器人连接前验证。模型输出沿用连续确认、丢帧后重新确认语义。

模型只对颜色候选做二分类，尚不是摆脱颜色约束的通用深度检测网络。
2.35 KB 是权重文件大小，不是 Python/OpenCV 的运行内存。运行需要兼容的 NumPy、
OpenCV 和 Python；不依赖 NPU。尚未确认 OrangePi 型号，不能承诺板上帧率。

## 数据与结果

第一组有 **346 张字节级重复帧**（1326 个唯一文件）；第二组虽然没有完全相同文件，
后半段仍是高度相关的静止画面。第一组前半段大多无球，不能把总体检出率叫召回率。

人工目视标注见 `data/red-ball-20260914/annotations.json`：29 张训练/开发图，
23 张固定测试图，跨 split 没有重复文件 hash。正样本训练/测试分属两次采集，
负样本按时间段隔离；同一场地且样本少，只能说明本次开发样本表现。
框来自联系表目视估计，非精确分割标注；仅剩球帽的 01457 帧排除。

| 方法 | 开发集定位正确 / 15 有球 | 固定测试定位正确 / 13 有球 | 测试无球误检 / 10 |
|---|---:|---:|---:|
| 原 LAB A≥132 | 11 | 13 | 0 |
| 新 LAB A≥136 | 12 | 13 | 0 |
| 轻量 SVM | 14 | 13 | 0 |

定位正确定义为预测框与标注框 IoU≥0.5；这里只比较单帧候选，不评价控制器闭环成功率。
SVM 在开发集有一帧定位不达标，按 FP 和 FN 各计一次。固定测试过于容易，三种方法
持平；不能据此宣称模型优于 LAB 或泛化准确率达到 100%。模型参数和阈值没有按 test 调优。

LAB 全量回放：第一组候选 **488→535**，第二组 **568→568**。
第一组新增 62 个候选，也丢失 15 个原候选，净增 47，部分丢失帧确实有球。
调参不是逐帧无回退；提高 A 下限可分离贴近大球与暖色背景，也可能剪掉模糊球边缘。
因此选择开发集同样改善的最小变化 A=136，没有继续放宽圆度/核心门控。
需要回退时把 `[detection].lab_min` 设回 `[30,132,100]`。

本机 Windows AMD64、OpenCV 单线程，SVM 完整 `process`（排除读盘）：

| 回放抽样 | 中位耗时 | P95 |
|---|---:|---:|
| 第一组 60 帧 | 16.5 ms | 43.1 ms |
| 第二组 30 帧 | 17.2 ms | 22.2 ms |

这些数字是桌面测量，不能替代 OrangePi 实测；繁杂背景最多处理 128 个候选，特征提取
比权重点积更耗时。训练报告和逐图预测在 `artifacts/edge-model-20260914/report.json`，
LAB 逐帧数据在 `artifacts/lab-tuning-20260914/`。
困难开发帧的三路可视化比较见 `artifacts/edge-model-20260914/comparison.jpg`；
图中的 hit 仅代表输出候选，不代表定位框达到 IoU 标准。

验证：完整 pytest 回归和所有修改代码的 Ruff 检查通过，模型配置预检及 30 帧
recording 后端 CLI 回放通过。为完成回归，修正两个已有测试假设：超时配置用例仍
匹配旧的 0.75 秒值；视频恢复用例假定 latest-wins 队列不会丢弃突发帧，导致无限等待。
后者现验证停顿后的新帧被处理，并设置 5 秒测试 watchdog；生产视频逻辑未改动。

## 复现与部署

后续的 LAB+SVM/ROI 提速、困难负样本和几何实验已保留独立权重及 GUI 可选配置，
见[版本清单与实测说明](edge-versions-20260914.md)。

在项目根目录、已安装项目的 Python 环境运行：

```sh
python tools/train_edge_detector.py --annotations data/red-ball-20260914/annotations.json
python -m hcirobot --config config.toml --edge-model artifacts/edge-model-20260914/red_ball_svm.npz --check-config
python -m hcirobot --config config.toml --edge-model artifacts/edge-model-20260914/red_ball_svm.npz --source artifacts/captures/capture-20260914-093059/frame-%05d.png --backend recording --max-frames 30
```

OrangePi 安装步骤沿用 `deploy/README.md`。将权重和代表性图片一起复制到板上，先运行
下面的纯离线基准（不打开摄像头、不发送动作）：

```sh
python tools/benchmark_edge_detector.py --frames artifacts/captures/capture-20260914-091540 --edge-model artifacts/edge-model-20260914/red_ball_svm.npz --out artifacts/orangepi-svm.json --limit 100
python tools/benchmark_edge_detector.py --frames artifacts/captures/capture-20260914-091540 --out artifacts/orangepi-lab.json --limit 100
```

报告自动记录机器架构、Linux 设备树型号、线程数、p50/p95 和 Linux 进程峰值 RSS。
RSS 包括 Python/OpenCV；不支持的平台记录 null。权重与 LAB 的球半径计算方式不同，
模型路线需要重新核对到达距离标定，不能直接把当前 LAB 到达阈值视为已验证。

## 课程 ZIP 与官方资料依据

课程 `Example/orangepi/robot-perception/perception/detector.py` 就是 LAB+轮廓检测，
未找到可直接复用的红球神经网络权重。课程颜色调试 PDF 第 6–7 页要求先调 A，再按光照
调 L/B。包内 5.35 MB Caffe SSD 是人脸检测模型，不适合红球。课程没有确认用户板型号。
选择提取文件和详细来源见 `artifacts/course-edge-research/README.md`。

- [OpenCV HOG/SVM 教程](https://docs.opencv.org/4.x/dd/d3b/tutorial_py_svm_opencv.html)：支持此类轻量分类路线；本项目为兼容现有 wheel 实现了 NumPy 训练与权重推理。
- [MobileNetV3-Small 官方规格](https://docs.pytorch.org/vision/stable/models/generated/torchvision.models.mobilenet_v3_small.html)：可作为后续 ROI 小型 CNN 路线，需更多场景标注再训练；本次没有训练 CNN。
- [Rockchip YOLOv8 部署示例](https://github.com/airockchip/rknn_model_zoo/blob/main/examples/yolov8/README.md)：确认 RK3588 等支持平台后可比较 YOLOv8n INT8/RKNN，本次没有生成或上板测试 RKNN 模型。
