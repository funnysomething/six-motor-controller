"""Raw Pi/ESP32 motor transactions with bounded asynchronous acknowledgements."""
import secrets
import time

if __package__:
    from .stabilization import RangeSample
else:
    from stabilization import RangeSample


def checksum(data):
    value = 0
    for byte in data:
        value ^= byte
    return value


class I2CPwm:
    def __init__(self, bus, message_factory, address=0x32, channel_map=(3, 4, 5, 0, 1, 2), clock=time.monotonic):
        if any(type(x) is not int for x in channel_map) or sorted(channel_map) != list(range(6)):
            raise ValueError("PWM map must be a permutation of channels 0-5")
        self.bus, self.message, self.address = bus, message_factory, address
        self.channel_map, self.clock = tuple(channel_map), clock
        self.sequence = secrets.randbits(32)
        self.pending = None
        self.telemetry_v2 = False
        self.range_samples = (None,) * 3
        self.ultrasonic_samples = (None,) * 3
        self.last_ack = None

    @property
    def ultrasonic_sample(self):
        return self.ultrasonic_samples[0]

    def invalidate(self):
        self.pending = None
        self.range_samples = (None,) * 3
        self.ultrasonic_samples = (None,) * 3
        self.telemetry_v2 = False

    def begin(self, values):
        if self.pending is not None:
            raise RuntimeError("Motor transaction already pending")
        if len(values) != 6 or any(type(x) is not int or not 0 <= x <= 100 for x in values):
            raise ValueError("PWM needs six integer percentages from 0 to 100")
        wire = [0]*6
        for i, channel in enumerate(self.channel_map):
            wire[channel] = values[i]
        self.sequence = (self.sequence+1) & 0xFFFFFFFF
        sequence = self.sequence.to_bytes(4, 'little')
        data = bytes((0xA5, 1))+sequence+bytes(wire)
        now = self.clock()
        self.bus.i2c_rdwr(self.message.write(self.address, data+bytes((checksum(data),))))
        self.pending = (sequence, bytes(wire), now, now+.3, now+.005, tuple(values))

    def poll(self):
        """Return None while pending, or acknowledged logical PWM magnitudes."""
        if self.pending is None:
            return None
        sequence, wire, sent, deadline, next_read, logical = self.pending
        now = self.clock()
        if now >= deadline:
            self.pending = None
            raise OSError("ESP32 did not acknowledge applied PWM within 0.3 seconds")
        if now < next_read:
            return None
        self.pending = (sequence, wire, sent, deadline, now+.005, logical)
        reply = self.message.read(self.address, 32)
        self.bus.i2c_rdwr(reply)
        status = bytes(reply)
        if not (len(status) >= 14 and status[0] == 0x5A and status[1] in (1, 2)
                and status[2:6] == sequence and status[6] == 0 and status[7:13] == wire
                and status[13] == checksum(status[:13])):
            return None
        received = self.clock()
        self.telemetry_v2 = status[1] == 2
        ranges = list(self.range_samples)
        samples = list(self.ultrasonic_samples)
        if self.telemetry_v2:
            for sensor in range(3):
                offset = 14+sensor*6
                block = status[offset:offset+6]
                if len(block) != 6 or block[0] != 0xD1+sensor or block[5] != checksum(block[:5]):
                    continue
                state, seq = block[1] & 3, block[1] >> 2
                mm = int.from_bytes(block[2:4], 'little')
                if state == 1 and not 20 <= mm <= 3000:
                    continue
                # Include transaction latency conservatively. Never refresh an
                # unchanged sequence, even if delivered in a new motor ack.
                at = received-block[4]*.01-(received-sent)
                if block[4] == 255:
                    ranges[sensor] = None
                    samples[sensor] = None
                elif ranges[sensor] is None or ranges[sensor].sequence != seq:
                    ranges[sensor] = RangeSample(at, seq, state, mm/1000.)
                    samples[sensor] = (at, state, mm)
        else:
            # Legacy ranges are display-only; their age isn't authoritative.
            ranges = [None]*3
            for sensor, offset, start in ((0,14,0),(1,19,19),(2,24,24)):
                if (len(status) >= offset+5 and status[offset] == 0xD1+sensor
                        and status[offset+1] in (0,1,2,3)
                        and status[offset+4] == checksum(status[start:offset+4])):
                    state = status[offset+1]
                    mm = int.from_bytes(status[offset+2:offset+4], 'little')
                    if state != 1 or 20 <= mm <= 3000:
                        samples[sensor] = (received, state, mm)
        self.range_samples, self.ultrasonic_samples = tuple(ranges), tuple(samples)
        self.last_ack = received
        self.pending = None
        return logical

    def set_percentages(self, values):
        """Synchronous startup/shutdown helper; normal operation uses begin/poll."""
        self.begin(values)
        while True:
            time.sleep(.002)
            if self.poll() is not None:
                return
