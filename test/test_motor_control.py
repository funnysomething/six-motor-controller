"""Exercise the real asynchronous protocol and scheduler with a simulated ESP32."""
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace
from raspberry_pi.control_transport import I2CPwm, checksum
from raspberry_pi.control_runtime import MotorController, DIRECTION_PINS
from raspberry_pi.stabilization import VehicleConfig, Attitude, RangeSample
from raspberry_pi import motor_control as motors


class Clock:
    def __init__(self):
        self.now = 10.
    def __call__(self):
        return self.now
    def sleep(self, value):
        self.now += value


class Message(bytearray):
    @classmethod
    def write(cls, address, data):
        m = cls(data); m.is_read = False; return m
    @classmethod
    def read(cls, address, count):
        m = cls(); m.is_read = True; m.requested = count; return m


class Bus:
    def __init__(self):
        self.frames = []
        self.applied = (0,)*6
        self.sequence = bytes(4)
        self.sensor_sequence = 1
        self.age = 1
        self.mm = 1000
        self.state = 1
        self.corrupt_sensor = None
        self.corrupt_prefix = False
        self.fail = False
        self.never_ack = False
        self.version = 2
        self.stale_replies = 0
        self.reads = 0

    def i2c_rdwr(self, message):
        if self.fail:
            raise OSError('link lost')
        if not message.is_read:
            self.frames.append(bytes(message))
            self.applied = tuple(message[6:12])
            self.sequence = bytes(message[2:6])
            return
        self.reads += 1
        sequence = bytes(4) if self.never_ack or self.stale_replies else self.sequence
        self.stale_replies = max(0, self.stale_replies-1)
        prefix = bytes((0x5A,self.version))+sequence+bytes((0,))+bytes(self.applied)
        message.extend(prefix+bytes((checksum(prefix)^self.corrupt_prefix,)))
        for i in range(3):
            if self.version == 2:
                block = bytes((0xD1+i,(self.sensor_sequence << 2)|self.state))+self.mm.to_bytes(2,'little')+bytes((self.age,))
                message.extend(block+bytes((checksum(block) ^ (self.corrupt_sensor == i),)))
            else:
                block = bytes((0xD1+i,self.state))+self.mm.to_bytes(2,'little')
                message.extend(block+bytes((checksum(bytes(message)+block if i == 0 else block),)))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.bus = Bus()
        self.pwm = I2CPwm(self.bus,Message,clock=self.clock)
        self.timer = patch('raspberry_pi.control_transport.time.sleep',self.clock.sleep)
        self.timer.start(); self.addCleanup(self.timer.stop)

    def send(self, values=(0,)*6):
        self.pwm.set_percentages(values)

    def test_wire_mapping_and_checksum(self):
        self.send((10,20,30,40,50,60))
        frame = self.bus.frames[-1]
        self.assertEqual(frame[:2],b'\xa5\x01')
        self.assertEqual(frame[6:12],bytes((40,50,60,10,20,30)))
        self.assertEqual(frame[-1],checksum(frame[:-1]))
        self.assertEqual(len(frame),13)
        self.assertTrue(self.pwm.telemetry_v2)

    def test_poll_is_nonblocking_and_does_not_read_early(self):
        self.pwm.begin((20,)*6)
        self.assertIsNone(self.pwm.poll())
        self.assertEqual(self.bus.reads,0)
        self.clock.now += .006
        self.assertEqual(self.pwm.poll(),(20,)*6)

    def test_poll_waits_for_matching_applied_sequence(self):
        self.bus.stale_replies = 3
        self.send()
        self.assertEqual(self.bus.reads,4)

    def test_ack_timeout_bounded(self):
        self.bus.never_ack = True
        with self.assertRaises(OSError): self.send()
        self.assertLess(self.clock.now,10.31)
        self.assertIsNone(self.pwm.pending)

    def test_invalid_prefix_cannot_refresh_ranges(self):
        self.bus.corrupt_prefix = True
        with self.assertRaises(OSError): self.send()
        self.assertEqual(self.pwm.range_samples,(None,)*3)

    def test_sequence_controls_sample_age(self):
        self.send()
        sample = self.pwm.range_samples[0]
        self.clock.now += .5
        self.send()
        self.assertEqual(self.pwm.range_samples[0],sample)
        self.bus.sensor_sequence += 1
        self.send()
        self.assertGreater(self.pwm.range_samples[0].at,sample.at)

    def test_measurement_age_is_not_receipt_age(self):
        self.bus.age=120
        self.send()
        self.assertGreater(self.clock.now-self.pwm.range_samples[0].at,1.2)

    def test_age_saturation_invalidates_sample(self):
        self.send(); self.bus.age=255; self.send()
        self.assertEqual(self.pwm.range_samples,(None,)*3)

    def test_sensor_checksums_independent(self):
        self.bus.corrupt_sensor=1
        self.send()
        self.assertIsNotNone(self.pwm.range_samples[0])
        self.assertIsNone(self.pwm.range_samples[1])
        self.assertIsNotNone(self.pwm.range_samples[2])

    def test_invalid_echo_and_out_of_range(self):
        self.bus.state=2; self.bus.mm=0; self.send()
        self.assertEqual(self.pwm.range_samples[0].state,2)
        self.bus.state=1; self.bus.mm=4000; self.bus.sensor_sequence+=1; self.send()
        self.assertEqual(self.pwm.range_samples[0].state,2)

    def test_legacy_is_manual_only(self):
        self.bus.version=1; self.send()
        self.assertFalse(self.pwm.telemetry_v2)
        self.assertEqual(self.pwm.range_samples,(None,)*3)
        self.assertEqual(self.pwm.ultrasonic_samples[0][2],1000)

    def test_pending_and_invalid_values_rejected(self):
        for values in ((0,)*5,(101,)*6,(-1,)*6,(True,)*6):
            with self.assertRaises(ValueError): self.pwm.begin(values)
        self.pwm.begin((0,)*6)
        with self.assertRaises(RuntimeError): self.pwm.begin((0,)*6)

    def test_mapping_validation_and_cli(self):
        for value in ('1,2,3','1,2,3,4,5,5','0,2,3,4,5,6'):
            with self.assertRaises(motors.argparse.ArgumentTypeError): motors.parse_pwm_map(value)
        self.assertEqual(motors.parse_pwm_map('4,5,6,1,2,3'),self.pwm.channel_map)
        with self.assertRaises(ValueError): I2CPwm(self.bus,Message,channel_map=(False,1,2,3,4,5))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock(); self.bus=Bus()
        self.timer=patch('raspberry_pi.control_transport.time.sleep',self.clock.sleep)
        self.timer.start(); self.addCleanup(self.timer.stop)
        self.pwm=I2CPwm(self.bus,Message,clock=self.clock)
        self.pins={}; test=self
        class Output:
            def __init__(self,pin,initial_value=False):
                self.pin=pin; self.value=initial_value; self.closed=False
                test.pins[pin]=self
            def _set(self,value):
                if self.value != value:
                    i=DIRECTION_PINS.index(self.pin)
                    channel=test.pwm.channel_map[i]
                    test.assertEqual(test.bus.applied[channel],0,'DIR changed while PWM active')
                self.value=value
            def on(self): self._set(True)
            def off(self): self._set(False)
            def close(self): self.closed=True
        self.controller=MotorController(self.pwm,Output,start_heartbeat=False,clock=self.clock)
        self.addCleanup(self.controller.close)

    def advance(self, seconds):
        for _ in range(round(seconds/.002)):
            self.clock.now+=.002
            self.controller._tick(self.clock.now)

    def test_zero_ack_before_gpio_claim(self):
        self.assertEqual(self.bus.frames[0][6:12],bytes(6))
        self.assertEqual(len(self.pins),6)
        self.assertIsNotNone(self.controller.last_pwm_ack_at)

    def test_all_six_manual_speeds(self):
        values=(60,-50,40,-30,0,100)
        self.controller.set_speeds(values)
        self.advance(.08)
        self.assertEqual(self.controller.speeds,values)
        self.assertEqual(tuple(bool(p.value) for p in self.pins.values()),tuple(x>0 for x in values))

    def test_direction_edges_wait_for_zero_ack(self):
        self.controller.set_speeds((50,)*6); self.advance(.08)
        self.controller.set_speeds((-40,)*6); self.advance(.08)
        self.assertEqual(self.controller.speeds,(0,)*6)
        self.assertTrue(all(p.value for p in self.pins.values()))
        self.advance(.6)
        self.assertEqual(self.controller.speeds,(-40,)*6)

    def test_reversal_does_not_block_other_motor_or_sensor(self):
        self.controller.imu=Mock(); self.controller.imu.readings={}
        self.controller.set_speeds((50,50,0,0,0,0)); self.advance(.08)
        self.controller.set_speeds((-50,80,0,0,0,0)); self.advance(.1)
        self.assertEqual(self.controller.speeds[:2],(0,80))
        self.assertTrue(self.controller.coasting[0])
        self.assertGreater(self.controller.imu.poll_due.call_count,10)

    def test_stop_cancels_pending_reversal(self):
        self.controller.set_speeds((50,)*6); self.advance(.08)
        self.controller.set_speeds((-50,)*6); self.advance(.1)
        self.controller.set_speeds((0,)*6); self.advance(.8)
        self.assertEqual(self.controller.speeds,(0,)*6)
        self.assertFalse(any(self.controller.coasting))

    def test_latest_target_wins_while_ack_pending(self):
        self.controller.set_speeds((50,)*6); self.advance(.002)
        self.controller.set_speeds((0,)*6); self.advance(.1)
        self.assertEqual(self.controller.speeds,(0,)*6)

    def test_recovery_sends_zero_only_and_requires_manual_reapply(self):
        self.controller.set_speeds((60,)*6); self.advance(.08)
        self.bus.fail=True; self.advance(.08)
        self.assertTrue(self.controller.recovering)
        self.assertTrue(all(p.value for p in self.pins.values()))
        count=len(self.bus.frames)
        self.bus.fail=False; self.advance(.5)
        self.assertFalse(self.controller.recovering)
        self.assertEqual(self.controller.speeds,(0,)*6)
        self.assertTrue(all(frame[6:12] == bytes(6) for frame in self.bus.frames[count:]))
        self.assertFalse(self.controller.automatic)

    def test_exhausted_recovery_holds_dir_until_watchdog(self):
        self.controller.set_speeds((60,)*6); self.advance(.08)
        self.bus.fail=True; self.advance(2.5)
        self.assertIsNotNone(self.controller.fault)
        self.assertTrue(all(p.value for p in self.pins.values()))
        # Simulate the independent ESP32 watchdog before DIR is released.
        self.bus.applied=(0,)*6
        self.advance(1.2)
        self.assertFalse(any(p.value for p in self.pins.values()))
        self.bus.fail=False

    def test_automatic_requires_configuration_and_imu(self):
        with self.assertRaises(ValueError): self.controller.enable_stabilization()

    def test_bad_manual_input_never_reaches_transport(self):
        before=len(self.bus.frames)
        for values in ((1,2),(101,)*6,(float('nan'),)*6):
            with self.assertRaises(ValueError): self.controller.set_speeds(values)
        self.assertEqual(len(self.bus.frames),before)

    def test_scheduler_deadline_disables_auto(self):
        self.controller.stabilizer=Mock(active=True)
        self.controller.last_tick=self.clock.now
        self.advance(.002)
        self.controller.stabilizer.stop.side_effect=lambda reason:setattr(self.controller.stabilizer,'active',False)
        self.clock.now+=.2
        self.controller._tick(self.clock.now)
        self.assertEqual(self.controller.requested,(0,)*6)

    def test_manual_commands_exit_auto(self):
        self.controller.stabilizer=Mock(active=True)
        self.controller.stabilizer.stop.side_effect=lambda reason:setattr(self.controller.stabilizer,'active',False)
        self.controller.set_speeds((40,0,0,0,0,0))
        self.controller.stabilizer.stop.assert_called()
        self.advance(.08)
        self.assertEqual(self.controller.speeds,(40,0,0,0,0,0))

    def test_single_motor_preserves_other_requested_values(self):
        self.controller.set_speeds((10,20,30,40,50,60))
        self.controller.set_motor(2,-30); self.advance(.08)
        self.assertEqual(self.controller.speeds,(10,-30,30,40,50,60))

    def test_close_acknowledges_zero_before_release(self):
        self.controller.set_speeds((40,)*6); self.advance(.08)
        self.controller.close()
        self.assertEqual(self.bus.applied,(0,)*6)
        self.assertTrue(all(p.closed for p in self.pins.values()))

    def prepare_auto(self):
        from raspberry_pi.stabilization import OrientationEstimator
        from raspberry_pi.sen0140 import ImuReading
        try:
            import imufusion
        except ImportError:
            self.skipTest('Install requirements-control.txt for estimator integration')
        class Imu:
            def __init__(self):
                self.readings={}
                self.next=0.
                self.gyro=(0,0,0)
                self.mag=(1,0,0)
            def poll_due(self, now):
                if now >= self.next:
                    self.next=now+.02
                    self.readings={k:ImuReading(now,v) for k,v in
                                   (('accel',(0,0,-1)),('gyro',self.gyro),('mag',self.mag))}
        self.controller.config=VehicleConfig(verified=True,magnetic_verified=True)
        self.controller.estimator=OrientationEstimator(self.controller.config)
        self.controller.imu=Imu()
        self.auto_advance(7.)
        self.controller.enable_stabilization()
        self.assertTrue(self.controller.automatic)

    def auto_advance(self, seconds):
        for _ in range(round(seconds/.002)):
            self.clock.now+=.002
            self.bus.sensor_sequence=int((self.clock.now-10)/.3)%64
            self.controller._tick(self.clock.now)

    def test_full_runtime_clearance_correction_and_compass_fallback(self):
        self.prepare_auto()
        self.assertEqual(self.controller.stabilizer.yaw_mode,'heading')
        self.bus.mm=800
        self.auto_advance(.5)
        self.assertTrue(all(self.controller.requested[i]>0 for i in (2,3,4)))
        self.controller.imu.mag=(0,1,0)
        self.controller.imu.gyro=(0,0,5)
        self.auto_advance(1.4)
        self.assertTrue(self.controller.automatic)
        self.assertEqual(self.controller.stabilizer.yaw_mode,'damping')
        self.assertGreater(self.controller.requested[0],0)
        self.assertEqual(self.controller.requested[5],0)

    def test_full_runtime_stale_range_stops_and_never_auto_restarts(self):
        self.prepare_auto()
        self.bus.mm=800
        self.auto_advance(.4)
        self.bus.age=150
        self.auto_advance(.4)
        self.assertFalse(self.controller.automatic)
        self.assertEqual(self.controller.requested,(0,)*6)
        self.bus.age=1
        self.auto_advance(.6)
        self.assertFalse(self.controller.automatic)
        self.assertEqual(self.controller.speeds,(0,)*6)

    def test_full_runtime_link_recovery_disables_auto(self):
        self.prepare_auto()
        self.bus.fail=True
        self.auto_advance(.1)
        self.assertFalse(self.controller.automatic)
        self.bus.fail=False
        self.auto_advance(.8)
        self.assertFalse(self.controller.recovering)
        self.assertFalse(self.controller.automatic)
        self.assertEqual(self.controller.speeds,(0,)*6)

    def test_invalid_command_parser(self):
        self.assertEqual(motors.parse_command('m 2 -50'),('motor',(2,-50)))
        self.assertIsNone(motors.parse_command('m 101'))
        self.assertEqual(motors.parse_command('s'),('s',()))
