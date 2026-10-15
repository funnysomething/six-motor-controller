"""Host tests for coordinated Pi direction/PWM and raw I2C acknowledgements."""

import unittest
from unittest.mock import patch

from raspberry_pi import motor_control as motors


class Clock:
    def __init__(self):
        self.now = 10.0
        self.pauses = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


class FakePwm:
    def __init__(self, events):
        self.events = events
        self.values = (0,) * 6
        self.fail_next = False

    def set_percentages(self, values):
        self.events.append(("pwm", tuple(values)))
        if self.fail_next:
            self.fail_next = False
            raise OSError("link lost")
        self.values = tuple(values)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.time_patch = patch.object(motors, "time", self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.events = []
        self.pwm = FakePwm(self.events)
        self.pins = {}
        test = self

        class Output:
            def __init__(self, pin, initial_value):
                self.pin = pin
                self.value = initial_value
                self.closed = False
                test.pins[pin] = self

            def off(self):
                self.value = False
                test.events.append(("off", self.pin))

            def on(self):
                motor = next(i for i, pair in enumerate(motors.DIRECTION_PINS) if self.pin in pair)
                # Every direction-high change must follow confirmed disabled PWM.
                test.assertEqual(test.pwm.values[motor], 0)
                other = next(pin for pin in motors.DIRECTION_PINS[motor] if pin != self.pin)
                test.assertFalse(test.pins[other].value)
                self.value = True
                test.events.append(("on", self.pin))

            def close(self):
                self.closed = True

        self.controller = motors.MotorController(self.pwm, Output, start_heartbeat=False)
        self.addCleanup(self.controller.close)
        self.events.clear()

    def test_all_six_independent_speeds_and_directions(self):
        values = (60, -50, 40, -30, 0, 100)
        self.controller.set_speeds(values)
        self.assertEqual(self.pwm.values, (60, 50, 40, 30, 0, 100))
        self.assertEqual(self.controller.speeds, values)
        for value, pair in zip(values, motors.DIRECTION_PINS):
            self.assertEqual((self.pins[pair[0]].value, self.pins[pair[1]].value),
                             (value > 0, value < 0))

    def test_single_motor_change_keeps_five_running(self):
        self.controller.set_speeds((60,) * 6)
        self.events.clear()
        self.controller.set_motor(2, -40)
        self.assertEqual(self.events[0], ("pwm", (60, 0, 60, 60, 60, 60)))
        toggled = {event[1] for event in self.events if event[0] in ("on", "off")}
        self.assertEqual(toggled, set(motors.DIRECTION_PINS[1]))
        self.assertEqual(self.controller.speeds, (60, -40, 60, 60, 60, 60))
        self.assertEqual(self.clock.pauses, [0.5])

    def test_speed_only_change_does_not_toggle_direction(self):
        self.controller.set_speeds((60,) * 6)
        self.events.clear()
        self.controller.set_speeds((30,) * 6)
        self.assertEqual(self.events, [("pwm", (30,) * 6)])

    def test_stop_then_reverse_waits_remaining_coast_time(self):
        self.controller.set_speeds((60,) * 6)
        self.controller.set_speeds((0,) * 6)
        self.clock.now += 0.3
        self.controller.set_speeds((-60,) * 6)
        self.assertAlmostEqual(self.clock.pauses[-1], 0.2)

    def test_stop_then_same_direction_needs_no_reverse_pause(self):
        self.controller.set_speeds((60,) * 6)
        self.controller.set_speeds((0,) * 6)
        self.controller.set_speeds((50,) * 6)
        self.assertEqual(self.clock.pauses, [])

    def test_invalid_commands_have_no_hardware_effect(self):
        for values in ((100,) * 5, (101,) * 6, (True,) * 6, (1.5,) * 6):
            with self.assertRaises(ValueError):
                self.controller.set_speeds(values)
        with self.assertRaises(ValueError):
            self.controller.set_motor(7, 60)
        self.assertEqual(self.events, [])

    def test_link_failure_disables_directions_and_latches_fault(self):
        self.controller.set_speeds((60,) * 6)
        self.events.clear()
        self.pwm.fail_next = True
        with self.assertRaises(OSError):
            self.controller.set_speeds((-60,) * 6)
        self.assertFalse(any(pin.value for pin in self.pins.values()))
        self.assertFalse(any(event[0] == "on" for event in self.events))
        self.assertEqual(self.pwm.values, (0,) * 6)
        self.assertIsNotNone(self.controller.fault)
        with self.assertRaises(OSError):
            self.controller.set_speeds((60,) * 6)

    def test_interrupted_reversal_disables_all_motors(self):
        self.controller.set_speeds((60,) * 6)
        with patch.object(self.clock, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.set_speeds((-60,) * 6)
        self.assertEqual(self.pwm.values, (0,) * 6)
        self.assertFalse(any(pin.value for pin in self.pins.values()))

    def test_heartbeat_failure_stops_outputs(self):
        self.controller.set_speeds((60,) * 6)
        self.pwm.fail_next = True
        with patch.object(self.controller.done, "wait", return_value=False):
            self.controller._heartbeat()
        self.assertTrue(self.controller.done.is_set())
        self.assertFalse(any(pin.value for pin in self.pins.values()))

    def test_close_disables_pwm_and_releases_pins(self):
        self.controller.set_speeds((60,) * 6)
        self.controller.close()
        self.assertEqual(self.pwm.values, (0,) * 6)
        self.assertTrue(all(pin.closed and not pin.value for pin in self.pins.values()))
        self.events.clear()
        self.controller.close()
        self.assertEqual(self.events, [])
        # close is not an API for restarting the controller.
        with self.assertRaises(OSError):
            self.controller.set_speeds((60,) * 6)

    def test_close_failure_still_releases_direction_pins(self):
        self.controller.set_speeds((60,) * 6)
        self.pwm.fail_next = True
        with self.assertRaises(OSError):
            self.controller.close()
        self.assertTrue(all(pin.closed and not pin.value for pin in self.pins.values()))

    def test_startup_requires_zero_ack_before_starting_heartbeat(self):
        self.pwm.fail_next = True
        self.events.clear()
        with self.assertRaises(OSError):
            motors.MotorController(self.pwm, self.pins[26].__class__)
        self.assertFalse(any(event[0] == "on" for event in self.events))
        self.assertTrue(all(pin.closed and not pin.value for pin in self.pins.values()))


class Message(bytearray):
    @classmethod
    def write(cls, address, data):
        value = cls(data)
        value.is_read = False
        value.address = address
        return value

    @classmethod
    def read(cls, address, length):
        value = cls(length)
        value.is_read = True
        value.address = address
        return value


class Bus:
    def __init__(self, bad_replies=None, never_ack=False):
        self.frames = []
        self.reads = 0
        self.bad_replies = list(bad_replies or [])
        self.never_ack = never_ack

    def i2c_rdwr(self, message):
        if not message.is_read:
            self.frames.append(bytes(message))
            return
        self.reads += 1
        command = self.frames[-1]
        status = bytearray(b"\x5a\x01" + command[2:6] + b"\x00" + command[6:12])
        status.append(motors.checksum(status))
        defect = self.bad_replies.pop(0) if self.bad_replies else None
        if self.never_ack:
            defect = "sequence"
        if defect == "sequence":
            status[2] ^= 1
        elif defect == "flags":
            status[6] = 3  # Needs-zero + watchdog/reboot must never acknowledge motion.
        elif defect == "pwm":
            status[7] ^= 1
        elif defect == "checksum":
            status[13] ^= 1
        if defect != "checksum":
            status[13] = motors.checksum(status[:13])
        message[:] = status


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        time_patch = patch.object(motors, "time", self.clock)
        time_patch.start()
        self.addCleanup(time_patch.stop)

    def test_one_raw_frame_contains_all_six_channels(self):
        bus = Bus()
        pwm = motors.I2CPwm(bus, Message)
        pwm.sequence = 0x12345677
        pwm.set_percentages((0, 20, 40, 60, 80, 100))
        frame = bus.frames[0]
        self.assertEqual(frame[:12], b"\xa5\x01\x78\x56\x34\x12\x00\x14\x28\x3c\x50\x64")
        self.assertEqual(len(frame), 13)
        self.assertEqual(frame[12], motors.checksum(frame[:12]))

    def test_stale_status_wrong_pwm_flags_and_checksum_are_rejected(self):
        bus = Bus(["sequence", "pwm", "flags", "checksum"])
        motors.I2CPwm(bus, Message).set_percentages((60,) * 6)
        self.assertEqual(bus.reads, 5)
        self.assertEqual(len(bus.frames), 1)

    def test_missing_applied_ack_is_bounded(self):
        bus = Bus(never_ack=True)
        with self.assertRaises(OSError):
            motors.I2CPwm(bus, Message).set_percentages((60,) * 6)
        self.assertLess(self.clock.now, 10.32)

    def test_distance_extension_is_cached_with_ack_and_legacy_still_works(self):
        class SensorBus(Bus):
            state = 1
            mm = 1234
            corrupt = False

            def i2c_rdwr(self, message):
                super().i2c_rdwr(message)
                if message.is_read:
                    message.extend(bytes((0xD1, self.state)) + self.mm.to_bytes(2, "little"))
                    message.append(motors.checksum(message) ^ int(self.corrupt))

        bus = SensorBus()
        pwm = motors.I2CPwm(bus, Message)
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_sample, (self.clock.now, 1, 1234))
        previous = pwm.ultrasonic_sample
        bus.corrupt = True
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_sample, previous)
        bus.corrupt = False
        bus.mm = 5000  # Invalid distance must not become a displayed reading.
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_sample, previous)
        bus.state, bus.mm = 2, 0
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_sample, (self.clock.now, 2, 0))
        bus.state = 3
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_sample, (self.clock.now, 3, 0))
        bus.state = 1
        for mm in (20, 3000):
            bus.mm = mm
            pwm.set_percentages((30,) * 6)
            self.assertEqual(pwm.ultrasonic_sample, (self.clock.now, 1, mm))
        previous = pwm.ultrasonic_sample
        for mm in (0, 19, 3001, 4000):
            bus.mm = mm
            pwm.set_percentages((30,) * 6)
            self.assertEqual(pwm.ultrasonic_sample, previous)
        legacy = motors.I2CPwm(Bus(), Message)
        legacy.set_percentages((30,) * 6)
        self.assertIsNone(legacy.ultrasonic_sample)

    def test_distance_is_not_updated_from_unacknowledged_motor_status(self):
        class StaleSensorBus(Bus):
            def i2c_rdwr(self, message):
                super().i2c_rdwr(message)
                if message.is_read:
                    message.extend(b"\xd1\x01\xd2\x04")
                    message.append(motors.checksum(message))

        pwm = motors.I2CPwm(StaleSensorBus(never_ack=True), Message)
        with self.assertRaises(OSError):
            pwm.set_percentages((30,) * 6)
        self.assertIsNone(pwm.ultrasonic_sample)

    def test_two_sensors_are_checked_and_cached_independently(self):
        class DualSensorBus(Bus):
            samples = [(1, 1234), (1, 2345)]
            corrupt = [False, False]
            second_present = True

            def i2c_rdwr(self, message):
                if message.is_read:
                    self.requested_size = len(message)
                super().i2c_rdwr(message)
                if message.is_read:
                    state, mm = self.samples[0]
                    message.extend(bytes((0xD1, state)) + mm.to_bytes(2, "little"))
                    message.append(motors.checksum(message) ^ int(self.corrupt[0]))
                    if self.second_present:
                        state, mm = self.samples[1]
                        block = bytes((0xD2, state)) + mm.to_bytes(2, "little")
                        message.extend(block + bytes((motors.checksum(block)
                                                      ^ int(self.corrupt[1]),)))
                    else:
                        message.extend(b"\x00" * 5)  # Old firmware padding.

        bus = DualSensorBus()
        pwm = motors.I2CPwm(bus, Message)
        pwm.set_percentages((30,) * 6)
        self.assertEqual(bus.requested_size, 24)
        self.assertEqual(pwm.ultrasonic_samples, ((self.clock.now, 1, 1234),
                                                (self.clock.now, 1, 2345)))
        first, second = pwm.ultrasonic_samples
        bus.samples = [(1, 432), (3, 0)]
        bus.corrupt = [True, False]
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples, (first, (self.clock.now, 3, 0)))
        second = pwm.ultrasonic_samples[1]
        bus.corrupt = [False, True]
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples, ((self.clock.now, 1, 432), second))
        first = pwm.ultrasonic_samples[0]
        bus.corrupt = [False, False]
        bus.samples = [(1, 3001), (1, 3000)]
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples, (first, (self.clock.now, 1, 3000)))
        bus.second_present = False
        legacy = motors.I2CPwm(bus, Message)
        legacy.set_percentages((30,) * 6)
        self.assertEqual(legacy.ultrasonic_samples, (None, None))
        bus.samples = [(1, 1234), (1, 2345)]
        legacy.set_percentages((30,) * 6)
        self.assertEqual(legacy.ultrasonic_samples, ((self.clock.now, 1, 1234), None))

    def test_invalid_pwm_is_rejected_before_bus_write(self):
        bus = Bus()
        pwm = motors.I2CPwm(bus, Message)
        for values in ((-1,) * 6, (101,) * 6, (50,) * 5, (True,) * 6):
            with self.assertRaises(ValueError):
                pwm.set_percentages(values)
        self.assertEqual(bus.frames, [])

    def test_command_parsing(self):
        self.assertEqual(motors.parse_command("m 60"), ("all", (60,) * 6))
        self.assertEqual(motors.parse_command("m 2 -50"), ("motor", (2, -50)))
        self.assertEqual(motors.parse_command("m 60 -60 40 40 0 75"),
                         ("all", (60, -60, 40, 40, 0, 75)))
        self.assertEqual(motors.parse_command(" S "), ("s", ()))
        for text in ("m", "m 7 60", "m 60 60 60", "m 101", "m x", "m 1 3.5"):
            self.assertIsNone(motors.parse_command(text), text)


if __name__ == "__main__":
    unittest.main()
