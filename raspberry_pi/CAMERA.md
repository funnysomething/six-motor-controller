# Raspberry Pi 4B AI Camera test

For your custom Testudo detector on the camera AI processor, use
[`imx500_detect.py` and the YOLO11n workflow](../testudo_training/IMX500.md).
It requires retraining YOLO11n and an IMX export, rather than your YOLO26n NCNN model.

`camera_test.py` is a separate Python program that runs on the Pi. It uses Picamera2 for the CSI camera and OpenCV for images and optional preview. No PlatformIO build or ESP32 is involved.

Start with camera capture, then test the IMX500's bundled detector, then benchmark YOLO26n on the Pi CPU. These tests answer different questions. A working image does not prove the AI processor works, and a detector finding no objects does not mean the camera is broken.

## Connect and install

Power off the Pi before inserting the ribbon cable. Use the Pi 4B's 15-pin CSI camera connector, not the display connector. Seat the correct cable straight and latch both ends. Follow the [Raspberry Pi camera installation instructions](https://www.raspberrypi.com/documentation/accessories/camera.html) for cable orientation.

Use 64-bit Raspberry Pi OS Bookworm or newer with current camera packages. On the Pi:

```sh
sudo apt update
sudo apt full-upgrade
sudo apt install imx500-all python3-picamera2 python3-opencv python3-venv python3-pip
sudo reboot
```

The [official AI Camera setup](https://www.raspberrypi.com/documentation/accessories/ai-camera.html) supports Pi 4B and requires the IMX500 firmware. Initial firmware/model loading can take several minutes. Wait for startup rather than interrupting it immediately.

You are running directly on the Pi. Open a terminal in the project's `raspberry_pi` folder. From the project root:

```sh
cd raspberry_pi
```

## 1. Check the camera image

```sh
rpicam-hello --list-cameras
python3 camera_test.py --list-cameras
python3 camera_test.py --seconds 10
```

The camera list should identify `imx500`. The program defaults to camera index 0 and requests a 640 x 480 stream at 30 FPS. It saves `first_frame.jpg`, `last_frame.jpg`, `frames.csv` and `summary.json` in a new directory under `camera_results/`. Check the JPEG for a visible scene, correct colours and focus. The test checks streaming, not the full 12MP resolution or every sensor mode. The lens focus is manually adjustable; follow the camera's focus instructions if the image is blurry.

On the Pi desktop, run `python3 camera_test.py --preview --seconds 10` for a live window. Press Q or Ctrl+C to stop. Open the saved JPEGs from the Pi file manager. Without `--preview`, the program also works in a text console. Use `--camera 1` if the camera list indicates a different index. `--output my-test` selects a new result directory; existing directories are rejected to preserve earlier results.

## 2. Check the camera's AI processor

Run the bundled MobileNet SSD detector before trying a custom model. The [official example](https://www.raspberrypi.com/documentation/accessories/ai-camera.html) runs inference on the IMX500 and draws boxes on the Pi. With a local desktop display:

```sh
rpicam-hello -t 0s --post-process-file /usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json --viewfinder-width 640 --viewfinder-height 480 --framerate 30
```

To save a 10-second annotated video without opening a window:

```sh
rpicam-vid --nopreview -t 10s -o imx500_detection.h264 --post-process-file /usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json --width 640 --height 480 --framerate 30
```

Open the video in a player supporting raw H.264, such as VLC. Point the camera at a person or familiar everyday objects. Correct boxes and labels demonstrate that the on-sensor detector and host post-processing work. This demo does not measure per-inference latency. The requested video FPS is not a measurement of fresh AI results per second.

## 3. Test YOLO26n on the Pi CPU

The [Ultralytics Pi guide](https://docs.ultralytics.com/guides/raspberry-pi/) supports YOLO26 on Pi 4 and Pi 5 and recommends NCNN for ARM inference. Its [IMX500 integration guide](https://docs.ultralytics.com/integrations/sony-imx500/) explicitly limits on-sensor support to YOLOv8n and YOLO11n. YOLO26 is marked unsupported there, despite a generic export table displaying a YOLO26 IMX path. Use YOLO11n if the goal is YOLO running inside the camera. A `.pt` file or NCNN model runs on the Pi CPU in this program.

Create a virtual environment that can access the system Picamera2/libcamera packages:

```sh
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install 'ultralytics>=8.4.0'
python camera_test.py --mode yolo --model yolo26n.pt --imgsz 320 --seconds 30
```

The first YOLO run downloads the weights and needs internet access. Installation needs enough free disk space for PyTorch and dependencies. Start at input size 320, then try 640 to compare detection quality and speed. Stream size defaults to 640 x 480 independently of model input size. Both inference and drawing boxes require CPU time; do not expect the 30 FPS camera request to produce 30 YOLO predictions per second.

For the NCNN comparison, export at the same input size and use the resulting directory:

```sh
python -c 'from ultralytics import YOLO; YOLO("yolo26n.pt").export(format="ncnn", imgsz=320)'
python camera_test.py --mode yolo --model yolo26n_ncnn_model --imgsz 320 --seconds 30
```

Export may install extra dependencies and takes time. Re-export at 640 before testing NCNN with `--imgsz 640`. Keep exported models in separate directories if you want to retain both sizes. Use `--threads 2` or `--threads 4` to compare CPU usage. Add `--preview` on a local desktop to see detections live.

The CSV records capture wait time, prediction time and detection count per processed frame. The JSON includes mean, median and p95 prediction latency, processed FPS and available temperature/throttling readings. Prediction time includes preprocessing, inference and post-processing inside `model.predict`. The input is square-letterboxed with `rect=False` in both formats so the PyTorch run does not get a smaller rectangular input than NCNN. Processed FPS includes capture, drawing, logging, JPEG writes and optional display. Startup, exposure settling and three warmup predictions are excluded. Camera capture can skip frames while the CPU is busy. These are live pipeline measurements, not isolated neural network inference benchmarks or accuracy measurements. Each invocation gets a separate output folder.

## 4. Compare YOLO26n against YOLO11n in both formats

`compare_yolo.py` runs four combinations: YOLO26n PyTorch `.pt`, YOLO26n NCNN, YOLO11n PyTorch `.pt`, and YOLO11n NCNN. I chose YOLO11n because [Ultralytics' comparison](https://docs.ultralytics.com/compare/yolo11-vs-yolov8/) reports higher COCO accuracy, fewer parameters and fewer FLOPs than YOLOv8n. Those published figures do not establish which model is fastest on your Pi 4B.

With the virtual environment activated, run this directly on the Pi from the same `raspberry_pi` folder:

```sh
source .venv/bin/activate
python compare_yolo.py --imgsz 320 --frames 60 --repeats 2
```

The program captures 60 reference images at a requested 10 FPS. Keep your test objects in view for about six seconds after exposure settling. It closes the camera, downloads both models and exports fresh NCNN models at the selected input size. Export may install extra dependencies. Each output folder contains its own model exports, so a previous export at a different size is never reused accidentally.

All four combinations process the same saved JPEG images in separate processes, using square inputs, CPU inference and the same confidence, IoU and detection limit. Exports use `half=False`. Runtime backend optimizations still apply. PyTorch uses four inference threads; NCNN uses the Ultralytics backend's default thread count. The benchmark records that distinction rather than claiming thread settings control both backends. Each backend performs five unmeasured warmup predictions. The test order is shuffled within each repeat, with a 10-second pause before each test to reduce ordering and heat effects. Cooling and power still matter; a fixed pause does not guarantee the same CPU temperature.

This comparison does not show a live preview during timed inference. Drawing and saving boxes happen after measurement. It prints a final comparison and saves:

- `comparison.csv`, with one row per model/format/repeat.
- Per-test `summary.json` and `frames.csv`, with latency and temperature/throttling readings.
- Per-test `first_detections.jpg` and `last_detections.jpg`, for comparing detections on matching images.
- `settings.json`, recording test order, package versions and settings, plus the reference images and model files.

`prediction_fps` is 1000 divided by mean prediction milliseconds. Prediction latency includes preprocessing, inference and post-processing. `replay_fps` also includes JPEG decoding and loop overhead. Neither number is live camera FPS. Detection count is not accuracy: more boxes can include false positives. Compare the saved boxes visually; measuring precision/recall or mAP requires labelled images.

To compare at 640 using exactly the same captured images, replace the placeholder path with the first run's output directory:

```sh
python compare_yolo.py --imgsz 640 --input camera_results/compare_TIMESTAMP/images --frames 60 --repeats 2
```

Use `--frames 10 --repeats 1 --cooldown 0` for a quick functional check. Use more frames/repeats for steadier timing estimates. Ctrl+C stops the comparison and preserves completed results. If an export or test fails, the program reports failure and retains completed output rather than declaring a winner.

For a live preview of any one combination after the comparison, use `camera_test.py`:

```sh
python camera_test.py --mode yolo --model yolo11n.pt --imgsz 320 --preview --seconds 30
```

For NCNN, pass that model's directory under the comparison output's `models/` folder to `--model` and use the matching `--imgsz`. These four comparison cases all run on the Pi CPU. The MobileNet test above separately exercises the IMX500 AI processor.

## Troubleshooting

### Illegal instruction

An `Illegal instruction` crash is a native-code failure. It can happen in PyTorch, NumPy/OpenBLAS, OpenCV or another compiled dependency before any camera access or YOLO inference. [PyTorch has reported this failure on Pi 4](https://github.com/pytorch/pytorch/issues/132032), but that historical report does not establish which installed version is failing on your Pi. Switching model weights or switching to NCNN through Ultralytics still imports shared dependencies and may crash too.

Run this in the same activated virtual environment that crashes:

```sh
python check_vision_install.py
```

This uses separate subprocesses to test NumPy, OpenCV, PyTorch import and CPU convolution, torchvision NMS, Ultralytics import and NCNN import. A crashing child reports `SIGILL` while the diagnostic continues. It prints OS, architecture, Python and package versions without loading native libraries in the parent. It accesses no camera and downloads no models. An absent NCNN package is expected before NCNN export.

Use the first failing probe and exact Python/package versions to choose a compatible package build. Do not downgrade Ultralytics to an old version that predates YOLO26 or reinstall every dependency before identifying the failure. A PyTorch version suggested for Python 3.11 may have no wheel for Python 3.13. The diagnostic output and the original crashing command are needed before selecting a repair.

If PyTorch imports but its CPU convolution crashes, test convolution with MKLDNN disabled in the same activated environment:

```sh
python test_torch_convolution.py
```

This script prints the Torch version and backend setting, then runs convolution in a child process so it can report an `Illegal instruction` crash. It changes the backend setting only for that child process, accesses no camera and changes no installed packages. A passing test suggests a backend workaround worth testing with YOLO; it does not prove full YOLO inference works.

If disabling MKLDNN still crashes with your Torch 2.14.1/Python 3.13 setup, try a separate environment:

```sh
python setup_pi4_vision.py
```

This preserves `.venv` and creates `.venv-pi4` using the same Python 3.13 and system camera bindings. It installs Torch 2.7.1 and torchvision 0.22.1, then tests Torch import, CPU convolution and torchvision NMS before installing Ultralytics 8.4.174. These matched versions have [Python 3.13 ARM64 wheels](https://pypi.org/project/torch/2.7.1/). This is a compatibility trial, not a verified fix for Torch 2.14.1 on your hardware. It stops on a failing test and preserves the partial environment. It does not install NCNN; the comparison's export step handles that separately.

If the probes pass, activate the new environment and retain its package constraints during exports:

```sh
source .venv-pi4/bin/activate
export PIP_CONSTRAINT="$VIRTUAL_ENV/vision-constraints.txt"
python camera_test.py --mode yolo --model yolo26n.pt --preview --seconds 10
python compare_yolo.py --frames 10 --repeats 1 --cooldown 0
```

The constraints prevent dependency installation from silently replacing the Torch build being tested. If an export requires incompatible versions, it should report a dependency conflict instead. To return to the original environment, run `deactivate`, `unset PIP_CONSTRAINT`, then `source .venv/bin/activate`.

### Camera and inference issues

If Ultralytics reports `['ncnn'] not found` followed by `restart runtime or rerun command`, rerun the same command once in the same activated environment. That warning is emitted after an automatic package installation succeeds, but a later failure can have another cause. No Pi reboot is required just to start a new Python process. Check the full final error if it still fails.

For a separate NCNN installation/import check:

```sh
source .venv-pi4/bin/activate
python setup_ncnn.py
```

Use `.venv` instead if that is the environment used for your test. The script checks NCNN in a fresh process, installs a binary wheel with the same Python interpreter only when the module is missing, and checks again. It automatically keeps the Pi 4 environment's Torch constraints. It reports installation errors or SIGILL separately. It does not export models, access the camera or verify actual NCNN inference. If no compatible wheel is available, it stops and prints the pip error rather than silently compiling from source.

- No `imx500` in the camera list: power down, reseat the ribbon, confirm the CSI socket and cable type, then verify firmware installation and reboot. Do not use the legacy camera stack or `cv2.VideoCapture(0)` for this CSI test.
- Camera busy: close other `rpicam` or Picamera2 processes before starting another test.
- Dark, uniform or blurry image: check the lens cover, illumination and manual focus. Capturing bytes alone is not proof of a useful image.
- Picamera2 missing inside the virtual environment: recreate it with `--system-site-packages`; install Picamera2 and libcamera through apt.
- NumPy or OpenCV import errors after pip installation: check `python -m pip check` and the reported conflicting versions. The camera-only test can still run with system `python3` outside the environment. Recreate the environment with compatible versions rather than replacing OS camera packages with pip packages.
- YOLO install fails: confirm a 64-bit OS with `uname -m`, which should report `aarch64`, and check free space with `df -h`. This program has no GPU or AI HAT backend.
- Low or declining FPS: compare NCNN, smaller input and headless operation. Check cooling, power and `vcgencmd get_throttled`. A nonzero throttle bitmask can include historical events; it is not automatically a current temperature problem.

Local syntax and simulated-camera checks cannot verify the CSI cable, sensor, firmware, installed Pi packages or actual FPS. Run these steps on the Pi to measure your hardware.
