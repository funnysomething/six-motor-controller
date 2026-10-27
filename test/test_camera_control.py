"""Camera backend, process isolation, frame transport and cleanup without Pi hardware."""

import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from raspberry_pi import camera_control as camera


def export(root, backend):
    directory = root / ("best_imx_model" if backend == "imx500" else "best_ncnn_model")
    directory.mkdir(exist_ok=True)
    files = (("packerOut.zip", "labels.txt") if backend == "imx500" else
             ("model.ncnn.param", "model.ncnn.bin", "metadata.yaml"))
    for filename in files:
        (directory / filename).write_text("testudo\n")
    return directory


class CameraTests(unittest.TestCase):
    def test_auto_uses_trained_pt_and_prefers_imx_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights = root / "best.pt"
            weights.touch()
            with patch.object(camera, "WEIGHTS", root):
                self.assertEqual(camera.CameraConfig().resolve(), ("pytorch", weights))
                imx = export(root, "imx500")
                self.assertEqual(camera.CameraConfig().resolve(), ("imx500", imx))

    def test_explicit_imx_does_not_fall_back_to_cpu_or_generic_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export(root, "ncnn")
            with patch.object(camera, "WEIGHTS", root):
                with self.assertRaisesRegex(ValueError, "packerOut.zip"):
                    camera.CameraConfig("imx500").resolve()

    def test_custom_model_and_interpreter_are_used_in_worker_command(self):
        with tempfile.TemporaryDirectory() as directory:
            model = export(Path(directory), "ncnn")
            command, backend = camera.CameraConfig("auto", model, "custom python", .4, 12).command()
            self.assertEqual(backend, "ncnn")
            self.assertEqual(command[0], "custom python")
            self.assertEqual(command[command.index("--model") + 1], str(model.resolve()))
            self.assertEqual(command[-4:], ["--conf", "0.4", "--fps", "12"])

    def test_user_pt_path_selects_cpu_even_when_an_imx_export_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export(root, "imx500")
            weights = root / "best.pt"
            weights.touch()
            with patch.object(camera, "WEIGHTS", root):
                self.assertEqual(camera.CameraConfig(model=weights).resolve(), ("pytorch", weights.resolve()))

    def test_pt_runner_loads_exact_checkpoint_and_closes_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            weights = Path(directory) / "best.pt"
            weights.touch()
            model = Mock()
            model.names = {0: "testudo"}
            result = Mock()
            result.boxes.conf.tolist.return_value = []
            model.predict.return_value = [result]
            yolo = Mock(return_value=model)
            capture = Mock()
            publish = Mock(side_effect=KeyboardInterrupt)
            with patch.dict("sys.modules", {"cv2": Mock(), "torch": Mock(),
                    "picamera2": SimpleNamespace(Picamera2=Mock(return_value=capture)),
                    "ultralytics": SimpleNamespace(YOLO=yolo)}):
                with self.assertRaises(KeyboardInterrupt):
                    camera.run_cpu(SimpleNamespace(model=weights, backend="pytorch", conf=.4, fps=16), publish)
            yolo.assert_called_once_with(str(weights), task="detect")
            self.assertEqual(model.predict.call_args.kwargs["device"], "cpu")
            self.assertEqual(publish.call_args.args[2], "YOLO11n PyTorch / Pi CPU")
            capture.stop.assert_called_once()
            capture.close.assert_called_once()

    def test_main_stops_motors_before_waiting_for_camera_cleanup(self):
        from raspberry_pi import motor_control as motors
        events = []
        controller = Mock()
        controller.fault = None
        controller.close.side_effect = lambda: events.append("motors stopped")
        session = Mock()
        session.close.side_effect = lambda: events.append("camera cleanup")
        with patch.dict("sys.modules", {"gpiozero": SimpleNamespace(DigitalOutputDevice=Mock()),
                                         "smbus2": SimpleNamespace(SMBus=MagicMock(), i2c_msg=Mock())}), \
                patch("sys.argv", ["motor_control.py", "--gui", "--no-imu"]), \
                patch.object(motors.signal, "signal"), patch.object(motors.signal, "SIGHUP", 1, create=True), \
                patch.object(motors, "make_gpio_factory", return_value=Mock()), \
                patch.object(motors, "MotorController", return_value=controller), \
                patch.object(motors, "run_gui") as gui, \
                patch.object(camera, "CameraSession", return_value=session):
            self.assertEqual(motors.main(), 0)
        gui.assert_called_once_with(controller, camera=session)
        self.assertEqual(events, ["motors stopped", "camera cleanup"])

    def test_invalid_config_and_missing_export_do_not_spawn_a_process(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(camera.subprocess, "Popen") as popen:
            for config in (camera.CameraConfig("imx500", Path(directory)),
                           camera.CameraConfig(confidence=float("nan")), camera.CameraConfig(fps=0)):
                session = camera.CameraSession(config)
                session.start()
                self.assertIsNotNone(session.error)
                self.assertFalse(session.running)
            popen.assert_not_called()

    def test_reader_keeps_only_latest_frame_and_bounded_logs_and_reports_crash(self):
        session = camera.CameraSession(Mock())
        lines = [f"native log {index}\n" for index in range(20)]
        for index in range(3):
            message = dict(type="frame", image=base64.b64encode(bytes([index])).decode(), status="streaming")
            lines.append(camera.MESSAGE_PREFIX + json.dumps(message) + "\n")
        process = Mock(stdout=io.StringIO("".join(lines)))
        process.wait.return_value = -4
        session._read(process)
        self.assertEqual(session.latest[1]["image"], b"\x02")
        self.assertEqual(len(session.logs), 6)
        self.assertIn("(-4)", session.error)

    def test_stop_is_nonblocking_and_poll_kills_a_stuck_worker(self):
        session = camera.CameraSession(Mock())
        session.process = Mock()
        session.process.poll.return_value = None
        with patch.object(camera.time, "monotonic", return_value=10):
            session.stop()
        session.process.terminate.assert_called_once()
        session.process.wait.assert_not_called()
        with patch.object(camera.time, "monotonic", return_value=14):
            session.poll()
            session.poll()
        session.process.kill.assert_called_once()
        session.process.poll.return_value = 0
        session.close()

    def test_reader_shows_loading_stages_before_first_frame(self):
        session = camera.CameraSession(Mock())
        stages = ["Preparing firmware", "Uploading firmware", "Waiting for detections"]
        observed = []

        def messages():
            for stage in stages:
                yield camera.MESSAGE_PREFIX + json.dumps(dict(type="status", text=stage)) + "\n"
                observed.append(session.status)

        process = Mock()
        process.stdout = MagicMock()
        process.stdout.__iter__.return_value = messages()
        process.wait.return_value = 0
        session._read(process)
        self.assertEqual(observed, stages)

    def test_real_worker_process_can_fail_without_touching_motor_controller(self):
        config = Mock()
        config.command.return_value = ([sys.executable, "-c", "raise RuntimeError('camera unplugged')"], "ncnn")
        session = camera.CameraSession(config)
        self.addCleanup(session.close)
        session.start()
        deadline = time.monotonic() + 5
        while session.running and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(session.running)
        self.assertIn("camera unplugged", session.error)

    def test_ncnn_runner_uses_export_size_and_releases_camera_on_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "metadata.yaml").write_text("metadata")
            cv2 = Mock()
            model = Mock()
            model.names = {0: "testudo"}
            result = Mock()
            result.boxes.conf.tolist.return_value = [.9]
            model.predict.return_value = [result]
            capture = Mock()
            publish = Mock(side_effect=KeyboardInterrupt)
            yolo = Mock(return_value=model)
            with patch.dict("sys.modules", {"cv2": cv2, "torch": Mock(),
                    "yaml": SimpleNamespace(safe_load=lambda text: {"names": {0: "testudo"}, "imgsz": [640, 640]}),
                    "picamera2": SimpleNamespace(Picamera2=Mock(return_value=capture)),
                    "ultralytics": SimpleNamespace(YOLO=yolo)}):
                with self.assertRaises(KeyboardInterrupt):
                    camera.run_cpu(SimpleNamespace(model=root, conf=.5, fps=16, backend="ncnn"), publish)
            yolo.assert_called_once_with(str(root), task="detect")
            self.assertEqual(model.predict.call_args.kwargs["imgsz"], [640, 640])
            publish.assert_called_once_with(result.plot.return_value, ["Testudo 90%"], "NCNN / Pi CPU", cv2)
            capture.stop.assert_called_once()
            capture.close.assert_called_once()

    def test_imx_runner_reuses_model_and_streams_until_stopped_without_opencv_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = export(Path(directory), "imx500")
            model_args = {}

            class Model:
                def __init__(self, **kwargs):
                    model_args.update(kwargs)

            detections = MagicMock()
            detections.confidence.__ge__.return_value = "mask"
            detections.__getitem__.return_value = [(None, .9, 0, None)]
            display = Mock(side_effect=AssertionError("GUI must not open a second preview"))
            frame = SimpleNamespace(detections=detections, image="rgb pixels", color_format="RGB", display=display)
            waiting = SimpleNamespace(detections=None, image="first pixels", color_format="RGB", display=display)
            device = MagicMock()
            device.__enter__.return_value = [waiting, frame]
            factory = Mock(return_value=device)
            annotator = Mock()
            cv2 = Mock()
            postprocess = Mock()
            publish = Mock(side_effect=[None, KeyboardInterrupt])
            status = Mock()
            with patch.dict("sys.modules", {"cv2": cv2, "modlib": Mock(),
                    "modlib.apps": SimpleNamespace(Annotator=Mock(return_value=annotator)),
                    "modlib.devices": SimpleNamespace(AiCamera=factory),
                    "modlib.models": SimpleNamespace(Model=Model, COLOR_FORMAT=SimpleNamespace(RGB="RGB"),
                                                     MODEL_TYPE=SimpleNamespace(CONVERTED="converted")),
                    "modlib.models.post_processors": SimpleNamespace(pp_od_yolo_ultralytics=postprocess)}), \
                    patch.object(camera.time, "monotonic", side_effect=[0, 300, 600]), \
                    self.assertRaises(KeyboardInterrupt):
                camera.run_imx500(SimpleNamespace(model=root, fps=16, conf=.5), publish, status=status)
            self.assertEqual(model_args["model_file"], str(root / "packerOut.zip"))
            factory.assert_called_once_with(frame_rate=16, timeout=None, headless=False)
            device.deploy.assert_called_once_with(device.deploy.call_args.args[0], overwrite=False)
            deployed = device.deploy.call_args.args[0]
            deployed.post_process(["tensors"])
            postprocess.assert_called_once_with(["tensors"])
            annotator.annotate_boxes.assert_called_once_with(frame, [(None, .9, 0, None)], labels=["testudo 0.90"])
            self.assertEqual(publish.call_count, 2)
            self.assertEqual(publish.call_args_list[0].args[1], [])
            self.assertEqual(publish.call_args.args, (cv2.cvtColor.return_value, ["testudo 0.90"], "IMX500 / camera AI", cv2))
            self.assertIn("firmware loaded", status.call_args.args[0])
            display.assert_not_called()
            device.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
