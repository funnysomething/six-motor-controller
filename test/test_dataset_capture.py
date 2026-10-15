"""Dataset save and failure checks without Picamera2, OpenCV, or Pi hardware."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from raspberry_pi.dataset_capture import capture


class Frame:
    size = 12
    shape = (2, 2, 3)

    def __init__(self, owned=False):
        self.owned = owned

    def copy(self):
        return Frame(owned=True)


class DatasetCaptureTests(unittest.TestCase):
    def run_capture(self, root, *, failure=False, interrupted=False, negative=False):
        args = capture.parse_args([
            "--output", str(root), "--max-images", "2", "--interval", "0.001",
            "--kind", "negative" if negative else "positive",
        ])
        camera = Mock()
        camera.camera_configuration.return_value = {"main": {"format": "RGB888"}}
        request = Mock()
        request.make_array.return_value = Frame()
        request.get_metadata.return_value = {"SensorTimestamp": 123, "ExposureTime": 1000}
        camera.capture_request.return_value = request
        if interrupted:
            camera.capture_request.side_effect = KeyboardInterrupt
        factory = Mock(return_value=camera)
        factory.global_camera_info.return_value = [{"Model": "imx500"}]
        cv2 = Mock()
        cv2.IMWRITE_JPEG_QUALITY = 1

        def save(path, frame, options):
            self.assertTrue(frame.owned, "Saving must own memory after request.release()")
            request.release.assert_called()
            if failure:
                return False
            Path(path).write_bytes(b"simulated JPEG")
            return True

        cv2.imwrite.side_effect = save
        with patch.object(capture.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            if failure:
                with self.assertRaisesRegex(RuntimeError, "Could not save"):
                    capture.run(args, factory, cv2)
                result = None
            else:
                result = capture.run(args, factory, cv2)
        camera.stop.assert_called_once()
        camera.close.assert_called_once()
        session = next(root.iterdir())
        manifest = json.loads((session / "session.json").read_text())
        rows = [json.loads(line) for line in (session / "frames.jsonl").read_text().splitlines()]
        return result, session, manifest, rows

    def test_positive_saves_and_metadata_without_false_background_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            result, session, manifest, rows = self.run_capture(Path(directory))
            self.assertEqual(result, 0)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["image_count"], 2)
            self.assertEqual(manifest["environment"], "dry")
            self.assertEqual(manifest["annotation_status"], "unlabeled")
            self.assertEqual(len(list((session / "images").glob("*.jpg"))), 2)
            self.assertFalse(list(session.rglob("*.txt")))
            self.assertFalse(list(session.rglob("*.tmp*")))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["metadata"]["SensorTimestamp"], 123)
            self.assertTrue((session / rows[0]["image"]).is_file())

    def test_negative_is_recorded_but_still_requires_annotation_review(self):
        with tempfile.TemporaryDirectory() as directory:
            _, session, manifest, rows = self.run_capture(Path(directory), negative=True)
            self.assertEqual(manifest["kind"], "negative")
            self.assertTrue(all(row["kind"] == "negative" for row in rows))
            self.assertFalse(list(session.rglob("*.txt")))

    def test_failed_write_releases_camera_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            _, session, manifest, rows = self.run_capture(Path(directory), failure=True)
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["image_count"], 0)
            self.assertFalse(rows)
            self.assertFalse(list((session / "images").iterdir()))

    def test_interrupt_releases_camera_and_finalizes_session(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, manifest, rows = self.run_capture(Path(directory), interrupted=True)
            self.assertEqual(result, 130)
            self.assertEqual(manifest["status"], "interrupted")
            self.assertFalse(rows)

    def test_invalid_intervals_and_unusable_manual_mode(self):
        for arguments in (["--interval", "nan"], ["--interval", "0"],
                          ["--interval", "inf"], ["--manual"], ["--quality", "101"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    capture.parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
