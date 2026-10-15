"""Exercise real Tk widgets with a fake motor controller and no hardware."""

import threading
import time
import unittest
from unittest.mock import patch

from raspberry_pi.motor_control import MotorGui

try:
    import tkinter as tk
except ImportError:
    tk = None


class Controller:
    def __init__(self):
        self.speeds = (0,) * 6
        self.fault = None
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    def set_speeds(self, speeds):
        self.calls.append(speeds)
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("Test did not release request")
        self.speeds = speeds


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

    def test_distance_updates_and_invalid_or_stale_readings_clear_value(self):
        self.controller.ultrasonic_sample = (time.monotonic(), 1, 1234)
        self.wait_until(lambda: "123.4 cm" in self.app.distance.get())
        self.controller.ultrasonic_sample = (time.monotonic(), 2, 0)
        self.wait_until(lambda: "no echo" in self.app.distance.get())
        self.controller.ultrasonic_sample = (time.monotonic() - 2, 1, 1234)
        self.wait_until(lambda: "stale" in self.app.distance.get())
        self.controller.fault = OSError("link lost")
        self.wait_until(lambda: "link failed" in self.app.distance.get())

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


if __name__ == "__main__":
    unittest.main()
