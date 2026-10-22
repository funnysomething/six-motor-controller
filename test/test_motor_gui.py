"""Exercise real Tk widgets with a fake motor controller and no hardware."""

import threading
import time
import unittest
from unittest.mock import patch

from raspberry_pi.motor_control import MotorGui
from raspberry_pi.sen0140 import ImuReading, Sen0140

try:
    import tkinter as tk
except ImportError:
    tk = None


class Controller:
    def __init__(self):
        self.speeds = (0,) * 6
        self.fault = None
        self.depth_control = None
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    def set_speeds(self, speeds):
        self.depth_control = None
        self.calls.append(speeds)
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("Test did not release request")
        self.speeds = speeds

    def enable_depth_control(self, settings):
        self.set_speeds((0,) * 6)
        self.depth_control = settings


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

    def test_invalid_input_never_reaches_hardware(self):
        for value in ("oops", "101", "1.5", ""):
            self.app.values[0].set(value)
            self.app.apply()
        self.app.group_value.set("-101")
        self.app.copy_group()
        self.assertEqual(self.controller.calls, [])
        self.assertFalse(self.app.busy)

    def test_automatic_mode_defaults_pairing_and_stop_returns_to_manual(self):
        self.assertFalse(self.app.auto_enabled.get())
        self.app.depth_speeds[1].set(-80)
        self.app.auto_enabled.set(True)
        self.app.toggle_auto()
        self.wait_until(lambda: not self.app.busy)
        self.assertEqual(self.controller.depth_control.pair_speeds, (100, -80, 100))
        self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget in self.app.controls))
        self.assertEqual(str(self.app.auto_toggle.cget("state")), "normal")
        self.app.apply()
        self.assertEqual(self.controller.calls, [(0,) * 6])
        self.app.stop()
        self.wait_until(lambda: not self.app.busy)
        self.assertFalse(self.app.auto_enabled.get())
        self.assertIsNone(self.controller.depth_control)
        self.assertEqual(self.controller.speeds, (0,) * 6)
        self.assertTrue(all(str(widget.cget("state")) == "normal" for widget in self.app.controls))

    def test_disabling_automatic_mode_stops_all_motors(self):
        self.app.auto_enabled.set(True)
        self.app.toggle_auto()
        self.wait_until(lambda: not self.app.busy)
        self.controller.speeds = (60,) * 6
        self.app.auto_enabled.set(False)
        self.app.toggle_auto()
        self.wait_until(lambda: not self.app.busy)
        self.assertIsNone(self.controller.depth_control)
        self.assertEqual(self.controller.speeds, (0,) * 6)

    def test_invalid_automatic_settings_do_not_enable_motion(self):
        for value in ("oops", "101", "1.5", ""):
            self.app.depth_speeds[0].set(value)
            self.app.auto_enabled.set(True)
            self.app.toggle_auto()
            self.assertFalse(self.app.auto_enabled.get())
        self.assertEqual(self.controller.calls, [])

    def test_custom_distance_limits_and_max_speeds_reach_automatic_controller(self):
        for sensor, (low, high, speed) in enumerate(((2.5, 8, 90), (5, 15, -80), (10, 30, 60))):
            self.app.depth_min_distances[sensor].set(str(low))
            self.app.depth_max_distances[sensor].set(str(high))
            self.app.depth_speeds[sensor].set(str(speed))
        self.app.auto_enabled.set(True)
        self.app.toggle_auto()
        self.wait_until(lambda: not self.app.busy)
        settings = self.controller.depth_control
        self.assertEqual(settings.min_distances_cm, (2.5, 5, 10))
        self.assertEqual(settings.max_distances_cm, (8, 15, 30))
        self.assertEqual(settings.pair_speeds, (90, -80, 60))
        self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget in self.app.controls))

    def test_invalid_distance_limits_leave_automatic_mode_disabled(self):
        for low, high in (("", "10"), ("nan", "10"), ("3", "inf"),
                          ("1", "10"), ("3", "301"), ("10", "10"), ("11", "10")):
            self.app.depth_min_distances[0].set(low)
            self.app.depth_max_distances[0].set(high)
            self.app.auto_enabled.set(True)
            self.app.toggle_auto()
            self.assertFalse(self.app.auto_enabled.get())
            self.assertFalse(self.app.busy)
        self.assertEqual(self.controller.calls, [])

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
        self.assertIsNone(self.controller.depth_control)
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

    def test_output_feedback_flags_disabled_gpio_despite_positive_applied_pwm(self):
        self.controller.speeds = (100,) * 6
        self.controller.last_pwm_ack_at = time.monotonic()
        self.controller.output_sample = ((100,) * 6, ((0, 0),) * 6, "")
        self.wait_until(lambda: "GPIO feedback disagrees" in self.app.status.get())
        self.assertIn("M1=00", self.app.output_status.get())
        self.assertEqual([value.get() for value in self.app.applied], ["+100%"] * 6)

    def test_stalled_motor_worker_feedback_warns_that_pwm_may_have_stopped(self):
        self.controller.speeds = (100,) * 6
        self.controller.output_sample = ((100,) * 6, ((1, 0),) * 6, "")
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
