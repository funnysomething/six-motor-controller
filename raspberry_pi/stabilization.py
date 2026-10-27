"""Hardware-independent BlueROV1 control, calibration and orientation estimation.

Body axes are forward/right/down. Angular values are degrees and deg/s;
clearance is metres, and movement efforts are normalized [-1, 1].
"""
from dataclasses import dataclass, field, asdict
import json
import math
from pathlib import Path


def clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def wrap(angle):
    return (angle + 180.0) % 360.0 - 180.0


def finite(values):
    return all(math.isfinite(x) for x in values)


def transform(matrix, values):
    return tuple(sum(a * b for a, b in zip(row, values)) for row in matrix)


IDENTITY = ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))


@dataclass
class Calibration:
    bias: tuple = (0., 0., 0.)
    matrix: tuple = IDENTITY
    body_rotation: tuple = IDENTITY

    def validate(self):
        for matrix in (self.matrix, self.body_rotation):
            if len(matrix) != 3 or any(len(row) != 3 or not finite(row) for row in matrix):
                raise ValueError("Calibration requires finite 3x3 matrices")
        if len(self.bias) != 3 or not finite(self.bias):
            raise ValueError("Calibration bias must have three finite values")
        r = self.body_rotation
        for i in range(3):
            for j in range(3):
                if abs(sum(r[i][k] * r[j][k] for k in range(3)) - (i == j)) > 1e-5:
                    raise ValueError("Body rotation must be orthonormal")
        det = sum(r[0][i] * (r[1][(i+1)%3] * r[2][(i+2)%3]
                            - r[1][(i+2)%3] * r[2][(i+1)%3]) for i in range(3))
        if det < .999:
            raise ValueError("Body rotation must preserve handedness")
        # Reject singular sensitivity/soft-iron matrices as invalid calibration.
        m = self.matrix
        determinant = sum(m[0][i] * (m[1][(i+1)%3]*m[2][(i+2)%3]
                                    - m[1][(i+2)%3]*m[2][(i+1)%3]) for i in range(3))
        if determinant <= 0:
            raise ValueError("Calibration sensitivity matrix must have positive determinant")

    def apply(self, values):
        return transform(self.body_rotation, transform(self.matrix,
                         tuple(x - b for x, b in zip(values, self.bias))))


@dataclass
class VehicleConfig:
    verified: bool = False
    magnetic_verified: bool = False
    depth_sensor: int = 1
    sensor_offset_m: tuple = (0., 0., 0.)
    motor_polarity: tuple = (1, 1, 1, 1, 1, 1)
    motor_gain: tuple = (1.,) * 6
    reverse_gain: tuple = (1.,) * 6
    pwm_limit: float = 20.
    reversal_seconds: float = .5
    level_roll_deg: float = 0.
    level_pitch_deg: float = 0.
    angle_kp: float = 1.
    rate_kp: float = .02
    rate_ki: float = 0.
    clearance_kp: float = 1.
    clearance_ki: float = 0.
    clearance_kd: float = .3
    buoyancy_trim: float = 0.
    max_rate_dps: float = 10.
    target_slew_mps: float = .05
    heading_slew_dps: float = 5.
    mag_magnitude_tolerance: float = .2
    mag_innovation_deg: float = 15.
    mag_step_deg: float = 10.
    accel: Calibration = field(default_factory=Calibration)
    gyro: Calibration = field(default_factory=Calibration)
    mag: Calibration = field(default_factory=Calibration)

    def validate(self, armed=False):
        numeric = (self.pwm_limit, self.reversal_seconds, self.level_roll_deg,
                   self.level_pitch_deg, self.angle_kp, self.rate_kp, self.rate_ki,
                   self.clearance_kp, self.clearance_ki, self.clearance_kd,
                   self.buoyancy_trim, self.max_rate_dps, self.target_slew_mps,
                   self.heading_slew_dps, self.mag_magnitude_tolerance,
                   self.mag_innovation_deg, self.mag_step_deg)
        if not finite(numeric):
            raise ValueError("Control settings must be finite")
        if type(self.depth_sensor) is not int or not 1 <= self.depth_sensor <= 3:
            raise ValueError("Select vertical ultrasonic sensor 1, 2, or 3")
        if (len(self.sensor_offset_m) != 3 or not finite(self.sensor_offset_m)
                or len(self.motor_polarity) != 6 or any(x not in (-1, 1) for x in self.motor_polarity)):
            raise ValueError("Verify sensor offset and six motor polarities")
        for gains in (self.motor_gain, self.reverse_gain):
            if len(gains) != 6 or not finite(gains) or any(not .1 <= x <= 10 for x in gains):
                raise ValueError("Six motor gains must be between 0.1 and 10")
        if not 0 < self.pwm_limit <= 100 or not 0 <= self.reversal_seconds <= 5:
            raise ValueError("PWM limit must be 0-100; reversal delay 0-5 seconds")
        if any(x < 0 for x in (self.angle_kp, self.rate_kp, self.rate_ki,
                              self.clearance_kp, self.clearance_ki, self.clearance_kd)):
            raise ValueError("Controller gains must be nonnegative")
        if (not 0 < self.max_rate_dps <= 45 or not 0 < self.target_slew_mps <= .5
                or not 0 < self.heading_slew_dps <= 45 or abs(self.buoyancy_trim) > 1
                or abs(self.level_roll_deg) > 10 or abs(self.level_pitch_deg) > 10
                or not 0 < self.mag_magnitude_tolerance <= .5
                or not 0 < self.mag_innovation_deg <= 45 or not 0 < self.mag_step_deg <= 45):
            raise ValueError("Control limits outside permitted commissioning range")
        for calibration in (self.accel, self.gyro, self.mag):
            calibration.validate()
        if armed and self.verified is not True:
            raise ValueError("Verify wiring, thrust polarity, downward sensor, IMU axes and calibration first")

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        for key in ("accel", "gyro", "mag"):
            if key in data:
                data[key] = Calibration(**data[key])
        config = cls(**data)
        config.validate()
        return config

    def save(self, path):
        self.validate()
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")


@dataclass(frozen=True)
class RangeSample:
    at: float
    sequence: int
    state: int
    metres: float


@dataclass(frozen=True)
class Attitude:
    at: float
    roll: float
    pitch: float
    heading: float
    rates: tuple
    ready: bool
    magnetic_ok: bool
    magnetic_good_since: float = None
    last_magnetic_at: float = None


def clearance(sample, attitude, config, now):
    if (sample is None or sample.state != 1 or not finite((sample.at, sample.metres))
            or not 0 <= now - sample.at <= 1. or not .02 <= sample.metres <= 3.):
        raise ValueError("Vertical range invalid or stale")
    r, p = math.radians(attitude.roll), math.radians(attitude.pitch)
    vertical_row = (-math.sin(p), math.sin(r)*math.cos(p), math.cos(r)*math.cos(p))
    return sample.metres * vertical_row[2] + sum(a*b for a, b in zip(vertical_row, config.sensor_offset_m))


class OrientationEstimator:
    """Magnetometer never enters the six-axis leveling filter.

    Heading is integrated using Euler yaw rate from calibrated body rates and
    corrected slowly only by independently gated tilt-compensated compass data.
    Fusion's no-magnetometer filter handles quaternion leveling and accel rejection.
    """
    def __init__(self, config):
        import imufusion
        import numpy as np
        self.np = np
        self.config = config
        self.filter = imufusion.Ahrs()
        self.filter.settings = imufusion.Settings(imufusion.CONVENTION_NED, .5, 2000, 10, 0, 250)
        self.previous = None
        self.heading = None
        self.last_mag_sequence = None
        self.last_mag_heading = None
        self.last_mag_at = None
        self.good_since = None
        self.latest = None
        self.started_at = None

    def update(self, readings, now):
        accel, gyro, mag = (readings.get(k) for k in ("accel", "gyro", "mag"))
        if any(x is None or x.error or not 0 <= now-x.at <= .1 for x in (accel, gyro)):
            return self.latest
        if self.previous == gyro.at:
            return self.latest
        a, g = self.config.accel.apply(accel.values), self.config.gyro.apply(gyro.values)
        if not finite((*a, *g)):
            raise ValueError("Nonfinite IMU measurement")
        if math.sqrt(sum(x*x for x in a)) < .05 or any(abs(x) >= 1.99 for x in a):
            raise ValueError("Accelerometer zero or saturated; verify calibration and sensor")
        dt = .02 if self.previous is None else gyro.at-self.previous
        if not 0 < dt <= .1:
            raise ValueError("IMU sampling deadline missed; estimator restart required")
        self.previous = gyro.at
        self.started_at = now if self.started_at is None else self.started_at
        self.filter.update_no_magnetometer(self.np.array(g), self.np.array(a), dt)
        roll, pitch, _ = (float(x) for x in self.filter.quaternion.to_euler())
        r, p = math.radians(roll), math.radians(pitch)
        yaw_rate = (g[1]*math.sin(r) + g[2]*math.cos(r)) / max(.1, math.cos(p))
        if self.heading is not None:
            self.heading = wrap(self.heading + yaw_rate*dt)
        if mag is not None and mag.at != self.last_mag_sequence:
            self.last_mag_sequence = mag.at
            trusted = False
            if not mag.error and 0 <= now-mag.at <= .3 and self.config.magnetic_verified:
                m = self.config.mag.apply(mag.values)
                magnitude = math.sqrt(sum(x*x for x in m))
                # Calibration normalizes the undisturbed field magnitude to 1.
                mx = m[0]*math.cos(p) + m[1]*math.sin(r)*math.sin(p) + m[2]*math.cos(r)*math.sin(p)
                my = m[1]*math.cos(r) - m[2]*math.sin(r)
                magnetic_heading = math.degrees(math.atan2(-my, mx))
                innovation = 0 if self.heading is None else wrap(magnetic_heading-self.heading)
                step = (0 if self.last_mag_heading is None else
                        wrap(magnetic_heading-self.last_mag_heading))
                trusted = (finite(m) and abs(magnitude-1) <= self.config.mag_magnitude_tolerance
                           and math.hypot(mx, my) > .1
                           and abs(innovation) <= self.config.mag_innovation_deg
                           and abs(step) <= self.config.mag_step_deg)
                self.last_mag_heading = magnetic_heading
                if trusted:
                    self.heading = (magnetic_heading if self.heading is None else
                                    wrap(self.heading + .05*innovation))
            if trusted:
                self.good_since = now if self.good_since is None else self.good_since
                self.last_mag_at = now
            else:
                self.good_since = None
        magnetic_ok = self.last_mag_at is not None and now-self.last_mag_at <= .3 and self.good_since is not None
        if not magnetic_ok:
            self.good_since = None
        flags = self.filter.flags
        ready = (now-self.started_at >= 3 and not flags.initialising
                 and not flags.angular_rate_recovery and not flags.acceleration_recovery)
        self.latest = Attitude(gyro.at, roll, pitch, self.heading or 0., g, ready,
                               magnetic_ok, self.good_since, self.last_mag_at)
        return self.latest


@dataclass(frozen=True)
class Effort:
    forward: float = 0.
    lateral: float = 0.
    upward: float = 0.
    roll: float = 0.
    pitch: float = 0.
    yaw: float = 0.


class Allocator:
    def __init__(self, config):
        self.config = config
        self.limited = False

    def mix(self, effort):
        if not finite(tuple(asdict(effort).values())):
            raise ValueError("Nonfinite movement effort")
        if effort.forward or effort.lateral:
            raise ValueError("Translation is unavailable in stage-one stabilization")
        c = self.config
        attitude = [0., 0., -.5*effort.roll+.5*effort.pitch,
                    .5*effort.roll+.5*effort.pitch, -effort.pitch, 0.]
        def usage(i, value):
            return abs(value)*(c.motor_gain[i] if value >= 0 else c.reverse_gain[i])
        peak = max(1., *(usage(i, attitude[i]) for i in (2,3,4)))
        attitude = [x/peak for x in attitude]
        heave = (0., 0., .45*effort.upward, .45*effort.upward, effort.upward, 0.)
        scale = 1.
        for i in (2,3,4):
            a, h = attitude[i], heave[i]
            if h:
                bound = 1/c.motor_gain[i] if h > 0 else -1/c.reverse_gain[i]
                scale = min(scale, max(0., (bound-a)/h))
        values = [attitude[i]+heave[i]*scale for i in range(6)]
        yaw = [-effort.yaw, effort.yaw]
        yaw_peak = max(1., *(usage(i, yaw[i]) for i in (0,1)))
        values[:2] = [x/yaw_peak for x in yaw]
        self.limited = peak > 1 or scale < 1 or yaw_peak > 1
        return tuple(round(clamp(x*(c.motor_gain[i] if x >= 0 else c.reverse_gain[i]))
                           *c.pwm_limit*c.motor_polarity[i]) for i, x in enumerate(values))


class Stabilizer:
    def __init__(self, config):
        config.validate(armed=True)
        self.config = config
        self.allocator = Allocator(config)
        self.active = False
        self.reason = "Stopped"
        self.yaw_mode = "damping"
        self.integral = [0.] * 4
        self.last_at = None
        self.range_sequence = None
        self.range_at = None
        self.last_clearance = None
        self.velocity = 0.
        self.bad_mag_at = None
        self.magnetic_loss_at = None
        self.requested = (0,) * 6
        self.measured_clearance = None

    def _health(self, attitude, sample, now):
        if (attitude is None or not finite((attitude.at, attitude.roll, attitude.pitch,
                                           attitude.heading, *attitude.rates))
                or not attitude.ready or not 0 <= now-attitude.at <= .1):
            raise ValueError("IMU stale, initializing, or invalid")
        if abs(attitude.roll) > 20 or abs(attitude.pitch) > 20:
            raise ValueError("Tilt exceeds 20 degree clearance-control limit")
        return clearance(sample, attitude, self.config, now)

    def enable(self, attitude, sample, now, telemetry_v2):
        if not telemetry_v2:
            raise ValueError("Automatic control requires ESP32 status v2 firmware")
        measured = self._health(attitude, sample, now)
        if not .02 <= measured <= 3:
            raise ValueError("Corrected clearance outside 2-300 cm")
        self.target = self.goal = measured
        self.heading_target = self.heading_goal = attitude.heading
        self.last_at = now
        self.integral = [0.] * 4
        self.range_sequence = None
        self.range_at = None
        self.last_clearance = None
        self.velocity = 0.
        self.bad_mag_at = None
        self.magnetic_loss_at = None
        self.active = True
        self.yaw_mode = "damping"
        self.reason = "Clearance and leveling active; yaw damping"
        if (attitude.magnetic_ok and attitude.magnetic_good_since is not None
                and now-attitude.magnetic_good_since >= 3):
            self.recapture_heading(attitude, now)

    def recapture_heading(self, attitude, now):
        if (not self.active or attitude is None or not attitude.magnetic_ok
                or attitude.magnetic_good_since is None or now-attitude.magnetic_good_since < 3
                or not 0 <= now-attitude.at <= .1):
            raise ValueError("Heading capture needs three seconds of healthy magnetic data")
        self.heading_target = self.heading_goal = attitude.heading
        self.yaw_mode = "heading"
        self.bad_mag_at = None
        self.magnetic_loss_at = None
        self.integral[2] = 0.

    def set_targets(self, clearance_m, heading_deg=None):
        if not math.isfinite(clearance_m) or not .02 <= clearance_m <= 3:
            raise ValueError("Use clearance 2-300 cm and a finite heading")
        if heading_deg is not None:
            if not math.isfinite(heading_deg) or self.yaw_mode != "heading":
                raise ValueError("Recapture healthy heading before editing heading target")
            self.heading_goal = wrap(heading_deg)
        self.goal = clearance_m

    def stop(self, reason="Stopped"):
        self.active = False
        self.reason = reason
        self.integral = [0.] * 4
        self.requested = (0,) * 6

    def update(self, attitude, sample, now, constrained=False):
        if not self.active:
            return (0,) * 6
        try:
            measured = self._health(attitude, sample, now)
            dt = now-self.last_at
            if not 0 < dt <= .15:
                raise ValueError("Control scheduler deadline missed")
            self.last_at = now
            c = self.config
            self.target += clamp(self.goal-self.target, -c.target_slew_mps*dt, c.target_slew_mps*dt)
            self.heading_target = wrap(self.heading_target + clamp(wrap(self.heading_goal-self.heading_target),
                                      -c.heading_slew_dps*dt, c.heading_slew_dps*dt))
            if self.range_sequence != sample.sequence:
                if self.range_at is not None and sample.at > self.range_at:
                    speed = (measured-self.last_clearance)/(sample.at-self.range_at)
                    self.velocity += .3*(clamp(speed, -.5, .5)-self.velocity)
                self.range_at, self.last_clearance = sample.at, measured
                self.range_sequence = sample.sequence
            self.measured_clearance = measured
            if self.yaw_mode == "heading":
                if attitude.magnetic_ok:
                    self.bad_mag_at = None
                else:
                    if self.bad_mag_at is None:
                        self.bad_mag_at = (attitude.last_magnetic_at
                                           if attitude.last_magnetic_at is not None else now)
                    if now-self.bad_mag_at >= 1.:
                        self.yaw_mode = "damping"
                        self.magnetic_loss_at = now
                        self.integral[2] = 0.
            desired_rates = [clamp(c.angle_kp*(c.level_roll_deg-attitude.roll), -c.max_rate_dps, c.max_rate_dps),
                             clamp(c.angle_kp*(c.level_pitch_deg-attitude.pitch), -c.max_rate_dps, c.max_rate_dps),
                             clamp(c.angle_kp*wrap(self.heading_target-attitude.heading), -c.max_rate_dps, c.max_rate_dps)
                             if self.yaw_mode == "heading" else 0.]
            r, p = math.radians(attitude.roll), math.radians(attitude.pitch)
            dr, dp, dy = desired_rates
            body_rates = (dr-dy*math.sin(p), dp*math.cos(r)+dy*math.sin(r)*math.cos(p),
                          -dp*math.sin(r)+dy*math.cos(r)*math.cos(p))
            errors = [body_rates[i]-attitude.rates[i] for i in range(3)] + [self.target-measured]
            freeze = constrained or self.allocator.limited
            candidate = [clamp(old + (0 if freeze else error*dt))
                         for old, error in zip(self.integral, errors)]
            angular = [c.rate_kp*errors[i]+c.rate_ki*candidate[i] for i in range(3)]
            h = c.clearance_kp*errors[3]+c.clearance_ki*candidate[3]-c.clearance_kd*self.velocity+c.buoyancy_trim
            output = self.allocator.mix(Effort(upward=h, roll=angular[0], pitch=angular[1], yaw=angular[2]))
            if not self.allocator.limited and not constrained:
                self.integral = candidate
            self.reason = (f"Clearance {measured:.3f}/{self.target:.3f} m; "
                           f"roll {attitude.roll:+.1f}/{c.level_roll_deg:+.1f}; "
                           f"pitch {attitude.pitch:+.1f}/{c.level_pitch_deg:+.1f}; "
                           f"heading {attitude.heading:+.1f}/{self.heading_target:+.1f}; yaw {self.yaw_mode}"
                           + (" (gyro bridge)" if self.yaw_mode == "heading" and not attitude.magnetic_ok else ""))
            self.requested = output
            return output
        except Exception as error:
            self.stop(str(error))
            raise
