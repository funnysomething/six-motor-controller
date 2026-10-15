"""Read-only ESP32 diagnostics and actionable I2C failure reports."""

import contextlib
import io
import unittest
from unittest.mock import patch

from raspberry_pi import motor_control as motors
from test.test_motor_control import Message


class StatusBus:
    def __init__(self, maximum=29, fail=False, corrupt=False):
        self.maximum = maximum
        self.fail = fail
        self.corrupt = corrupt
        self.read_sizes = []

    def i2c_rdwr(self, message):
        if not message.is_read:
            raise AssertionError("Diagnostic must never write a motor command")
        size = len(message)
        self.read_sizes.append(size)
        if self.fail or size > self.maximum:
            raise OSError(5, "Input/output error")
        status = bytearray(b"\x5a\x01" + bytes(4) + b"\x01" + bytes(6))
        status.append(motors.checksum(status))
        for sensor in range(3):
            block = bytes((0xD1 + sensor, 3, 0, 0))
            checksum = motors.checksum(status + block if sensor == 0 else block)
            status.extend(block + bytes((checksum,)))
        if self.corrupt:
            status[13] ^= 1
        message[:] = status[:size]


class I2CDiagnosticTests(unittest.TestCase):
    def inspect(self, bus):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(motors.time, "sleep"):
            result = motors.inspect_i2c(bus, Message, 0x32)
        return result, output.getvalue()

    def test_full_status_is_read_without_command_writes(self):
        bus = StatusBus()
        result, output = self.inspect(bus)
        self.assertEqual(result, 0)
        self.assertEqual(bus.read_sizes, [29])
        self.assertIn("Three-sensor firmware", output)
        self.assertIn("flags=0x01", output)  # Does not clear the startup interlock.
        self.assertIn("does not test command writes", output)

    def test_short_firmware_reply_is_distinguished_from_unreachable_device(self):
        bus = StatusBus(maximum=14)
        result, output = self.inspect(bus)
        self.assertEqual(result, 1)
        self.assertEqual(bus.read_sizes, [29, 29, 14])
        self.assertIn("Motor status is readable", output)

    def test_no_device_or_corrupt_status_never_passes(self):
        for bus in (StatusBus(fail=True), StatusBus(corrupt=True)):
            with self.subTest(fail=bus.fail):
                result, output = self.inspect(bus)
                self.assertEqual(result, 1)
                self.assertEqual(bus.read_sizes, [29, 29, 14, 14])
                self.assertIn("could not be verified", output)

    def test_diagnostic_dispatch_does_not_claim_gpio(self):
        with patch("sys.argv", ["motor_control.py", "--diagnose-i2c"]), \
                patch.object(motors, "diagnose_i2c", return_value=1) as diagnostic, \
                patch.object(motors, "make_gpio_factory") as gpio:
            self.assertEqual(motors.main(), 1)
        diagnostic.assert_called_once_with(1, 0x32)
        gpio.assert_not_called()

    def test_write_and_read_failures_keep_errno_and_identify_transaction(self):
        class FailedBus:
            def __init__(self, write_fails):
                self.write_fails = write_fails
                self.calls = 0

            def i2c_rdwr(self, message):
                self.calls += 1
                if self.write_fails or message.is_read:
                    raise OSError(5, "Input/output error")

        for write_fails, operation in ((True, "command write"), (False, "29-byte status read")):
            with self.subTest(operation=operation):
                bus = FailedBus(write_fails)
                with self.assertRaises(OSError) as caught:
                    motors.I2CPwm(bus, Message).set_percentages((0,) * 6)
                self.assertEqual(caught.exception.errno, 5)
                self.assertIn(operation, str(caught.exception))
                self.assertIn("0x32", str(caught.exception))
                self.assertIn("--diagnose-i2c", str(caught.exception))
                self.assertEqual(bus.calls, 1 if write_fails else 2)
