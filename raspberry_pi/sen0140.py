"""SEN0140 V2 I2C readings, polled by the motor controller's bus-owning worker.

Register references: DFRobot ADXL345, ITG3200 and QMC5883 libraries;
Bosch BMP280 datasheet, including floating-point compensation in appendix 1.
"""

from dataclasses import dataclass
import math
import struct
import time


@dataclass(frozen=True)
class ImuReading:
    at: float
    values: tuple = ()
    error: str = ""


class Sen0140:
    GROUPS = ("accel", "gyro", "mag", "barometer")
    STALE_SECONDS = 3.0

    def __init__(self, bus):
        self.bus = bus
        self.readings = dict.fromkeys(self.GROUPS)
        self.devices = {}
        self.ready_at = {}
        self.next_group = 0

    def _read(self, address, register, count):
        data = bytes(self.bus.read_i2c_block_data(address, register, count))
        if len(data) != count:
            raise OSError("Incomplete I2C register read")
        return data

    def _byte(self, address, register):
        return self._read(address, register, 1)[0]

    def _write(self, address, register, value):
        self.bus.write_byte_data(address, register, value)

    def _find(self, addresses, register, identity):
        for address in addresses:
            try:
                if identity(self._byte(address, register)):
                    return address
            except OSError:
                pass
        raise OSError("Sensor not detected; check power and shared SDA/SCL")

    def _initialize(self, group):
        if group == "accel":
            address = self._find((0x53, 0x1D), 0x00, lambda value: value == 0xE5)
            self._write(address, 0x2D, 0)  # Standby before changing format/rate.
            self._write(address, 0x31, 0x08)  # Full resolution, +/-2 g, 3.9 mg/LSB.
            self._write(address, 0x2C, 0x0A)  # 100 Hz.
            self._write(address, 0x2D, 0x08)  # Measurement mode.
            return (address, "ADXL345")
        if group == "gyro":
            address = self._find((0x68, 0x69), 0x00, lambda value: value & 0x7E == 0x68)
            self._write(address, 0x3E, 0x01)  # Awake, PLL using X gyro.
            self._write(address, 0x15, 9)  # 100 Hz with filtered 1 kHz base.
            self._write(address, 0x16, 0x1B)  # +/-2000 degrees/s, 42 Hz filter.
            self._write(address, 0x17, 0x01)  # Enable raw-data-ready status.
            return (address, "ITG3200/3205")
        if group == "mag":
            # DFRobot's SEN0140 library supports VCM, QMC and legacy HMC chips.
            try:
                self._byte(0x0C, 0x00)
            except OSError:
                pass
            else:
                self._write(0x0C, 0x0B, 0x00)
                self._write(0x0C, 0x0A, 0x4D)
                return (0x0C, "VCM5883L")
            try:
                identity = self._byte(0x0D, 0x0D)
            except OSError:
                identity = None
            if identity == 0xFF:
                self._write(0x0D, 0x0B, 0x01)  # Recommended set/reset period.
                self._write(0x0D, 0x09, 0x15)  # Continuous, 50 Hz, +/-8 gauss.
                return (0x0D, "QMC5883L")
            if self._read(0x1E, 0x0A, 3) != b"H43":
                raise OSError("Supported magnetometer not detected")
            self._write(0x1E, 0x00, 0x10)  # 15 Hz.
            self._write(0x1E, 0x01, 0x20)  # +/-1.3 gauss.
            self._write(0x1E, 0x02, 0x00)  # Continuous.
            return (0x1E, "HMC5883L")
        address = self._find((0x76, 0x77), 0xD0, lambda value: value == 0x58)
        if self._byte(address, 0xF3) & 1:
            raise OSError("BMP280 calibration is still loading")
        calibration = self._read(address, 0x88, 24)
        self.calibration = struct.unpack("<HhhHhhhhhhhh", calibration)
        if not self.calibration[0] or not self.calibration[3] or calibration == b"\xff" * 24:
            raise OSError("Invalid BMP280 calibration")
        self._write(address, 0xF4, 0x00)  # Sleep before configuration.
        self._write(address, 0xF5, 0xA0)  # 1000 ms standby.
        self._write(address, 0xF4, 0x27)  # Normal mode, temperature/pressure x1.
        return (address, "BMP280")

    @staticmethod
    def compensate_bmp280(raw_pressure, raw_temperature, calibration):
        t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9 = calibration
        v1 = (raw_temperature / 16384.0 - t1 / 1024.0) * t2
        v2 = (raw_temperature / 131072.0 - t1 / 8192.0) ** 2 * t3
        fine = v1 + v2
        temperature = fine / 5120.0
        v1 = fine / 2.0 - 64000.0
        v2 = v1 * v1 * p6 / 32768.0 + v1 * p5 * 2.0
        v2 = v2 / 4.0 + p4 * 65536.0
        v1 = (p3 * v1 * v1 / 524288.0 + p2 * v1) / 524288.0
        v1 = (1.0 + v1 / 32768.0) * p1
        if v1 == 0:
            raise OSError("Invalid BMP280 pressure calibration")
        pressure = (1048576.0 - raw_pressure - v2 / 4096.0) * 6250.0 / v1
        v1 = p9 * pressure * pressure / 2147483648.0
        v2 = pressure * p8 / 32768.0
        pressure += (v1 + v2 + p7) / 16.0
        return (temperature, pressure / 100.0)  # Celsius, hPa.

    def _measure(self, group):
        address, chip = self.devices[group]
        if group == "accel":
            if not self._byte(address, 0x30) & 0x80:  # DATA_READY.
                return None
            values = struct.unpack("<hhh", self._read(address, 0x32, 6))
            return tuple(value * 0.0039 for value in values)
        if group == "gyro":
            if not self._byte(address, 0x1A) & 1:
                return None
            values = struct.unpack(">hhh", self._read(address, 0x1D, 6))
            return tuple(value / 14.375 for value in values)
        if group == "mag":
            if chip == "HMC5883L":
                if not self._byte(address, 0x09) & 1:
                    return None
                x, z, y = struct.unpack(">hhh", self._read(address, 0x03, 6))
                if -4096 in (x, y, z):
                    raise OSError("Magnetometer overflow")
                return (x, y, z)
            if chip == "QMC5883L":
                status = self._byte(address, 0x06)
                if status & 2:
                    raise OSError("Magnetometer overflow")
                if not status & 1:
                    return None
            values = struct.unpack("<hhh", self._read(address, 0x00, 6))
            # Match DFRobot's VCM axis convention. Report counts, not an
            # assumed magnetic scale shared across different chip versions.
            return tuple(-value for value in values) if chip == "VCM5883L" else values
        data = self._read(address, 0xF7, 6)
        pressure = int.from_bytes(data[:3], "big") >> 4
        temperature = int.from_bytes(data[3:], "big") >> 4
        if 0x80000 in (pressure, temperature):
            return None  # Skipped/unavailable ADC output.
        values = self.compensate_bmp280(pressure, temperature, self.calibration)
        if not (-40 <= values[0] <= 85 and 300 <= values[1] <= 1100):
            raise OSError("BMP280 reading outside specified range")
        return values

    def poll_next(self):
        """One component per heartbeat; caller serializes access to the bus.

        Failed components retry after two seconds. Other components and motor
        acknowledgements continue, and cached values retain their real age.
        """
        group = self.GROUPS[self.next_group]
        self.next_group = (self.next_group + 1) % len(self.GROUPS)
        now = time.monotonic()
        if now < self.ready_at.get(group, 0):
            return
        try:
            if group not in self.devices:
                self.devices[group] = self._initialize(group)
                self.ready_at[group] = now + 0.1
                return
            values = self._measure(group)
            if values is None:
                return
            reading = ImuReading(time.monotonic(), tuple(values))
        except OSError as error:
            self.devices.pop(group, None)
            self.ready_at[group] = time.monotonic() + 2.0
            reading = ImuReading(time.monotonic(), error=str(error))
        self.readings = {**self.readings, group: reading}

    @staticmethod
    def tilt(acceleration):
        x, y, z = acceleration
        if math.sqrt(x * x + y * y + z * z) < 0.05:
            return None
        return (math.degrees(math.atan2(y, z)),
                math.degrees(math.atan2(-x, math.hypot(y, z))))
