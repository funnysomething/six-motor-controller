# Testudo dataset capture on Raspberry Pi

Collect images with the Pi 4B's CSI IMX500 camera, using the same Picamera2 path as
`../camera_test.py`. This program only captures images. It does not run detection,
control motors, or require Ultralytics, PyTorch, or NCNN. Close other camera
programs before running it.

The default session is **dry, positive**: use the bare camera and dry statue.
Images are ordinary high-quality JPEGs with automatic exposure and white balance,
without detection boxes or preview text. "Raw camera" here means the unannotated
camera view, not Bayer RAW sensor files. YOLO training uses decoded color images.

## Install and run

Copy this entire directory to the Pi, or use the repository checkout there. On
Raspberry Pi OS, install the system camera packages:

```sh
sudo apt update
sudo apt install imx500-all python3-picamera2 python3-opencv
```

For initial camera installation, cable connections, firmware, and manual focus,
follow [the existing camera guide](../CAMERA.md). Initial IMX500 firmware loading
can take several minutes. If using a virtual environment, it must have been
created with `--system-site-packages` to access the apt-installed camera bindings.
The system `python3` can run this collector directly.

From the repository root on the Pi:

```sh
python3 raspberry_pi/dataset_capture/capture.py --list-cameras
python3 raspberry_pi/dataset_capture/capture.py --preview --manual --notes "dry statue, front, desk background"
```

With the preview window focused:

- Space or S saves one image.
- P starts or pauses automatic capture, saving every two seconds by default.
- Q, Escape, or closing the window finishes the session.
- Ctrl+C in the terminal also stops collection.

`--manual` starts paused so you can position the statue before saving. Omit it to
start automatic capture after the three-second exposure warmup. Preview text is
drawn on a separate copy and never saved in the training images.

For automatic collection over SSH without a display:

```sh
python3 raspberry_pi/dataset_capture/capture.py --interval 2 --max-images 100 --notes "dry, side and diagonal views"
```

The first automatic image is saved after warmup. Move between views, then hold
still for each capture. Use `--seconds 120` for a duration limit. If both limits
are supplied, the first reached ends collection. Without either limit, capture
continues until stopped. The duration starts after warmup and includes pauses.
Manual saves also count toward `--max-images`. Slow disk writes do not trigger
bursts to catch up with the timer.

Remove Testudo completely for a separate negative session:

```sh
python3 raspberry_pi/dataset_capture/capture.py --preview --manual --kind negative --notes "orange objects, no statue"
```

Every invocation creates a unique directory under `sessions/` beside the script,
regardless of the terminal's current directory. Keep these sessions in Git.
Use `--output` to select another parent directory within this repository.

Each session contains:

- `images/*.jpg`, with globally distinct session-prefixed names.
- `frames.jsonl`, one record per saved image, with dimensions, timing, and metadata
  from that same camera request, including exposure/gain when supplied by the sensor.
- `session.json`, with dry/underwater setting, positive/negative setting, notes,
  camera identification, configuration, final count, and completion/failure status.

Session files retain successful captures when a later capture fails or the run is
interrupted. An abrupt power loss can leave the summary stale; inspect the images
and frame records. Back up the sessions before clearing the Pi's storage.

Other options include `--camera 1`, `--width 640 --height 480`, `--fps 15`,
`--quality 95`, and `--warmup 5`. Use `--help` for all options. Capture defaults to
1280 x 960 at a requested 15 FPS. This preserves more detail for labeling than the
existing 640 x 480 benchmark stream. Use the deployment stream size when you want
to check how much detail the eventual detector will receive. Stream dimensions
and YOLO model input size are separate settings; images need not be square.

## Dry statue capture checklist

1. Set the IMX500's manual focus for the working distance. Check a saved JPEG at
   full size, especially the head and shell edges. Avoid close-ups inside the
   lens's usable focus range.
2. Capture front, rear, side, and diagonal views. Include low, level, and elevated
   angles that the submarine might encounter. Keep hands and supports out of
   most frames.
3. Vary distance so the statue appears small, medium, and large. Include the
   entire assembly in many frames, some partly cropped views, and positions near
   the image edges. Do not keep it centered in every picture.
4. Change backgrounds between sessions. Include blue backgrounds, clutter, and
   several lighting directions. Avoid making a particular desk or towel a cue
   that always accompanies Testudo.
5. Include partial obstruction and realistic movement. Keep usable difficult
   views, but inspect for accidental severe blur or blown-out orange highlights.
6. Capture negative sessions with orange/red objects, turtle-like shapes, and
   empty backgrounds. Never mark an image negative while Testudo is visible.
7. Start with about 500-1,000 varied selected images across multiple sessions,
   including roughly 10% negatives. These are initial collection targets, not
   minimums or accuracy guarantees. Review duplicates before labeling.

Dry collection is useful for the initial model and software workflow. Before
relying on underwater detection, add real images taken through the final housing
in the tank, using `--environment underwater`. Reserve underwater evaluation
sessions too. Dry images alone do not measure underwater performance.

## Label, train YOLO26n, then export NCNN

For step-by-step rectangle labeling instructions and the training program, see
[Testudo training](../../testudo_training/README.md).

The collector intentionally creates **no YOLO label files**. Positive/negative
session settings are collection notes, not bounding box annotations. In
particular, an unlabeled positive must never enter training as a background image.

Use a bounding box annotation tool to label one class, `testudo`, with class ID 0.
Choose a consistent boundary, preferably the entire turtle and pedestal if that
assembly is the mission target. Label every target instance. For negatives that
you have reviewed, create empty `.txt` labels. Export YOLO detection format, one
matching label file per image, with normalized `class x_center y_center width height`.

Assign whole capture sessions to train, validation, or test, approximately
70/15/15 by image count. Do not split neighboring frames between these groups.
Use several sessions so every group includes representative positive and negative
views. Split before applying augmentation. After annotation and splitting, arrange:

```text
testudo_dataset/
  data.yaml
  images/train/
  images/val/
  images/test/
  labels/train/
  labels/val/
  labels/test/
```

The prepared dataset is at `datasets/testudo/data.yaml` in the repo. Omit `path`
so images resolve relative to the YAML file on both Mac and Linux:

```yaml
train: images/train
val: images/val
test: images/test
names:
  0: testudo
```

On your training computer with a YOLO26-capable Ultralytics installation:

```sh
yolo detect train model=yolo26n.pt data=datasets/testudo/data.yaml imgsz=640 epochs=100 project=runs/testudo name=dry_v1
yolo detect val model=runs/testudo/dry_v1/weights/best.pt data=datasets/testudo/data.yaml split=test imgsz=640
yolo export model=runs/testudo/dry_v1/weights/best.pt format=ncnn imgsz=640
```

Train the `.pt` model first and export your **trained `best.pt`**, rather than
exporting the original pretrained weights. NCNN is the deployment format. Copy
the complete exported `best_ncnn_model` directory to the Pi. The existing repo
camera test can use it with the Pi vision environment activated:

```sh
python camera_test.py --mode yolo --model /absolute/path/to/best_ncnn_model --imgsz 640 --preview --seconds 30
```

Run that command from `raspberry_pi`. NCNN inference in this workflow runs on the
Pi CPU. It does not load the model into the IMX500's AI processor. See the existing
[camera guide](../CAMERA.md) for the Pi inference environment and NCNN setup.
If 640 is too slow, export a separate model at 320 and benchmark with `--imgsz 320`;
check accuracy on small/distant targets before choosing the smaller input.

References: [Picamera2 manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf),
[Ultralytics dataset guidance](https://docs.ultralytics.com/yolov5/tutorials/tips-for-best-training-results/),
[NCNN export](https://docs.ultralytics.com/integrations/ncnn/).

## No cameras available

First run `rpicam-hello --list-cameras` on the Pi. If it also reports no cameras,
the failure is below this capture program. On Raspberry Pi OS, install/update
`imx500-all`, `python3-picamera2`, and `rpicam-apps`, then reboot and list cameras
again. Follow the [official AI Camera setup](https://www.raspberrypi.com/documentation/accessories/ai-camera.html).

If detection still fails, shut down the Pi and disconnect power before reseating
both ribbon ends. On Pi 4, use the CSI camera connector, not the similar display
connector. Check cable orientation against the official camera installation
instructions. A connected cable alone does not establish sensor communication.

Inspect `/boot/firmware/config.txt` on current Raspberry Pi OS for a disabled
`camera_auto_detect` setting or custom camera overlays. Standard official camera
auto-detection uses `camera_auto_detect=1`. Share the relevant settings and kernel
logs before adding manual overlays or replacing the OS.

If `rpicam-hello` lists the camera but this script does not, run with the system
Python outside the virtual environment and compare the Picamera2/libcamera
installation. If the camera is listed but opening it fails with a resource-busy
error, close other camera programs. A busy camera and an empty camera list are
different failures.

## Local verification

From the repo root, without Pi hardware:

```sh
python3 -m unittest discover -s test -p 'test_dataset_capture.py'
```

These simulated-camera tests verify saving, session metadata, buffer ownership,
and failure cleanup. Actual focus, color, preview, and CSI capture still need a
run on the Raspberry Pi.
