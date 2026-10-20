# Computer vision launchers

Run these scripts from the repo root. They also work when called by absolute path
from another directory. Each changes to the repository root and selects the
appropriate Python environment. You do not need to run `source .../activate`.

Add `--dry-run` to any launcher to print its commands without opening a camera,
training, downloading, or installing anything. Additional arguments are forwarded
to the underlying program; later values override the profile defaults.

## Raspberry Pi camera and data capture

```sh
./scripts/vision/preview.sh
./scripts/vision/capture.sh
./scripts/vision/capture-negatives.sh
```

Preview runs for 120 seconds. Capture opens a live preview and starts paused.
Space/S saves a photo, P toggles automatic capture, and Q exits. Remove the statue
for negative capture sessions. These use system Python and require the camera
packages from apt. They do not need YOLO installed. Camera sessions stay under
`raspberry_pi/dataset_capture/sessions/`.

## Raspberry Pi CPU detection

One-time setup, if your existing Pi CPU environment is not ready:

```sh
./scripts/vision/setup-pi-cpu.sh
```

This installs system camera packages and uses this repo's existing Pi 4/Python
3.13 compatibility setup for `.venv-pi4`, then checks/installs NCNN. It preserves
an existing `.venv-pi4`. The compatibility installer deliberately stops on other
Python versions; follow `raspberry_pi/CAMERA.md` for the standard environment on
another version. Installation can download large packages. It does not reboot
automatically; reboot if this is your initial IMX500 firmware installation.

Select the detector:

```sh
./scripts/vision/detect-ncnn.sh
./scripts/vision/detect-yolo26.sh
./scripts/vision/detect-yolo11.sh
```

These all run the neural network on the **Pi CPU**, with a local preview for 120
seconds. They choose `.venv-pi4`, falling back to `.venv`. Defaults are the trained
assets already present in this repo:

- NCNN: `testudo_training/runs/testudo-3/weights/best_ncnn_model`.
- YOLO26n PyTorch: `testudo_training/runs/testudo-3/weights/best.pt`.
- YOLO11n PyTorch: `testudo_training/runs/testudo_imx500/weights/best.pt`.

The existing NCNN export has input size 640. Its launcher reads `metadata.yaml`
and rejects a conflicting input size. Select another exported model or change
the duration/confidence with extra arguments:

```sh
./scripts/vision/detect-ncnn.sh --model testudo_training/runs/another-run/weights/best_ncnn_model --seconds 300 --conf 0.4
./scripts/vision/detect-yolo26.sh --model testudo_training/runs/testudo_fast/weights/best.pt --imgsz 320
```

Model paths are relative to the repo root. Copy/clone the complete repo onto the
Pi so these assets are available. Missing files produce an error rather than
silently choosing pretrained generic weights.

## Raspberry Pi camera AI detection

```sh
./scripts/vision/setup-imx500.sh
./scripts/vision/detect-imx500.sh
```

Setup installs camera packages and Sony modlib into the separate `.venv-imx500`.
Detection defaults to `testudo_training/runs/testudo_imx500/weights/best_imx_model`.
This must contain `packerOut.zip` and `labels.txt`. Those are produced by the
Linux export below. The launcher cannot turn your existing NCNN export into an
IMX model. Initial packaging and camera firmware loading can take minutes.

The camera runs the neural network; the Pi handles results and display. For SSH:

```sh
./scripts/vision/detect-imx500.sh --headless --seconds 300
```

## Training on your Mac

```sh
./scripts/vision/setup-training.sh
./scripts/vision/train-yolo26.sh
./scripts/vision/train-yolo26-fast.sh
./scripts/vision/train-yolo11.sh
```

Run one training profile at a time. Setup creates/reuses `testudo_training/.venv`
and installs its requirements. Training uses `datasets/testudo/data.yaml`, MPS on
an Apple Silicon Mac, or CPU elsewhere. Profiles are:

- YOLO26n: 640 input, batch 4, 100 epochs; run name `testudo`.
- Fast YOLO26n experiment: 320 input, batch 16, 50 epochs; run name `testudo_fast`.
- YOLO11n for camera AI: 640 input, batch 4, 100 epochs; run name `testudo_imx500`.

Use `--batch 4` if the fast profile runs out of memory. Smaller inputs and fewer
epochs change the accuracy/speed tradeoff. Existing runs are preserved, so the
actual saved directory may acquire a suffix. Read the trained weights path
printed at completion. Training profiles do not automatically export. You can
add `--export-ncnn` to train and export in one command, or use a separate exporter.

```sh
./scripts/vision/train-yolo11.sh --epochs 50 --batch 8 --name testudo_imx500_v2
```

## Export completed training

NCNN, on your Mac or a configured Linux machine:

```sh
./scripts/vision/export-ncnn.sh --weights testudo_training/runs/testudo_fast/weights/best.pt --imgsz 320
```

The default weights, if omitted, are `testudo-3/weights/best.pt` and default input
size is 640. That run already has an NCNN export; the exporter refuses to overwrite
it. To retain exports at different sizes, copy the checkpoint into a different
directory inside the repo and export from there. Full NCNN directories contain
metadata and must stay together.

IMX500 export requires Linux, Java 17+, the converter's dependencies, and the full
calibration dataset. Copy/clone this complete repo to the Linux environment:

```sh
./scripts/vision/setup-training.sh
./scripts/vision/export-imx500.sh
```

Defaults are the trained YOLO11n `testudo_imx500/weights/best.pt`, the repo dataset,
and size 640. The launcher stops immediately on macOS with Linux guidance.
Use `--weights` to select a newer YOLO11n run. Export creates `best_imx_model`
beside the selected weights; point `detect-imx500.sh --model-dir ...` there when
using another run. See `testudo_training/IMX500.md` for conversion prerequisites.

## Benchmark generic models

```sh
./scripts/vision/benchmark.sh
```

This runs the existing comparison of **generic pretrained** YOLO26n and YOLO11n,
both PyTorch and NCNN, at 320. It captures common reference frames, downloads and
exports models, and saves timing reports under `camera_results/`. It does not
measure the accuracy of your trained Testudo detectors. Those reports are
disposable and ignored; preserve anything needed inside the repo and explicitly
add it to Git if desired.

All asset defaults stay in the repository. Dataset images, labels, model exports,
and weights are intended for Git. Environments, caches, and build products are
ignored. Launching a run does not automatically commit generated files. Preview
windows need the Pi desktop; ordinary SSH cannot display them. Quit CPU preview
with Q and stop any program with Ctrl+C.
