"""Exercise real Tk widgets with a fake motor controller and no hardware."""

import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from raspberry_pi.motor_control import MotorGui
from raspberry_pi.stabilization import VehicleConfig
from raspberry_pi.sen0140 import ImuReading, Sen0140

try:
    import tkinter as tk
except ImportError:
    tk = None


class Controller:
    def __init__(self):
        self.speeds = (0,) * 6
        self.fault = None
        self.automatic = False
        self.config = VehicleConfig(verified=True)
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    def set_speeds(self, speeds):
        self.automatic = False
        self.calls.append(speeds)
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("Test did not release request")
        self.speeds = speeds

    def enable_stabilization(self):
        self.set_speeds((0,)*6)
        self.automatic = True
        self.stabilizer = SimpleNamespace(target=1., heading_target=30., yaw_mode="heading")

    def set_targets(self, clearance, heading):
        if not .02 <= clearance <= 3 or not self.automatic:
            raise ValueError("Invalid target or automatic mode disabled")
        self.targets = clearance, heading

    def recapture_heading(self):
        if not self.automatic:
            raise ValueError("Enable stabilization first")
        self.stabilizer.heading_target = 45.


@unittest.skipIf(tk is None, "Optional Tk is not installed")
class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"No Tk display: {error}")
        self.root.withdraw()  # Never show test windows on the user's desktop.
        self.controller = Controller()
        self.app = MotorGui(self.root, self.controller)

    def tearDown(self):
        self.controller.release.set()
        if self.app.worker is not None:
            self.app.worker.join(timeout=2)
        try:
            for callback in self.root.tk.call("after", "info"):
                self.root.after_cancel(callback)
            self.root.destroy()
        except tk.TclError:
            pass

    def wait_until(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.assertTrue(condition(), "GUI action did not complete")

    def test_group_edits_are_staged_until_apply(self):
        self.app.group_value.set(-45)
        self.app.copy_group()
        self.assertEqual([int(v.get()) for v in self.app.values], [-45] * 6)
        self.assertEqual(self.controller.calls, [])
        self.app.apply()
        self.wait_until(lambda: not self.app.busy)
        self.assertEqual(self.controller.speeds, (-45,) * 6)
        self.assertEqual([v.get() for v in self.app.applied], ["-45%"] * 6)

    def attach_camera(self):
        from unittest.mock import Mock
        camera = SimpleNamespace(running=True, stopping_at=None, status="YOLO11n PyTorch / Pi CPU",
                                 latest=None, start=Mock(), stop=Mock(), poll=Mock())
        self.app.camera = camera
        self.app.tabs.select(self.app.camera_tab)
        return camera

    def test_camera_preview_displays_real_pixels_without_commanding_motors(self):
        camera = self.attach_camera()
        camera.latest = (time.monotonic(), {"image": b"P6\n1 1\n255\n\xff\x00\x00"})
        self.app.update_camera()
        self.assertEqual(self.app.camera_image.get(0, 0), (255, 0, 0))
        self.assertEqual(self.controller.calls, [])
        self.assertIn("Pi CPU", self.app.camera_status.get())

    def test_camera_error_keeps_manual_motion_and_stop_available(self):
        camera = self.attach_camera()
        camera.running = False
        camera.status = "Camera failed: unplugged"
        self.app.update_camera()
        self.assertIn("unplugged", self.app.camera_status.get())
        self.app.values[0].set(40)
        self.app.apply()
        self.wait_until(lambda: not self.app.busy)
        self.assertEqual(self.controller.speeds, (40, 0, 0, 0, 0, 0))
        self.app.stop()
        self.wait_until(lambda: not self.app.busy)
        self.assertEqual(self.controller.speeds, (0,) * 6)

    def test_stale_camera_frame_is_cleared_without_changing_motor_settings(self):
        camera = self.attach_camera()
        camera.latest = (time.monotonic() - 4, {"image": b"old frame"})
        self.controller.speeds = (50,) * 6
        self.app.update_camera()
        self.assertIn("stale", self.app.camera_status.get())
        self.assertIsNone(self.app.camera_image)
        self.assertEqual(self.controller.speeds, (50,) * 6)

    def test_closing_gui_requests_camera_stop(self):
        camera = self.attach_camera()
        with patch.object(self.root, "destroy"):
            self.app.close()
        camera.stop.assert_called_once()

    def test_invalid_input_never_reaches_hardware(self):
        for value in ("oops", "101", "1.5", ""):
            self.app.values[0].set(value)
            self.app.apply()
        self.app.group_value.set("-101")
        self.app.copy_group()
        self.assertEqual(self.controller.calls, [])
        self.assertFalse(self.app.busy)

    def test_automatic_mode_captures_targets_and_stop_returns_to_manual(self):
        self.assertFalse(self.app.auto_enabled.get())
        self.app.auto_enabled.set(True)
        self.app.toggle_auto()
        self.wait_until(lambda: not self.app.busy)
        self.assertTrue(self.controller.automatic)
        self.assertEqual(self.app.clearance_target.get(), "100.0")
        self.assertEqual(self.app.heading_target.get(), "30.0")
        self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget in self.app.controls))
        self.app.stop()
        self.wait_until(lambda: not self.app.busy)
        self.assertFalse(self.controller.automatic)
        self.assertEqual(self.controller.speeds,(0,)*6)

    def test_disabling_automatic_mode_stops_all_motors(self):
        self.app.auto_enabled.set(True)
        self.app.toggle_auto()
        self.wait_until(lambda: not self.app.busy)
        self.controller.speeds = (60,) * 6
        self.app.auto_enabled.set(False)
        self.app.toggle_auto()
        self.wait_until(lambda: not self.app.busy)
        self.assertFalse(self.controller.automatic)
        self.assertEqual(self.controller.speeds, (0,) * 6)

    def test_invalid_targets_do_not_reach_controller(self):
        self.controller.automatic = True
        self.controller.stabilizer = SimpleNamespace(yaw_mode="heading")
        for value in ("oops", "nan", "inf", "", "1", "301"):
            self.app.clearance_target.set(value)
            self.app.heading_target.set("30")
            self.app.apply_targets()
        self.assertFalse(hasattr(self.controller,"targets"))

    def test_targets_and_explicit_heading_recapture(self):
        self.controller.enable_stabilization()
        self.app.clearance_target.set("120")
        self.app.heading_target.set("40")
        self.app.apply_targets()
        self.assertEqual(self.controller.targets,(1.2,40.))
        self.app.recapture_heading()
        self.assertEqual(self.app.heading_target.get(),"45.0")

    def test_compass_degradation_does_not_uncheck_auto(self):
        self.controller.enable_stabilization()
        self.app.auto_enabled.set(True)
        self.controller.control_notice = "Clearance and leveling active; yaw damping"
        self.app.poll()
        self.assertTrue(self.app.auto_enabled.get())
        self.assertIn("yaw damping", self.app.stabilization_status.get())

    def test_stop_during_enable_cancels_automatic_mode_as_next_request(self):
        self.controller.release.clear()
        self.app.auto_enabled.set(True)
        self.app.toggle_auto()
        self.assertTrue(self.controller.started.wait(1))
        self.app.stop()
        self.assertTrue(self.app.stop_requested)
        self.controller.release.set()
        self.wait_until(lambda: not self.app.busy)
        self.assertFalse(self.app.auto_enabled.get())
        self.assertFalse(self.controller.automatic)
        self.assertEqual(self.controller.calls, [(0,) * 6, (0,) * 6])

    def test_apply_keeps_ui_responsive_and_stop_is_next_command(self):
        self.controller.release.clear()
        for value in self.app.values:
            value.set(60)
        self.app.apply()
        self.assertTrue(self.controller.started.wait(1))
        # Apply has returned although the hardware request is still waiting.
        self.assertTrue(self.app.busy)
        self.app.stop()
        self.app.apply()  # No extra motion may queue ahead of the stop.
        self.assertTrue(self.app.stop_requested)
        self.assertEqual(self.controller.calls, [(60,) * 6])
        self.controller.release.set()
        self.wait_until(lambda: not self.app.busy)
        self.assertEqual(self.controller.calls, [(60,) * 6, (0,) * 6])
        self.assertEqual(self.controller.speeds, (0,) * 6)

    def test_heartbeat_fault_is_shown_without_user_input(self):
        self.controller.fault = OSError("link lost")
        self.wait_until(lambda: "link lost" in self.app.status.get())
        self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget in self.app.controls))
        self.app.apply()
        self.assertEqual(self.controller.calls, [])

    def test_output_feedback_flags_reverse_dir_despite_positive_applied_pwm(self):
        self.controller.speeds = (100,) * 6
        self.controller.last_pwm_ack_at = time.monotonic()
        self.controller.output_sample = ((100,) * 6, (0,) * 6, "")
        self.wait_until(lambda: "GPIO feedback disagrees" in self.app.status.get())
        self.assertIn("M1=0", self.app.output_status.get())
        self.assertEqual([value.get() for value in self.app.applied], ["+100%"] * 6)

    def test_stalled_motor_worker_feedback_warns_that_pwm_may_have_stopped(self):
        self.controller.speeds = (100,) * 6
        self.controller.output_sample = ((100,) * 6, (1,) * 6, "")
        self.controller.last_pwm_ack_at = time.monotonic() - 2
        self.wait_until(lambda: "Motor updates stale" in self.app.status.get())
        self.assertIn("watchdog", self.app.status.get())

    def test_distance_updates_and_invalid_or_stale_readings_clear_value(self):
        self.controller.ultrasonic_sample = (time.monotonic(), 1, 1234)
        self.wait_until(lambda: "123.4 cm" in self.app.distance.get())
        self.controller.ultrasonic_sample = (time.monotonic(), 2, 0)
        self.wait_until(lambda: "no underwater echo" in self.app.distance.get())
        self.assertIn("2-300 cm", self.app.distance.get())
        self.controller.ultrasonic_sample = (time.monotonic(), 3, 0)
        self.wait_until(lambda: "no valid UART response" in self.app.distance.get())
        self.controller.ultrasonic_sample = (time.monotonic() - 2, 1, 1234)
        self.wait_until(lambda: "stale" in self.app.distance.get())
        self.controller.fault = OSError("link lost")
        self.wait_until(lambda: "link failed" in self.app.distance.get())

    def test_two_sensor_readings_and_faults_are_independent(self):
        now = time.monotonic()
        self.controller.ultrasonic_samples = ((now, 1, 1234), (now, 1, 2345))
        self.wait_until(lambda: "234.5 cm" in self.app.distances[1].get())
        self.assertEqual(self.app.distances[0].get(), "Ultrasonic 1: 123.4 cm")
        self.assertEqual(self.app.distances[1].get(), "Ultrasonic 2: 234.5 cm")
        self.controller.ultrasonic_samples = ((time.monotonic(), 1, 1234),
                                              (time.monotonic(), 3, 0))
        self.wait_until(lambda: "no valid UART response" in self.app.distances[1].get())
        self.assertIn("123.4 cm", self.app.distances[0].get())
        self.controller.ultrasonic_samples = ((time.monotonic() - 2, 1, 1234),
                                              (time.monotonic(), 1, 2345))
        self.wait_until(lambda: "stale" in self.app.distances[0].get())
        self.assertIn("234.5 cm", self.app.distances[1].get())
        self.controller.ultrasonic_samples = (None, (time.monotonic(), 2, 0))
        self.wait_until(lambda: "no underwater echo" in self.app.distances[1].get())
        self.assertIn("unavailable", self.app.distances[0].get())
        self.controller.fault = OSError("link lost")
        self.wait_until(lambda: all("link failed" in variable.get()
                                    for variable in self.app.distances))

    def test_third_sensor_display_timeout_and_older_firmware(self):
        now = time.monotonic()
        self.controller.ultrasonic_samples = ((now, 1, 321), (now, 1, 1234), (now, 1, 2345))
        self.wait_until(lambda: "234.5 cm" in self.app.distances[2].get())
        self.assertEqual(self.app.distances[2].get(), "Ultrasonic 3: 234.5 cm")
        self.controller.ultrasonic_samples = ((now, 1, 321), (now, 1, 1234), (now, 3, 0))
        self.wait_until(lambda: "no valid UART response" in self.app.distances[2].get())
        self.assertIn("32.1 cm", self.app.distances[0].get())
        self.assertIn("123.4 cm", self.app.distances[1].get())
        self.controller.ultrasonic_samples = ((now, 1, 321), (now, 1, 1234),
                                              (now - 2, 1, 2345))
        self.wait_until(lambda: "stale" in self.app.distances[2].get())
        self.controller.ultrasonic_samples = ((now, 1, 321), (now, 1, 1234))
        self.wait_until(lambda: "unavailable" in self.app.distances[2].get())
        self.assertIn("123.4 cm", self.app.distances[1].get())

    def test_close_waits_for_active_request_before_leaving_mainloop(self):
        self.controller.release.clear()
        self.app.values[0].set(60)
        self.app.apply()
        self.assertTrue(self.controller.started.wait(1))
        with patch.object(self.root, "destroy", wraps=self.root.destroy) as destroy:
            self.app.close()
            destroy.assert_not_called()
            self.controller.release.set()
            self.wait_until(lambda: destroy.called)
        self.assertFalse(self.app.busy)

    def test_imu_units_tilt_errors_and_stale_data(self):
        self.controller.imu = Sen0140(None)
        now = time.monotonic()
        self.controller.imu.readings = {
            "accel": ImuReading(now, (0.0, 0.0, 1.0)),
            "gyro": ImuReading(now, (1.0, -2.0, 3.0)),
            "mag": ImuReading(now, (123, -456, 789)),
            "barometer": ImuReading(now, (25.08, 1006.53)),
        }
        self.wait_until(lambda: "1006.53 hPa" in self.app.imu_values["barometer"].get())
        self.assertIn("Z +1.000 g", self.app.imu_values["accel"].get())
        self.assertIn("deg/s", self.app.imu_values["gyro"].get())
        self.assertIn("raw counts", self.app.imu_values["mag"].get())
        self.assertIn("roll +0.0 deg", self.app.imu_tilt.get())
        self.controller.imu.readings["mag"] = ImuReading(now, error="No response")
        self.controller.imu.readings["accel"] = ImuReading(now - 4, (0, 0, 1))
        self.wait_until(lambda: "No response" in self.app.imu_values["mag"].get())
        self.assertIn("stale", self.app.imu_values["accel"].get())
        self.assertIn("unavailable", self.app.imu_tilt.get())
        self.assertIn("1006.53 hPa", self.app.imu_values["barometer"].get())
        self.controller.fault = OSError("link lost")
        self.wait_until(lambda: all("polling stopped" in variable.get()
                                    for variable in self.app.imu_values.values()))

    def test_failed_request_is_reported_and_motion_controls_disabled(self):
        def fail(values):
            self.controller.fault = OSError("no PWM acknowledgement")
            raise self.controller.fault

        self.controller.set_speeds = fail
        self.app.values[0].set(60)
        self.app.apply()
        self.wait_until(lambda: not self.app.busy)
        self.assertIn("no PWM acknowledgement", self.app.status.get())
        self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget in self.app.controls))

    def test_shutdown_signal_requests_normal_window_close(self):
        with patch.object(self.root, "destroy", wraps=self.root.destroy) as destroy:
            self.app.shutdown_requested.set()
            self.wait_until(lambda: destroy.called)
        self.assertTrue(self.app.closing)


    def test_reconnecting_status_disables_motion_then_recovers_without_restart(self):
        self.controller.ultrasonic_samples = ((time.monotonic(), 1, 30),) * 3
        self.controller.recovering = True
        self.controller.link_notice = "I2C reconnecting (1/5); all motors stopped."
        self.wait_until(lambda: "reconnecting" in self.app.status.get())
        self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget in self.app.controls))
        self.assertTrue(all("reconnecting" in variable.get() for variable in self.app.distances))
        self.app.apply()
        self.assertEqual(self.controller.calls, [])
        self.controller.recovering = False
        self.controller.link_notice = "I2C recovered; all motors stopped."
        self.wait_until(lambda: "recovered" in self.app.status.get())
        self.assertTrue(all(str(widget.cget("state")) == "normal" for widget in self.app.controls))
        self.assertIsNone(self.controller.fault)


if __name__ == "__main__":
    unittest.main()
