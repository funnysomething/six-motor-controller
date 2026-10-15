"""Check training/export orchestration without downloading weights or using Torch."""

import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from testudo_training import train


class TestudoTrainingTests(unittest.TestCase):
    def test_export_and_test_use_trained_best_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data.yaml"
            data.write_text("names: {0: testudo}\n")
            best = root / "weights" / "best.pt"
            best.parent.mkdir()
            best.write_bytes(b"simulated weights")
            args = train.parse_args(["--data", str(data), "--model", "yolo11n.pt", "--test", "--export-ncnn",
                                     "--export-imgsz", "320"])
            initial, trained = Mock(), Mock()
            initial.trainer = SimpleNamespace(best=best)
            factory = Mock(side_effect=[initial, trained])
            check = Mock(return_value={"names": {0: "testudo"}, "test": "images/test"})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(train.run(args, factory, check), 0)
            check.assert_called_once_with(str(data.resolve()), autodownload=False)
            self.assertEqual(factory.call_args_list[0].args[0], "yolo11n.pt")
            self.assertEqual(factory.call_args_list[1].args[0], str(best))
            trained.export.assert_called_once_with(format="ncnn", imgsz=320, device="cpu")
            self.assertEqual(trained.val.call_args.kwargs["split"], "test")
            self.assertFalse(initial.export.called)

    def test_wrong_class_and_missing_test_stop_before_training(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data.yaml"
            data.touch()
            args = train.parse_args(["--data", str(data), "--test"])
            for dataset in ({"names": {0: "turtle"}, "test": "test"},
                            {"names": {0: "testudo"}, "test": None}):
                with self.subTest(dataset=dataset):
                    factory = Mock()
                    with self.assertRaises(ValueError):
                        train.run(args, factory, Mock(return_value=dataset))
                    factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
