"""Check model validation and on-camera dispatch without hardware or Sony modlib."""

import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from raspberry_pi import imx500_detect
from testudo_training import export_imx500


class Imx500Tests(unittest.TestCase):
    def test_cpu_model_directory_is_rejected_before_camera_access(self):
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                imx500_detect.parse_args(["--model-dir", directory, "--headless"])

    def test_custom_camera_model_passes_tensors_to_sony_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "packerOut.zip").write_bytes(b"simulated export")
            (root / "labels.txt").write_text("testudo\n")
            args = imx500_detect.parse_args(["--model-dir", directory, "--headless"])
            captures = {}

            class Model:
                def __init__(self, **kwargs):
                    captures.update(kwargs)

            class Scores:
                def __ge__(self, value):
                    return "confidence-mask"

            detections = MagicMock()
            detections.confidence = Scores()
            selected = [(None, .9, 0, None)]
            detections.__getitem__.return_value = selected
            device = MagicMock()
            device.__enter__.return_value = [SimpleNamespace(detections=detections)]
            factory = Mock(return_value=device)
            postprocess = Mock(return_value="decoded")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(imx500_detect.run(
                    args, factory, Model, SimpleNamespace(CONVERTED="converted"),
                    SimpleNamespace(RGB="rgb"), postprocess, Mock()), 0)
            deployed = device.deploy.call_args.args[0]
            self.assertEqual(deployed.post_process(["tensors"]), "decoded")
            postprocess.assert_called_once_with(["tensors"])
            self.assertEqual(captures["model_file"], str(root.resolve() / "packerOut.zip"))
            self.assertEqual(captures["model_type"], "converted")
            factory.assert_called_once_with(frame_rate=16, timeout=60, headless=True)
            device.__exit__.assert_called_once()

    def test_mac_export_fails_with_linux_guidance_before_loading_ultralytics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights = root / "best.pt"
            data = root / "data.yaml"
            weights.touch()
            data.touch()
            output = io.StringIO()
            with patch.object(export_imx500.platform, "system", return_value="Darwin"), \
                    contextlib.redirect_stderr(output):
                code = export_imx500.main(["--weights", str(weights), "--data", str(data)])
            self.assertEqual(code, 1)
            self.assertIn("Linux and Java 17+", output.getvalue())


if __name__ == "__main__":
    unittest.main()
