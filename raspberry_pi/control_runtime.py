"""Single-owner bus scheduler; no sleeps during acknowledgements or reversals."""
import json
import threading
import time

if __package__:
    from .stabilization import OrientationEstimator, Stabilizer
else:
    from stabilization import OrientationEstimator, Stabilizer

DIRECTION_PINS = (26, 19, 13, 6, 5, 11)
ZERO = (0,)*6


class MotorController:
    def __init__(self, pwm, output_factory, start_heartbeat=True, imu=None,
                 trace=False, config=None, log=None, clock=time.monotonic):
        self.pwm, self.imu, self.trace = pwm, imu, trace
        self.clock, self.config, self.log = clock, config, log
        self.outputs = []
        self.speeds = self.requested = ZERO
        self.last_directions = [0]*6
        self.stopped_at = [float('-inf')]*6
        self.wire_directions = [0]*6
        self.coasting = [False]*6
        self.pending_directions = None
        self.last_pwm_ack_at = None
        self.output_sample = None
        self.fault = None
        self.link_notice = ''
        self.recovering = False
        self.recovery_attempt = 0
        self.retry_at = 0.
        self.release_at = None
        self.stabilizer = None
        self.estimator = None
        self.attitude = None
        self.control_notice = 'Manual mode'
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.closed = False
        self.worker = None
        self.events = []
        self.next_pwm = self.next_control = 0.
        self.last_tick = None
        self.last_logged = {}
        if config is not None:
            config.validate()
            try:
                self.estimator = OrientationEstimator(config)
            except ImportError:
                self.control_notice = 'Install raspberry_pi/requirements-control.txt to enable stabilization'
        # GPIO is claimed only after acknowledged zero PWM.
        try:
            pwm.set_percentages(ZERO)
            self.last_pwm_ack_at = clock()
            for pin in DIRECTION_PINS:
                self.outputs.append(output_factory(pin, initial_value=False))
            self._record_outputs()
            if start_heartbeat:
                self.worker = threading.Thread(target=self._heartbeat, daemon=True)
                self.worker.start()
        except BaseException:
            for output in self.outputs:
                for cleanup in (output.off, output.close):
                    try:
                        cleanup()
                    except Exception:
                        pass
            raise

    @property
    def ultrasonic_samples(self):
        return self.pwm.ultrasonic_samples

    @property
    def ultrasonic_sample(self):
        return self.ultrasonic_samples[0]

    @property
    def automatic(self):
        return self.stabilizer is not None and self.stabilizer.active

    def _record_outputs(self):
        try:
            directions = tuple(int(bool(output.value)) for output in self.outputs)
            error = ''
        except Exception as exc:
            directions, error = None, str(exc)
        self.output_sample = self.speeds, directions, error

    def _disable_auto(self, reason):
        if self.stabilizer is not None:
            self.stabilizer.stop(reason)
        self.requested = ZERO
        self.control_notice = reason

    def set_speeds(self, values):
        values = tuple(values)
        if len(values) != 6 or any(type(x) is not int or not -100 <= x <= 100 for x in values):
            raise ValueError('Provide six integer speeds from -100 to 100')
        with self.lock:
            if self.fault is not None or self.closed:
                raise OSError('Controller stopped; restart before commanding motion')
            if self.recovering:
                if any(values):
                    raise OSError('I2C recovering; only STOP is available')
            self._disable_auto('Manual mode' if any(values) else 'Stopped')
            self.requested = values
            self.next_pwm = 0.

    def set_motor(self, motor, speed):
        if type(motor) is not int or not 1 <= motor <= 6:
            raise ValueError('Motor number must be 1-6')
        with self.lock:
            values = list(self.requested)
            values[motor-1] = speed
            self.set_speeds(values)

    def enable_stabilization(self):
        with self.lock:
            if self.fault or self.recovering or self.closed or self.imu is None or self.estimator is None:
                raise ValueError('Stabilization needs calibrated IMU, control dependencies and a healthy motor link')
            if (any(self.speeds) or any(self.requested)
                    or (self.pwm.pending is not None and any(self.pwm.pending[-1]))):
                raise ValueError('STOP and wait for acknowledged zero before enabling')
            candidate = Stabilizer(self.config)
            candidate.enable(self.attitude, self.pwm.range_samples[self.config.depth_sensor-1],
                             self.clock(), self.pwm.telemetry_v2)
            self.stabilizer = candidate
            self.next_control = self.clock()+.05

    def recapture_heading(self):
        with self.lock:
            if not self.automatic:
                raise ValueError('Enable stabilization first')
            self.stabilizer.recapture_heading(self.attitude, self.clock())

    def set_targets(self, clearance_m, heading_deg=None):
        with self.lock:
            if not self.automatic:
                raise ValueError('Enable stabilization first')
            self.stabilizer.set_targets(clearance_m, heading_deg)

    def _enter_recovery(self, error, now):
        self._disable_auto('Automatic control stopped; explicitly enable again after recovery')
        self.recovering = True
        self.recovery_attempt = 0
        self.retry_at = now+.1
        self.link_notice = f'I2C reconnecting; requesting zero PWM: {error}'
        self.pwm.invalidate()
        self.pending_directions = None

    def _latch_fault(self, error, now):
        self.fault = error
        self._disable_auto(str(error))
        self.link_notice = str(error)
        self.recovering = True
        self.pwm.invalidate()
        self.pending_directions = None
        # No further writes. The firmware watchdog disables PWM before DIR moves.
        self.release_at = now+1.1

    def _apply_ack(self, values, now):
        for i, magnitude in enumerate(values):
            if not magnitude and self.speeds[i]:
                self.stopped_at[i] = now
        self.speeds = tuple(magnitude*direction for magnitude, direction in zip(values, self.pending_directions))
        self.last_pwm_ack_at = now
        self.pending_directions = None
        if self.recovering:
            for output in self.outputs:
                output.off()
            self.wire_directions = [0]*6
            self.stopped_at = [now]*6
            self.recovering = False
            self.link_notice = 'I2C recovered; all motors stopped. Re-enable automatic control explicitly.'
        self._record_outputs()

    def _frame(self, now):
        values = []
        delay = self.config.reversal_seconds if self.config else .5
        for i, target in enumerate(self.requested):
            desired = (target > 0)-(target < 0)
            current = (self.speeds[i] > 0)-(self.speeds[i] < 0)
            self.coasting[i] = False
            if current and desired != current:
                # DIR remains steady until this zero frame is acknowledged.
                values.append(0)
                continue
            if not self.speeds[i]:
                reversing = desired and self.last_directions[i] not in (0, desired)
                if reversing and now-self.stopped_at[i] < delay:
                    values.append(0)
                    self.coasting[i] = True
                    continue
                # Both transitions are safe only with acknowledged zero PWM.
                if desired > 0:
                    self.outputs[i].on()
                else:
                    self.outputs[i].off()
                self.wire_directions[i] = desired
            if desired:
                self.last_directions[i] = desired
            values.append(abs(target))
        return tuple(values)

    def _tick(self, now):
        """One scheduler iteration, also exposed for deterministic host tests."""
        with self.lock:
            if self.release_at is not None:
                if now >= self.release_at:
                    for output in self.outputs:
                        output.off()
                    self.speeds = ZERO
                    self.wire_directions = [0]*6
                    self.recovering = False
                    self.release_at = None
                    self._record_outputs()
                return
            if self.fault is not None:
                return
            if self.automatic and self.last_tick is not None and now-self.last_tick > .1:
                self._disable_auto('Scheduler stalled; automatic control stopped')
                self.next_pwm = 0
            self.last_tick = now
            try:
                ack = self.pwm.poll()
                now = self.clock()
                if ack is not None:
                    self._apply_ack(ack, now)
                # Recovery serializes only zero frames and suppresses IMU traffic.
                if self.recovering:
                    if self.pwm.pending is None and now >= self.retry_at:
                        if self.recovery_attempt >= 5:
                            self._latch_fault(OSError('I2C recovery exhausted five retries'), now)
                        else:
                            self.recovery_attempt += 1
                            self.retry_at = now+min(.1*2**self.recovery_attempt, .5)
                            self.pending_directions = tuple(self.wire_directions)
                            self.pwm.begin(ZERO)
                    return
                if self.imu is not None:
                    self.imu.poll_due(now)
                    now = self.clock()
                    if self.estimator is not None:
                        try:
                            self.attitude = self.estimator.update(self.imu.readings, now)
                        except Exception as error:
                            self._disable_auto(f'Estimator reset required: {error}')
                            self.estimator = None
                            self.attitude = None
                if self.automatic and now >= self.next_control:
                    self.next_control = now+.05
                    try:
                        self.requested = self.stabilizer.update(self.attitude,
                            self.pwm.range_samples[self.config.depth_sensor-1], now,
                            any(self.coasting) or self.requested != self.speeds)
                        self.control_notice = self.stabilizer.reason
                    except Exception as error:
                        self._disable_auto(f'Automatic control stopped: {error}')
                        self.next_pwm = 0.
                if self.pwm.pending is None and now >= self.next_pwm:
                    self.next_pwm = now+.05
                    frame = self._frame(now)
                    self.pending_directions = tuple(self.wire_directions)
                    self.pwm.begin(frame)
                self._log(now)
            except OSError as error:
                if self.recovering:
                    self.pwm.invalidate()
                    self.pending_directions = None
                    self.retry_at = now+min(.1*2**self.recovery_attempt, .5)
                else:
                    self._enter_recovery(error, now)
            except Exception as error:
                self._latch_fault(error, now)

    def _log(self, now):
        if self.imu is not None and self.log is not None:
            for group, reading in self.imu.readings.items():
                if reading is not None and self.last_logged.get(group) != reading.at:
                    self.last_logged[group] = reading.at
                    self.events.append(dict(type='raw', at=reading.at, group=group,
                                            values=reading.values, error=reading.error))
        if self.trace or self.log is not None:
            if now-self.last_logged.get('control', float('-inf')) >= .1:
                self.last_logged['control'] = now
                self.events.append(dict(type='control', at=now, requested=self.requested,
                    applied=self.speeds, coasting=self.coasting[:], notice=self.control_notice,
                    link=self.link_notice, ack_at=self.last_pwm_ack_at,
                    attitude=None if self.attitude is None else vars(self.attitude),
                    saturated=bool(self.stabilizer and self.stabilizer.allocator.limited)))

    def _heartbeat(self):
        while not self.done.wait(.002):
            self._tick(self.clock())
            # File I/O never holds the controller lock; keep a bounded log queue.
            with self.lock:
                events, self.events = self.events[-100:], []
            for event in events:
                try:
                    line = json.dumps(event, allow_nan=False)
                    if self.log is not None:
                        self.log.write(line+'\n')
                    if self.trace and event['type'] == 'control':
                        print(line, flush=True)
                except (OSError, ValueError) as error:
                    with self.lock:
                        self._disable_auto(f'Diagnostic logging failed: {error}')

    def close(self):
        if self.closed:
            return
        self.done.set()
        if self.worker is not None:
            self.worker.join()
        with self.lock:
            self.closed = True
            self._disable_auto('Closed')
            # A previously sent nonzero frame may still be pending. Cancel its
            # acknowledgement and send a fresh zero command before releasing DIR.
            self.pwm.invalidate()
            try:
                self.pwm.set_percentages(ZERO)
            except BaseException:
                time.sleep(1.1)
            finally:
                self.speeds = ZERO
                for output in self.outputs:
                    output.off()
                    output.close()
                self._record_outputs()
