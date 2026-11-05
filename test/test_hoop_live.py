"""Live transport, freshness and camera identity tests; no physical camera opened."""
import contextlib
import base64
import io
import json
import struct
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

import cv2
import numpy as np

from raspberry_pi.hoop_vision.live import LiveState, VisionWorker, main, start_preview
from raspberry_pi.hoop_vision.native_capture import (
    BridgeCapture, CameraPacket, LatestSlot, read_packet, select_camera)
from raspberry_pi.hoop_vision.tracker import HoopTracker, TrackerConfig
from raspberry_pi.hoop_vision.types import Observation
from test.test_hoop_ellipse_tracking import qualified
from test.test_hoop_tracking import obs


DEVICE = dict(id="iphone-exact-id", name="Test iPhone")


def packet(pts=10., received=100., jpeg=b"jpeg", **changes):
    metadata = dict(sequence=1, source_time_s=pts, device_id=DEVICE["id"], width=640,
                    height=480, jpeg_bytes=len(jpeg), timestamp_provenance="avfoundation_pts")
    metadata.update(changes)
    return CameraPacket(metadata, jpeg, received)


def wire(item):
    header = json.dumps(item.metadata).encode()
    return struct.pack("!I", len(header)) + header + item.jpeg


class TransportTests(unittest.TestCase):
    def test_read_packet_preserves_real_pts_and_receipt_clock(self):
        result = read_packet(io.BytesIO(wire(packet())), clock=lambda:123.)
        self.assertEqual(result.metadata["source_time_s"], 10.)
        self.assertEqual(result.received_at, 123.)
        self.assertEqual(result.jpeg, b"jpeg")
        self.assertIsNone(read_packet(io.BytesIO()))

    def test_fragmented_reads_are_reassembled(self):
        class Fragmented(io.BytesIO):
            def read(self, count=-1):
                return super().read(min(count, 3))
        self.assertEqual(read_packet(Fragmented(wire(packet()))).jpeg, b"jpeg")

    def test_incomplete_packet_is_not_a_frame(self):
        for data in (b"\0", wire(packet())[:-1]):
            with self.assertRaises(EOFError):
                read_packet(io.BytesIO(data))

    def test_invalid_packet_metadata_is_rejected(self):
        for changes in (dict(source_time_s=float("nan")), dict(width=True),
                        dict(jpeg_bytes=20*1024*1024), dict(device_id=""),
                        dict(timestamp_provenance="fabricated"), dict(sequence=0)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read_packet(io.BytesIO(wire(packet(**changes))))
        for data in (struct.pack("!I", 20000), struct.pack("!I", 2)+b"[]"):
            with self.assertRaises(ValueError):
                read_packet(io.BytesIO(data))

    def test_selection_requires_one_explicit_identity(self):
        mac = dict(id="mac", name="FaceTime HD Camera")
        self.assertEqual(select_camera([mac, DEVICE], name="IPHONE"), DEVICE)
        self.assertEqual(select_camera([mac, DEVICE], device_id=DEVICE["id"]), DEVICE)
        for devices, selectors in (([mac], dict(name="iPhone")), ([mac], {}),
                                   ([DEVICE, dict(id="other", name="Other iPhone")], dict(name="iPhone")),
                                   ([DEVICE], dict(name="iPhone", device_id=DEVICE["id"]))):
            with self.assertRaises(ValueError):
                select_camera(devices, **selectors)

    def test_latest_slot_discards_backlog_and_clears_on_disconnect(self):
        slot = LatestSlot()
        for i in range(100):
            slot.publish(i)
        version, value = slot.take(timeout=0)
        self.assertEqual((version, value), (100, 99))
        self.assertEqual(slot.take(version, timeout=0), (version, None))
        slot.close("Disconnected")
        slot.publish(101)
        self.assertIsNone(slot.take(timeout=0)[1])

    def test_wrong_device_stops_capture_without_opening_a_fallback(self):
        process = MagicMock(stdout=io.BytesIO(wire(packet(device_id="mac"))), stderr=io.BytesIO())
        process.poll.return_value = None
        with patch("raspberry_pi.hoop_vision.native_capture.subprocess.Popen", return_value=process) as launch:
            capture = BridgeCapture("/fake/bridge", DEVICE, 10)
            capture.reader.join(timeout=2)
            self.assertTrue(capture.slot.closed)
            self.assertIn("identity changed", capture.slot.error)
            self.assertIsNone(capture.slot.value)
            self.assertEqual(launch.call_count, 1)
            self.assertIn(DEVICE["id"], launch.call_args[0][0])
            process.terminate.assert_called()
            capture.close()

    def test_capture_callback_delivers_frames_without_a_vision_consumer(self):
        delivered=[]
        data=wire(packet(sequence=1))+wire(packet(sequence=2))
        process=MagicMock(stdout=io.BytesIO(data),stderr=io.BytesIO())
        process.poll.return_value=None
        with patch('raspberry_pi.hoop_vision.native_capture.subprocess.Popen',return_value=process):
            capture=BridgeCapture('/fake/bridge',DEVICE,10,on_frame=delivered.append,max_dimension=960)
            capture.reader.join(timeout=2)
            self.assertEqual([p.metadata['sequence'] for p in delivered],[1,2])
            capture.close()


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.05
        self.state = LiveState(DEVICE, clock=lambda:self.now)
        self.observation = Observation((640,480), decision="accepted", accepted_target={"track_id":1})
        self.state.publish(packet(), self.observation, b"annotated jpeg", 25)

    def test_measurement_expiry_keeps_only_unannotated_preview(self):
        self.assertEqual(self.state.snapshot()["decision"], "accepted")
        self.assertEqual(base64.b64decode(self.state.snapshot()['image']),b'annotated jpeg')
        self.now = 100.201
        expired = self.state.snapshot()
        self.assertEqual(expired["decision"], "unknown")
        self.assertEqual(base64.b64decode(expired['image']),b'jpeg')
        self.assertIsNone(expired["observation"])
        self.assertFalse(expired['detection_fresh'])
        self.assertTrue(expired['available'])
        self.assertEqual(expired['remaining_ms'],0)

    def test_preview_expires_if_camera_really_stops_delivering(self):
        self.now=101.001
        expired=self.state.snapshot()
        self.assertFalse(expired['available'])
        self.assertIsNone(expired['image'])
        self.assertIsNone(expired['observation'])
        self.assertEqual(expired['preview_state'],'stalled')

    def test_preview_updates_even_when_vision_has_not_finished(self):
        self.now=100.6
        self.state.receive(packet(received=100.58,sequence=2,jpeg=b'new raw frame'))
        status=self.state.snapshot()
        self.assertEqual(base64.b64decode(status['image']),b'new raw frame')
        self.assertIsNone(status['observation'])
        self.assertEqual(status['decision'],'unknown')
        self.assertEqual(status['capture_sequence'],2)

    def test_late_vision_result_does_not_roll_capture_back(self):
        self.now=100.5
        self.state.receive(packet(received=100.49,sequence=2,jpeg=b'latest raw'))
        self.state.publish(packet(),self.observation,b'old annotation',300)
        status=self.state.snapshot()
        self.assertEqual(status['capture_sequence'],2)
        self.assertEqual(base64.b64decode(status['image']),b'latest raw')
        self.assertIsNone(status['observation'])

    def test_repeated_polls_do_not_extend_the_lifetime_of_old_pixels(self):
        self.now=100.8
        first=self.state.snapshot()
        self.now=100.9
        second=self.state.snapshot()
        self.assertLess(second['preview_remaining_ms'],first['preview_remaining_ms'])
        self.now=101.01
        self.assertIsNone(self.state.snapshot()['image'])

    def test_short_delivery_gaps_do_not_blank_preview_or_serve_stale_detections(self):
        arrivals=iter([100.,100.1,100.4,100.9])
        next_arrival=next(arrivals,None)
        sequence=0
        for step in range(96):
            self.now=100.+step*.02
            while next_arrival is not None and next_arrival<=self.now+1e-9:
                sequence+=1
                self.state.publish(packet(received=next_arrival,sequence=sequence),self.observation,b'green ellipse',25)
                next_arrival=next(arrivals,None)
            status=self.state.snapshot()
            self.assertIsNotNone(status['image'])
            if not status['detection_fresh']:
                self.assertIsNone(status['observation'])
                self.assertEqual(base64.b64decode(status['image']),b'jpeg')
        self.now=102.
        self.assertIsNone(self.state.snapshot()['image'])

    def test_old_measurement_skip_does_not_remove_latest_capture(self):
        self.state.clear_measurement('Vision skipped an old frame')
        self.assertTrue(self.state.snapshot()['available'])
        self.assertIsNone(self.state.snapshot()['observation'])

    def test_invalid_preview_timeouts_are_rejected(self):
        for timeout in (.1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):LiveState(DEVICE,preview_timeout=timeout)

    def test_disconnect_cannot_be_undone_by_inflight_processing(self):
        self.state.invalidate("Camera disconnected", terminal=True)
        self.state.publish(packet(), self.observation, b"old jpeg", 10)
        self.assertFalse(self.state.snapshot()["available"])

    def test_http_status_is_local_token_scoped_and_never_cached(self):
        server, url = start_preview(self.state)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            with urlopen(url + "status", timeout=2) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.load(response)["decision"], "accepted")
            self.now += 1
            with urlopen(url + "status", timeout=2) as response:
                self.assertIsNone(json.load(response)["image"])
            with self.assertRaises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{server.server_port}/status", timeout=2)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


class LiveVisionTests(unittest.TestCase):
    def test_native_pts_can_confirm_and_duplicate_pts_withholds_output(self):
        tracker = HoopTracker(TrackerConfig.ellipse())
        tracker.update(obs(qualified()), 10., "avfoundation_pts")
        self.assertEqual(tracker.update(obs(qualified()), 10.11, "avfoundation_pts").decision, "accepted")
        self.assertEqual(tracker.update(obs(qualified()), 10.11, "avfoundation_pts").decision, "unknown")
        self.assertEqual(tracker.update(obs(qualified()), 10.2, "wall_clock_guess").decision, "unknown")

    def test_full_jpeg_ellipse_tracking_pipeline_and_no_motor_pose(self):
        image = np.full((480,640,3), 35, np.uint8)
        cv2.ellipse(image, ((320,240),(240,300),20), (245,245,245), 20)
        ok, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        capture = SimpleNamespace(device=DEVICE, slot=LatestSlot())
        state = LiveState(DEVICE, clock=lambda:100.)
        worker = VisionWorker(capture, state)
        # Freeze host time so this verifies detection, not test-machine speed.
        with patch("raspberry_pi.hoop_vision.live.time.monotonic", return_value=100.):
            for i in range(3):
                worker.process(packet(pts=10+i*.11, sequence=i+1, jpeg=jpeg.tobytes()))
        result = state.snapshot()
        self.assertEqual(result["decision"], "accepted")
        self.assertIsNotNone(result["observation"]["accepted_target"]["ellipse"])
        self.assertFalse(result["observation"]["pose_valid"])
        self.assertFalse(result["motor_output"])
        self.assertEqual(result["capture_to_host_latency"], "unmeasured")
        view=cv2.imdecode(np.frombuffer(base64.b64decode(result['image']),np.uint8),cv2.IMREAD_COLOR)
        self.assertEqual(view.shape[:2],image.shape[:2])

    def test_bad_jpeg_stops_worker_and_clears_previous_result(self):
        capture = SimpleNamespace(device=DEVICE, slot=LatestSlot())
        capture.slot.publish(packet(received=time.monotonic()))
        state = LiveState(DEVICE)
        worker = VisionWorker(capture, state)
        worker.thread.start()
        worker.thread.join(timeout=2)
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(state.snapshot()["available"])
        self.assertIn("JPEG", state.snapshot()["reason"])

    def test_unplug_clears_preview_without_another_frame(self):
        capture = SimpleNamespace(device=DEVICE, slot=LatestSlot())
        state = LiveState(DEVICE)
        state.publish(packet(received=time.monotonic()), Observation((640,480)), b"image", 1)
        worker = VisionWorker(capture, state)
        capture.slot.close("Unplugged")
        worker.thread.start()
        worker.thread.join(timeout=2)
        self.assertEqual(state.snapshot()["reason"], "Unplugged")
        self.assertIsNone(state.snapshot()["image"])

    def test_invalid_cli_arguments_do_not_discover_or_open_a_camera(self):
        with patch("raspberry_pi.hoop_vision.live.build_bridge") as build:
            for arguments in ([], ["--camera-name","iPhone","--fps","nan"],
                              ["--camera-name","iPhone","--seconds","-1"]):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(arguments)
            build.assert_not_called()

    def test_invalid_capture_and_preview_limits_do_not_access_a_camera(self):
        with patch('raspberry_pi.hoop_vision.live.build_bridge') as build:
            for option,value in (('--preview-timeout','nan'),('--preview-timeout','0.1'),
                                 ('--capture-max-dimension','1')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['--camera-name','iPhone',option,value]),1)
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
