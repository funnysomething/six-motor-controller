# Testudo datasets

Keep datasets, labels, capture sessions, trained weights, and model exports inside
this repository and track them in Git. Python virtual environments, build output,
and disposable caches remain ignored.

- `testudo/` is the prepared dataset: 116 training images and 30 validation images,
  with matching annotations and a portable `data.yaml`.
- `testudo_cvat_export/` preserves the original CVAT annotations and image list.
  It has no image files or validation split. Train using `testudo/data.yaml`.
- Original camera sessions are under `raspberry_pi/dataset_capture/sessions/`.
- Trained weights and exports are under `testudo_training/runs/`.

From the repository root on your Mac:

```sh
python testudo_training/train.py --data datasets/testudo/data.yaml --device mps --batch 4 --export-ncnn
```

For YOLO11n training targeting the IMX500 camera processor:

```sh
python testudo_training/train.py --data datasets/testudo/data.yaml --model yolo11n.pt --device mps --batch 4 --name testudo_imx500
```

After copying or cloning the repo to Linux, export the existing trained YOLO11n:

```sh
python testudo_training/export_imx500.py --weights testudo_training/runs/testudo_imx500/weights/best.pt --data datasets/testudo/data.yaml --imgsz 640
```

No machine-specific dataset path is stored in the prepared YAML. The scripts pass
its absolute filename to Ultralytics, which resolves the image directories beside
that file. Copy/clone the complete repo to keep images and weights available.

The prepared split comes from one session, with five boundary frames excluded.
Use separately collected sessions for stronger evaluation. Preserve old run
configuration files as historical records, even when they mention earlier paths.
