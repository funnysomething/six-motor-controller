import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from raspberry_pi.stabilization import (VehicleConfig, Calibration, Attitude, RangeSample,
    Stabilizer, Effort, Allocator, OrientationEstimator, clearance, wrap)
from raspberry_pi.sen0140 import ImuReading


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.c = VehicleConfig(verified=True)
        self.a = Attitude(10., 0., 0., 179., (0.,0.,0.), True, True, 5., 10.)
        self.s = RangeSample(10., 1, 1, 1.)
        self.control = Stabilizer(self.c)
        self.control.enable(self.a, self.s, 10., True)

    def update(self, now=10.05, **changes):
        attitude = replace(self.a, at=now, **changes)
        return self.control.update(attitude, self.s, now)

    def test_unverified_config_blocks_arming(self):
        with self.assertRaises(ValueError):
            Stabilizer(VehicleConfig())

    def test_bad_config_rejected(self):
        for change in ({'pwm_limit':float('nan')}, {'depth_sensor':4}, {'rate_ki':-1},
                       {'accel':Calibration(body_rotation=((1,0,0),(0,1,0),(0,0,-1)))},
                       {'mag':Calibration(matrix=((0,0,0),)*3)}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                VehicleConfig(**change).validate()

    def test_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'vehicle.json'
            self.c.save(path)
            self.assertEqual(VehicleConfig.load(path).motor_polarity, [1]*6)

    def test_status_v2_required(self):
        with self.assertRaises(ValueError):
            self.control.enable(self.a, self.s, 10., False)

    def test_axis_signs(self):
        mixer = Allocator(VehicleConfig(pwm_limit=100))
        self.assertEqual(mixer.mix(Effort(yaw=.5)), (-50,50,0,0,0,0))
        self.assertEqual(mixer.mix(Effort(upward=1)), (0,0,45,45,100,0))
        self.assertEqual(mixer.mix(Effort(roll=1)), (0,0,-50,50,0,0))
        self.assertEqual(mixer.mix(Effort(pitch=1)), (0,0,50,50,-100,0))
        with self.assertRaises(ValueError):
            mixer.mix(Effort(lateral=1))

    def test_attitude_priority_and_independent_yaw_limit(self):
        mixer = Allocator(VehicleConfig(pwm_limit=100))
        values = mixer.mix(Effort(upward=2, pitch=1, yaw=2))
        self.assertEqual(values, (-100,100,100,100,11,0))
        self.assertTrue(mixer.limited)

    def test_polarity_and_asymmetric_gain(self):
        mixer = Allocator(VehicleConfig(pwm_limit=100, motor_polarity=(-1,1,1,1,1,1),
                                       reverse_gain=(2,1,1,1,1,1)))
        self.assertEqual(mixer.mix(Effort(yaw=.5))[:2], (100,50))

    def test_clearance_counteracts_both_directions(self):
        self.s = replace(self.s, metres=.8)
        values = self.update()
        self.assertTrue(all(values[i] > 0 for i in (2,3,4)))
        self.s = replace(self.s, sequence=2, at=10.1, metres=1.2)
        values = self.update(10.1)
        self.assertTrue(all(values[i] < 0 for i in (2,3,4)))

    def test_roll_and_pitch_counter_signs(self):
        values = self.update(roll=10)
        self.assertGreater(values[2], values[3])
        self.assertEqual(values[5], 0)
        values = self.update(10.1, pitch=10)
        self.assertLess(values[2], 0)
        self.assertGreater(values[4], 0)

    def test_shortest_heading_path(self):
        values = self.update(heading=-179)
        self.assertGreater(values[0], 0)
        self.assertLess(values[1], 0)
        self.assertEqual(wrap(-358), 2)

    def test_bad_compass_degrades_without_losing_leveling(self):
        self.update(magnetic_ok=False)
        for n in range(1,22):
            now = 10.05+n*.05
            self.s = replace(self.s, at=now, sequence=n+1)
            self.update(now, magnetic_ok=False, roll=5, rates=(0,0,5))
        self.assertTrue(self.control.active)
        self.assertEqual(self.control.yaw_mode, 'damping')
        self.assertGreater(self.control.requested[0], 0)
        self.assertGreater(self.control.requested[2], self.control.requested[3])
        self.s = replace(self.s, at=11.2)
        self.update(11.2, magnetic_ok=True)
        self.assertEqual(self.control.yaw_mode, 'damping')
        self.control.recapture_heading(replace(self.a, at=11.2), 11.2)
        self.assertEqual(self.control.yaw_mode, 'heading')

    def test_no_compass_at_start_allows_damping(self):
        self.control.enable(replace(self.a, magnetic_ok=False), self.s, 10., True)
        self.assertEqual(self.control.yaw_mode, 'damping')

    def test_recapture_requires_three_seconds(self):
        with self.assertRaises(ValueError):
            self.control.recapture_heading(replace(self.a, magnetic_good_since=9.), 10.)

    def test_stale_required_inputs_latch_stop(self):
        for attitude, sample in ((replace(self.a, at=9.),self.s),
                                 (self.a,replace(self.s, at=8.)),
                                 (self.a,replace(self.s,state=2)),
                                 (replace(self.a,roll=21),self.s)):
            self.control.enable(self.a, self.s, 10., True)
            with self.assertRaises(ValueError):
                self.control.update(attitude,sample,10.05)
            self.assertFalse(self.control.active)
            self.assertEqual(self.control.requested,(0,)*6)

    def test_range_velocity_only_on_new_sequence(self):
        self.update()
        self.s = replace(self.s, metres=.5)
        self.update(10.1)
        self.assertEqual(self.control.velocity,0.)

    def test_constrained_integrator_freezes(self):
        self.c.rate_ki = .1
        self.control.update(replace(self.a,at=10.05,roll=10),self.s,10.05,True)
        self.assertEqual(self.control.integral,[0.]*4)

    def test_slant_and_mount_offset(self):
        c = replace(self.c, sensor_offset_m=(0,0,.1))
        result = clearance(self.s, replace(self.a,roll=10,pitch=15),c,10.)
        self.assertAlmostEqual(result,1.1*math.cos(math.radians(10))*math.cos(math.radians(15)))


class EstimatorTests(unittest.TestCase):
    def setUp(self):
        try:
            import imufusion
        except ImportError:
            self.skipTest('Install requirements-control.txt for real Fusion tests')
        self.c = VehicleConfig(magnetic_verified=True)
        self.e = OrientationEstimator(self.c)

    def feed(self, t, mag=(1.,0.,0.), gyro=(0.,0.,0.), accel=(0.,0.,-1.)):
        readings = {k:ImuReading(t,v) for k,v in (('accel',accel),('gyro',gyro),('mag',mag))}
        return self.e.update(readings,t)

    def settle(self):
        for n in range(200):
            a = self.feed(1+n*.02)
        self.assertTrue(a.ready)
        return a

    def test_stationary_ned_level(self):
        a = self.settle()
        self.assertAlmostEqual(a.roll,0.,places=3)
        self.assertAlmostEqual(a.pitch,0.,places=3)
        self.assertTrue(a.magnetic_ok)

    def test_bad_magnetometer_cannot_corrupt_tilt(self):
        self.settle()
        a = self.feed(5.,mag=(0.,1.,0.))
        self.assertFalse(a.magnetic_ok)
        self.assertAlmostEqual(a.heading,0.,places=3)
        self.assertAlmostEqual(a.roll,0.,places=3)
        for n in range(1,301):
            a = self.feed(5.+n*.02,mag=(0.,1.,0.))
        self.assertFalse(a.magnetic_ok)
        self.assertAlmostEqual(a.heading,0.,places=3)
        self.assertAlmostEqual(a.roll,0.,places=3)

    def test_magnitude_rejection(self):
        self.settle()
        self.assertFalse(self.feed(5.,mag=(4.,0.,0.)).magnetic_ok)

    def test_gyro_propagates_yaw_without_compass(self):
        self.settle()
        for n in range(50):
            a=self.feed(5.+n*.02,mag=(4.,0.,0.),gyro=(0,0,10))
        self.assertAlmostEqual(a.heading,10.,delta=.1)

    def test_stale_and_sampling_gap(self):
        self.settle()
        with self.assertRaises(ValueError):
            self.feed(6.)

    def test_gyro_bias_applied(self):
        self.c.gyro.bias=(0,0,2)
        a=self.feed(1.,gyro=(0,0,2))
        self.assertEqual(a.rates,(0,0,0))

    def test_zero_and_saturated_acceleration_rejected(self):
        for accel in ((0,0,0),(2,0,-1),(float('nan'),0,-1)):
            with self.assertRaises(ValueError):
                self.feed(1.,accel=accel)
