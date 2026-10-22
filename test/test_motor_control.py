"""Host tests for coordinated Pi direction/PWM and raw I2C acknowledgements."""

import unittest
from unittest.mock import Mock, patch

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
        self.ultrasonic_samples = (None, None, None)

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

    def test_missing_imu_does_not_interrupt_pwm_heartbeat(self):
        from raspberry_pi.sen0140 import Sen0140
        from test.test_sen0140 import Bus

        bus = Bus()
        bus.failed.update((0x53, 0x68, 0x0C, 0x77))
        self.controller.imu = Sen0140(bus)
        with patch.object(self.controller.done, "wait", side_effect=[False] * 4 + [True]):
            self.controller._heartbeat()
        self.assertEqual(len(self.events), 4)
        self.assertTrue(all(event == ("pwm", (0,) * 6) for event in self.events))
        self.assertIsNone(self.controller.fault)
        self.assertTrue(all(reading.error for reading in self.controller.imu.readings.values()))

    def test_unexpected_imu_exception_stops_outputs_and_reports_worker_fault(self):
        self.controller.set_speeds((100,) * 6)
        self.controller.imu = Mock()
        self.controller.imu.poll_next.side_effect = RuntimeError("IMU worker crashed")
        with patch.object(self.controller.done, "wait", return_value=False):
            self.controller._heartbeat()
        self.assertIsInstance(self.controller.fault, RuntimeError)
        self.assertIn("IMU worker crashed", str(self.controller.fault))
        self.assertEqual(self.controller.speeds, (0,) * 6)
        self.assertTrue(self.controller.done.is_set())
        self.assertFalse(any(pin.value for pin in self.pins.values()))

    def test_output_feedback_reads_gpio_rather_than_infer_it_from_requested_speed(self):
        self.controller.set_speeds((100, 100, -50, -50, 0, 0))
        speeds, directions, error = self.controller.output_sample
        self.assertEqual(speeds, self.controller.speeds)
        self.assertEqual(directions, ((1, 0), (1, 0), (0, 1), (0, 1), (0, 0), (0, 0)))
        self.assertEqual(error, "")
        self.pins[motors.DIRECTION_PINS[0][0]].off()
        self.controller._record_outputs()
        self.assertEqual(self.controller.output_sample[0][0], 100)
        self.assertEqual(self.controller.output_sample[1][0], (0, 0))

    def test_trace_contains_automatic_pwm_gpio_readback_and_ack_age(self):
        import contextlib
        import io

        self.controller.enable_depth_control(motors.DepthControl())
        self.pwm.ultrasonic_samples = ((self.clock.now, 1, 30),) * 3
        self.controller.trace = True
        output = io.StringIO()
        with contextlib.redirect_stderr(output), \
                patch.object(self.controller.done, "wait", side_effect=[False, True]):
            self.controller._heartbeat()
        self.assertIn("Motors auto: applied=(100, 100, 100, 100, 100, 100)", output.getvalue())
        self.assertIn("GPIO IN1/IN2=10 10 10 10 10 10", output.getvalue())
        self.assertIn("PWM ack age=0.00s", output.getvalue())
        self.controller.trace = False

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
        self.events.clear()
        self.pwm.fail_next = True
        with patch.object(self.controller.done, "wait", return_value=False):
            self.controller._heartbeat()
        self.assertTrue(self.controller.done.is_set())
        self.assertFalse(any(pin.value for pin in self.pins.values()))
        # One failed motion write, followed only by the best-effort safe stop.
        self.assertEqual([event for event in self.events if event[0] == "pwm"],
                         [("pwm", (60,) * 6), ("pwm", (0,) * 6)])
        self.assertEqual(self.clock.pauses, [])
        with self.assertRaises(OSError):
            self.controller.set_speeds((60,) * 6)

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

    def test_automatic_pairs_follow_independent_sensors_and_stop_on_bad_readings(self):
        self.controller.set_speeds((80,) * 6)
        settings = motors.DepthControl((100, -80, 50))
        self.controller.enable_depth_control(settings)
        self.assertEqual(self.controller.speeds, (0,) * 6)
        self.pwm.ultrasonic_samples = ((self.clock.now, 1, 65),
                                       (self.clock.now, 1, 30),
                                       (self.clock.now, 1, 100))
        self.clock.now += 0.6  # Initial manual command has coasted before reversing.
        with patch.object(self.controller.done, "wait", side_effect=[False, True]):
            self.controller._heartbeat()
        self.assertEqual(self.controller.speeds, (50, 50, -80, -80, 0, 0))
        # No echo stops only the affected pair; the second pair keeps moving.
        self.pwm.ultrasonic_samples = ((self.clock.now, 2, 0),
                                       (self.clock.now, 1, 30), None)
        self.controller._update_depth_control()
        self.assertEqual(self.controller.speeds, (0, 0, -80, -80, 0, 0))
        self.clock.now += 1.01
        self.controller._update_depth_control()
        self.assertEqual(self.controller.speeds, (0,) * 6)
        # Fresh data resumes automatic control without re-enabling the mode.
        self.pwm.ultrasonic_samples = (None, None, (self.clock.now, 1, 30))
        self.controller._update_depth_control()
        self.assertEqual(self.controller.speeds, (0, 0, 0, 0, 50, 50))

    def test_automatic_heartbeat_matches_manual_pwm_and_direction_outputs(self):
        values = (100, 100, -50, -50, 20, 20)
        self.controller.set_speeds(values)
        manual_events = list(self.events)
        manual_gpio = tuple(self.pins[pin].value for pair in motors.DIRECTION_PINS for pin in pair)
        self.controller.set_speeds((0,) * 6)
        self.clock.now += motors.REVERSE_PAUSE
        self.controller.enable_depth_control(motors.DepthControl((100, -100, 100)))
        self.events.clear()
        self.pwm.ultrasonic_samples = ((self.clock.now, 1, 30),
                                       (self.clock.now, 1, 65),
                                       (self.clock.now, 1, 86))
        with patch.object(self.controller.done, "wait", side_effect=[False, True]):
            self.controller._heartbeat()
        # The heartbeat first refreshes the previous (zero) command, then
        # automatic mode must make the same GPIO/PWM changes as manual Apply.
        self.assertEqual(self.events[1:], manual_events)
        self.assertEqual(self.controller.speeds, values)
        self.assertEqual(self.pwm.values, tuple(abs(value) for value in values))
        self.assertEqual(tuple(self.pins[pin].value for pair in motors.DIRECTION_PINS for pin in pair),
                         manual_gpio)

    def test_automatic_reversal_keeps_polling_without_blocking_and_rechecks_sensor(self):
        self.controller.set_speeds((-30, -30, 0, 0, 0, 0))
        self.controller.enable_depth_control(motors.DepthControl((30,) * 3))
        self.pwm.ultrasonic_samples = ((self.clock.now, 1, 30),)
        self.controller._update_depth_control()
        self.assertEqual(self.controller.speeds, (0,) * 6)
        self.clock.now += 0.2
        self.controller._update_depth_control()
        self.assertEqual(self.controller.speeds, (0,) * 6)
        self.clock.now += 0.31
        self.pwm.ultrasonic_samples = ((self.clock.now, 3, 0),)
        self.controller._update_depth_control()
        self.assertEqual(self.controller.speeds, (0,) * 6)
        self.pwm.ultrasonic_samples = ((self.clock.now, 1, 30),)
        self.controller._update_depth_control()
        self.assertEqual(self.controller.speeds[:2], (30, 30))
        self.assertEqual(self.clock.pauses, [])

    def test_manual_stop_and_link_fault_cancel_automatic_mode(self):
        settings = motors.DepthControl((30,) * 3)
        self.controller.enable_depth_control(settings)
        self.controller.set_speeds((0,) * 6)
        self.assertIsNone(self.controller.depth_control)
        self.controller.enable_depth_control(settings)
        self.pwm.fail_next = True
        with patch.object(self.controller.done, "wait", return_value=False):
            self.controller._heartbeat()
        self.assertIsNone(self.controller.depth_control)
        self.assertEqual(self.controller.speeds, (0,) * 6)
        self.assertFalse(any(pin.value for pin in self.pins.values()))


class DepthControlTests(unittest.TestCase):
    def test_proportional_range_endpoints_and_signed_pair_directions(self):
        settings = motors.DepthControl((100, -80, 60))
        self.assertEqual(settings.speeds(((10, 1, 30), (10, 1, 65), (10, 1, 100)), 10),
                         (100, 100, -40, -40, 0, 0))
        self.assertEqual(settings.speeds(((10, 1, 20), (10, 1, 29), (10, 1, 30)), 10),
                         (100, 100, -80, -80, 60, 60))
        self.assertEqual(settings.speeds(((10, 1, 100), (10, 1, 101), (10, 1, 3000)), 10),
                         (0,) * 6)
        # 4.4 cm = 80%, 6.5 cm = 50%, 8.6 cm = 20% of full speed.
        self.assertEqual(motors.DepthControl().speeds(
            ((10, 1, 44), (10, 1, 65), (10, 1, 86)), 10),
            (80, 80, 50, 50, 20, 20))

    def test_missing_invalid_out_of_range_and_stale_samples_stop_pairs(self):
        settings = motors.DepthControl((30,) * 3)
        for sample in (None, (10, 0, 30), (10, 2, 0), (10, 3, 0),
                       (10, 1, 19), (10, 1, 3001), (8.99, 1, 30), (11, 1, 30)):
            self.assertEqual(settings.speeds((sample,), 10), (0,) * 6, sample)
        self.assertEqual(settings.speeds((), 10), (0,) * 6)

    def test_settings_reject_invalid_speeds(self):
        for speeds in ((30, 30), (101, 30, 30), (-101, 30, 30), (True, 30, 30), (1.5, 30, 30)):
            with self.assertRaises(ValueError):
                motors.DepthControl(speeds)


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

    def test_pwm_map_routes_each_motor_to_its_enable_and_validates_wire_order_ack(self):
        bus = Bus()
        channel_map = (2, 3, 4, 5, 0, 1)
        pwm = motors.I2CPwm(bus, Message, channel_map=channel_map)
        pwm.set_percentages((100, 80, 60, 40, 20, 0))
        self.assertEqual(bus.frames[-1][6:12], bytes((20, 0, 100, 80, 60, 40)))
        self.assertEqual(bus.reads, 1)
        for motor, channel in enumerate(channel_map):
            values = [0] * 6
            values[motor] = 100
            pwm.set_percentages(values)
            expected = bytearray(6)
            expected[channel] = 100
            self.assertEqual(bus.frames[-1][6:12], expected)
        pwm.set_percentages((0,) * 6)
        self.assertEqual(bus.frames[-1][6:12], bytes(6))

    def test_pwm_map_rejects_missing_duplicate_or_invalid_channels(self):
        for channel_map in ((0, 1, 2), (0, 1, 2, 3, 4, 4), (0, 1, 2, 3, 4, 6),
                            (-1, 1, 2, 3, 4, 5), (False, 1, 2, 3, 4, 5)):
            bus = Bus()
            with self.assertRaises(ValueError):
                motors.I2CPwm(bus, Message, channel_map=channel_map)
            self.assertEqual(bus.frames, [])
        self.assertEqual(motors.parse_pwm_map("3,4,5,6,1,2"), (2, 3, 4, 5, 0, 1))
        for text in ("1,2,3", "1,2,3,4,5,5", "0,2,3,4,5,6", "1,2,3,4,5,7", "a,b,c,d,e,f"):
            with self.assertRaises(motors.argparse.ArgumentTypeError):
                motors.parse_pwm_map(text)

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
        self.assertEqual(bus.requested_size, 29)
        self.assertEqual(pwm.ultrasonic_samples[:2], ((self.clock.now, 1, 1234),
                                                (self.clock.now, 1, 2345)))
        first, second = pwm.ultrasonic_samples[:2]
        bus.samples = [(1, 432), (3, 0)]
        bus.corrupt = [True, False]
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples[:2], (first, (self.clock.now, 3, 0)))
        second = pwm.ultrasonic_samples[:2][1]
        bus.corrupt = [False, True]
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples[:2], ((self.clock.now, 1, 432), second))
        first = pwm.ultrasonic_samples[:2][0]
        bus.corrupt = [False, False]
        bus.samples = [(1, 3001), (1, 3000)]
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples[:2], (first, (self.clock.now, 1, 3000)))
        bus.second_present = False
        legacy = motors.I2CPwm(bus, Message)
        legacy.set_percentages((30,) * 6)
        self.assertEqual(legacy.ultrasonic_samples[:2], (None, None))
        bus.samples = [(1, 1234), (1, 2345)]
        legacy.set_percentages((30,) * 6)
        self.assertEqual(legacy.ultrasonic_samples[:2], ((self.clock.now, 1, 1234), None))

    def test_third_sensor_extension_and_bad_or_missing_readings(self):
        class TripleBus(Bus):
            states = [1, 1, 1]
            distances = [321, 1234, 2345]
            corrupt = -1
            count = 3

            def i2c_rdwr(self, message):
                if message.is_read:
                    self.requested_size = len(message)
                super().i2c_rdwr(message)
                if message.is_read:
                    for sensor in range(self.count):
                        block = bytes((0xD1 + sensor, self.states[sensor]))
                        block += self.distances[sensor].to_bytes(2, "little")
                        check = motors.checksum(bytes(message) + block if sensor == 0 else block)
                        message.extend(block + bytes((check ^ int(sensor == self.corrupt),)))

        bus = TripleBus()
        pwm = motors.I2CPwm(bus, Message)
        pwm.set_percentages((30,) * 6)
        self.assertEqual(bus.requested_size, 29)
        self.assertEqual([sample[2] for sample in pwm.ultrasonic_samples], [321, 1234, 2345])
        third = pwm.ultrasonic_samples[2]
        bus.distances = [400, 1400, 2500]
        bus.corrupt = 2
        pwm.set_percentages((30,) * 6)
        self.assertEqual([sample[2] for sample in pwm.ultrasonic_samples[:2]], [400, 1400])
        self.assertEqual(pwm.ultrasonic_samples[2], third)
        bus.corrupt = 0
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples[2][2], 2500)
        bus.corrupt = -1
        for state, mm in ((3, 0), (2, 0), (1, 20), (1, 3000)):
            bus.states = [1, 1, state]
            bus.distances = [400, 1400, mm]
            pwm.set_percentages((30,) * 6)
            self.assertEqual(pwm.ultrasonic_samples[2], (self.clock.now, state, mm))
        third = pwm.ultrasonic_samples[2]
        bus.distances = [400, 1400, 3001]
        pwm.set_percentages((30,) * 6)
        self.assertEqual(pwm.ultrasonic_samples[2], third)
        for count in (1, 2):
            bus.count = count
            legacy = motors.I2CPwm(bus, Message)
            legacy.set_percentages((30,) * 6)
            self.assertIsNone(legacy.ultrasonic_samples[2])
            self.assertEqual(legacy.ultrasonic_samples[0][2], 400)

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
