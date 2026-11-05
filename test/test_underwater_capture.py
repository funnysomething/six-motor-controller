"""Recorder lifecycle and clean-stream separation without CSI hardware."""

import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen
import threading

import numpy as np

from raspberry_pi import underwater_capture as capture
from raspberry_pi.hoop_vision import replay


class CaptureTests(unittest.TestCase):
    def test_browser_status_withholds_expired_acceptance(self):
        preview = capture.Preview()
        preview.publish(b'jpeg', {'decision': 'accepted', 'sensor_age_s': .25})
        server = capture.ThreadingHTTPServer(('127.0.0.1', 0), preview.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/status', timeout=2) as response:
                status = json.load(response)
            self.assertEqual(status['decision'], 'unknown')
            self.assertTrue(status['preview_stale'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_reject_invalid_rates(self):
        for value in ('0', 'nan', 'inf', '-1'):
            with self.subTest(value=value), self.assertRaises(SystemExit), patch('sys.stderr', io.StringIO()):
                capture.parse_args(['--output', 'test', '--fps', value])

    def session(self, directory, hoop=False, encoder_error=False, capture_error=False):
        args = capture.parse_args(['--output', str(Path(directory) / 'session'), '--seconds', '.06',
                                   '--process-fps', '100'] + (['--hoop'] if hoop else []))
        camera = Mock()
        camera.camera_properties = {'Model': 'simulated'}
        camera.camera_configuration.return_value = {'main': 'simulated'}
        # Simulate a proper planar YUV420 frame from the low-resolution stream.
        yuv = np.full((540, 640), 128, dtype=np.uint8)
        request = Mock()
        request.make_array.return_value = yuv
        clock = getattr(time, 'CLOCK_BOOTTIME', time.CLOCK_MONOTONIC)
        request.get_metadata.side_effect = lambda: {'SensorTimestamp': int(time.clock_gettime(clock) * 1e9)}
        camera.capture_request.side_effect = RuntimeError('CSI failure') if capture_error else lambda: request
        output = Mock()
        output.ptsoutput = io.StringIO()
        output_factory = Mock(return_value=output)
        if encoder_error:
            camera.start_recording.side_effect = lambda *_: output.error_callback(RuntimeError('disk full'))
        processor = Mock()
        # The processor can change its preview pixels without changing the clean main stream.
        def process(image, *unused):
            image[:] = 255
            observation = Mock()
            observation.decision = 'unknown'
            observation.accepted_target = None
            observation.to_dict.return_value = {'decision': 'unknown'}
            return observation
        processor.process.side_effect = process
        server = Mock()
        server.serve_forever.return_value = None
        modules = {'picamera2': SimpleNamespace(Picamera2=Mock(return_value=camera)),
                   'picamera2.encoders': SimpleNamespace(H264Encoder=Mock()),
                   'picamera2.outputs': SimpleNamespace(PyavOutput=output_factory)}
        with patch.dict(sys.modules, modules), patch.object(capture, 'ThreadingHTTPServer', return_value=server), \
                patch.object(capture.signal, 'signal'), patch.object(capture, 'HoopProcessor', return_value=processor), \
                patch.object(replay, 'overlay', side_effect=lambda image, *a, **kw: image):
            error = None
            try:
                capture.run(args)
            except RuntimeError as caught:
                error = caught
        summary = json.loads((args.output / 'session.json').read_text())
        return summary, camera, request, yuv, output_factory, server, error

    def test_records_clean_main_while_processing_separate_low_resolution_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            summary, camera, request, yuv, factory, server, error = self.session(directory, hoop=True)
            self.assertIsNone(error)
            self.assertGreater(summary['processed_frames'], 0)
            self.assertEqual(summary['status'], 'completed')
            self.assertTrue(np.all(yuv == 128))
            request.release.assert_called()
            camera.stop_recording.assert_called_once()
            camera.close.assert_called_once()
            self.assertTrue(factory.call_args.args[0].endswith('clean.mp4'))
            server.shutdown.assert_called_once()

    def test_encoder_failure_stops_and_marks_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            summary, camera, _, _, _, _, error = self.session(directory, encoder_error=True)
            self.assertIn('disk full', str(error))
            self.assertEqual(summary['status'], 'failed')
            camera.stop_recording.assert_called_once()
            camera.close.assert_called_once()

    def test_camera_failure_still_finalizes_video(self):
        with tempfile.TemporaryDirectory() as directory:
            summary, camera, _, _, _, _, error = self.session(directory, capture_error=True)
            self.assertIn('CSI failure', str(error))
            self.assertEqual(summary['status'], 'failed')
            camera.stop_recording.assert_called_once()
            camera.close.assert_called_once()

    def test_real_tracker_accepts_ring_then_withholds_on_blank(self):
        import cv2
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.ellipse(image, (320, 240), (130, 100), 0, 0, 360, (255, 255, 255), 16)
        processor = capture.HoopProcessor()
        processor.process(image, 0, lambda: .01)
        observation = processor.process(image, .1, lambda: .01)
        self.assertIsNotNone(observation.accepted_target)
        observation = processor.process(np.zeros_like(image), .2, lambda: .01)
        self.assertIsNone(observation.accepted_target)


if __name__ == '__main__':
    unittest.main()
