"""Raspberry Pi thermal readings with fake sysfs files and no Pi hardware."""

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from raspberry_pi.motor_control import (
    PiThermalMonitor, PiThermalReading, pi_thermal_text, read_pi_thermals, read_pi_throttled)


def no_vcgencmd(*args, **kwargs):
    raise FileNotFoundError("vcgencmd")


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.temp, self.freq, self.throttled = root / "temp", root / "freq", root / "throttled"

    def tearDown(self):
        self.dir.cleanup()

    def read(self, run=no_vcgencmd):
        return read_pi_thermals(self.temp, self.freq, self.throttled, run=run, clock=lambda: 7.0)

    def test_reads_sysfs_temperature_frequency_and_throttle_bits(self):
        self.temp.write_text("52583\n")
        self.freq.write_text("1500000\n")
        self.throttled.write_text("50005\n")
        self.assertEqual(self.read(), PiThermalReading(7.0, 52.583, 1500.0, 0x50005))

    def test_missing_temperature_is_an_error_reading(self):
        reading = self.read()
        self.assertIsNone(reading.temperature_c)
        self.assertIn("no CPU temperature", reading.error)

    def test_frequency_and_throttle_are_optional(self):
        self.temp.write_text("45000")
        self.assertEqual(self.read(), PiThermalReading(7.0, 45.0, None, None))

    def test_throttle_falls_back_to_vcgencmd(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(stdout="throttled=0x4\n")

        self.assertEqual(read_pi_throttled(self.throttled, run), 0x4)
        self.assertEqual(calls, [["vcgencmd", "get_throttled"]])

    def test_vcgencmd_failures_mean_unknown(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("vcgencmd", 1)

        for run in (no_vcgencmd, timeout, lambda *a, **k: SimpleNamespace(stdout="garbage")):
            self.assertIsNone(read_pi_throttled(self.throttled, run))


class TextTests(unittest.TestCase):
    def text(self, temperature, throttled=0, mhz=1800.0, at=10.0, now=10.5):
        return pi_thermal_text(PiThermalReading(at, temperature, mhz, throttled), now)

    def test_normal_reading(self):
        self.assertEqual(self.text(48.04),
                         ("CPU temperature: 48.0 C at 1800 MHz", "Throttling: none", "normal"))

    def test_temperature_thresholds(self):
        self.assertEqual(self.text(70.0)[2], "warning")
        self.assertIn("hot", self.text(70.0)[0])
        self.assertEqual(self.text(80.0)[2], "critical")
        self.assertIn("throttling range", self.text(80.0)[0])

    def test_active_and_past_throttle_flags(self):
        _, line, severity = self.text(50, throttled=0x50005)
        self.assertEqual(line, "Throttling NOW: under-voltage, throttled")
        self.assertEqual(severity, "critical")
        _, line, severity = self.text(50, throttled=0x80000)
        self.assertEqual(line, "Throttling: none; since boot: soft temperature limit")
        self.assertEqual(severity, "warning")

    def test_unknown_throttle_keeps_temperature(self):
        temperature, line, severity = self.text(40, throttled=None, mhz=None)
        self.assertEqual(temperature, "CPU temperature: 40.0 C")
        self.assertIn("unknown", line)
        self.assertEqual(severity, "normal")

    def test_waiting_error_and_stale(self):
        self.assertIn("waiting", pi_thermal_text(None, 0)[0])
        self.assertIn("unavailable (boom)", pi_thermal_text(PiThermalReading(1.0, error="boom"), 1)[0])
        self.assertIn("stale", self.text(50, at=0.0, now=10.0)[0])


class MonitorTests(unittest.TestCase):
    def test_samples_in_background_and_survives_reader_errors(self):
        readings = iter([PiThermalReading(1.0, 50.0)])

        def reader():
            try:
                return next(readings)
            except StopIteration:
                raise RuntimeError("sensor gone")

        monitor = PiThermalMonitor(reader, interval=0.01)
        try:
            deadline = time.monotonic() + 2
            while (monitor.latest is None or not monitor.latest.error) and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(monitor.latest.error, "sensor gone")
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
