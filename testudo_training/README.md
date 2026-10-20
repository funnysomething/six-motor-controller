# Train the Testudo detector

To use the IMX500 camera's AI processor, follow [the YOLO11n camera workflow](IMX500.md).
`train.py --model yolo11n.pt` trains its supported architecture. The default
YOLO26n/NCNN workflow below runs inference on the Pi CPU.

`train.py` fine-tunes pretrained YOLO26n weights using labeled images and can export
the resulting best weights to NCNN. Run training on your laptop, desktop, or a GPU
machine. The Raspberry Pi captures images and runs the deployed model; CPU training
on the Pi is possible but can be very slow. Camera detection is not needed to train
from images you already have.

## Do I have to label the images?

Yes, object detection needs a rectangle identifying where Testudo appears in each
positive image. A folder name or the capture program's positive-session setting
does not provide that rectangle. You do not need to trace the outline, paint a
mask, or label individual body parts.

You can draw the initial boxes yourself, then use a trained model to suggest boxes
for later images. Review and correct every suggestion. A pretrained generic model
does not already know your specific statue, and a single reference photo is not a
useful training dataset by itself.

## Label with CVAT

Use [CVAT](https://www.cvat.ai/) on your computer. Its
[rectangle instructions](https://docs.cvat.ai/docs/annotation/manual-annotation/shapes/annotation-with-rectangles/)
and [Ultralytics export documentation](https://docs.cvat.ai/docs/dataset_management/formats/format-yolo-ultralytics/)
cover the annotation and output format.

1. Select varied images from your capture sessions. Put entire sessions into
   training, validation, and test groups, about 70/15/15 by image count. Keep
   neighboring frames together. Do this before uploading, or preserve the session
   groups when organizing your labeled export afterward.
2. Create a project with one label, `testudo`. Create image tasks and upload your
   selected JPEGs. Keep the same label definition across tasks. Use separate
   tasks for session groups so you can preserve their train/validation/test assignment.
3. Open a task's annotation job. Choose the rectangle tool, the `testudo` label,
   and shape mode for individual images. Draw a box from the upper-left to the
   lower-right of the target. Include the turtle's head, feet, shell, and pedestal
   if the whole assembly is the detection target. Use that choice consistently.
4. Fit the box tightly to the target's outer edges. Do not include a large margin
   of background. For a statue cut off by the frame edge, end the box at that
   edge. For partial obstruction, use a consistent visible-extent box policy and
   avoid images where you cannot confidently identify the target.
5. Move to the next image and repeat. Label every visible Testudo instance. Do not
   label orange decorations as Testudo. Leave confirmed negative images without
   rectangles. Do not treat an image you skipped reviewing as a negative.
6. Save and review your work. Export using **Ultralytics YOLO Detection**, with
   images included. Do not choose classification, segmentation, or oriented boxes.
   Unzip the export and preserve image/label filename pairs. CVAT may export one
   subset per task, so arrange the exported subsets into the layout below and
   update `data.yaml` before training.

For an image called `statue_001.jpg`, its corresponding label is `statue_001.txt`.
The annotation tool writes lines such as:

```text
0 0.50 0.55 0.40 0.60
```

The numbers mean class ID 0, box center at 50% of image width and 55% of height,
box width 40% of image width, and box height 60% of image height. Those are example
coordinates, not labels for your photos. You should draw boxes in the tool rather
than calculate these numbers yourself. See the
[YOLO detection format](https://docs.ultralytics.com/datasets/detect/).

Use an empty `.txt` file for each reviewed image with no target. Some exporters
omit negative label files, which Ultralytics also supports. Review these cases:
missing labels on positive images would silently teach the wrong behavior.

## Arrange the exported dataset

```text
testudo_dataset/
  data.yaml
  images/train/    # training JPEGs
  images/val/      # validation JPEGs
  images/test/     # held-out test JPEGs
  labels/train/   # matching training .txt files
  labels/val/     # matching validation .txt files
  labels/test/    # matching test .txt files
```

Copy `data.example.yaml` to this dataset directory as `data.yaml`, and omit
`path` so directories resolve relative to the YAML file on each machine. The only class must be
`0: testudo`. Keep all three groups representative, with different views, sizes,
backgrounds, and negatives. The test split is optional unless you request `--test`.

The script also accepts a valid exported Ultralytics YAML using image-list files
instead of image directories. Check that its `path`, `train`, and `val` entries
resolve to the extracted local images. The script checks dataset paths and the
class definition; it cannot tell whether you drew the boxes correctly. Inspect
the training run's batch visualization images to confirm boxes match the statue.

## Install and train

From this repository's root on the training computer:

```sh
python3 -m venv testudo_training/.venv
source testudo_training/.venv/bin/activate
python -m pip install -r testudo_training/requirements.txt
python testudo_training/train.py --data datasets/testudo/data.yaml
```

The default is 100 epochs at image size 640, batch size 8, on the CPU. The first
run downloads pretrained `yolo26n.pt`. This initializes the detector before it
learns your `testudo` class. Use `--device mps` on a supported Apple Silicon Mac,
or `--device 0` for a supported NVIDIA GPU. Reduce `--batch` if memory runs out.
Use `--workers 0` if your environment has multiprocessing problems.

To also evaluate the held-out test split and export NCNN:

```sh
python testudo_training/train.py --data datasets/testudo/data.yaml --device mps --test --export-ncnn
```

Replace `mps` with your device. Leave out `--test` until a separate labeled test
split exists. Tune using validation results, then evaluate the test split after
choosing the model. Inspect misses and false positives on fresh images too.

Runs are saved under `testudo_training/runs/`, with new run names to preserve
earlier results. The script prints the actual `weights/best.pt` path. Keep datasets,
trained weights, model exports, and capture sessions in this repository and in Git.
Use an in-repository directory if selecting a custom `--project`. Python virtual
environments and disposable caches remain ignored. See [dataset locations](../datasets/README.md).

NCNN export uses your trained `best.pt`, runs on CPU, and may install export
dependencies. If export fails, your trained weights remain available. You can
retry export independently from the activated training environment:

```sh
yolo export model=/absolute/path/to/weights/best.pt format=ncnn imgsz=640
```

Copy the entire resulting `best_ncnn_model` directory to the Pi. With the existing
Pi inference environment activated, run from the repository root on the Pi:

```sh
python raspberry_pi/camera_test.py --mode yolo --model /absolute/path/to/best_ncnn_model --imgsz 640 --preview --seconds 30
```

Use the same input size at export and inference. `--export-imgsz 320` can create
a smaller deployment input for speed, but evaluate detection at that size too.
NCNN runs on the Pi CPU in this repo's camera program. It does not install the
model on the IMX500's AI processor. See [NCNN export guidance](https://docs.ultralytics.com/integrations/ncnn/).

Dry statue images let you start the workflow. Add labeled images from the actual
underwater camera and housing before judging tank performance.
