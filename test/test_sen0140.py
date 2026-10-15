"""SEN0140 chip detection, register decoding, compensation and fault recovery."""

import struct
import unittest
from unittest.mock import patch

from raspberry_pi.sen0140 import Sen0140


# Bosch BMP280 datasheet worked example, not values derived from this driver.
CALIBRATION = (27504, 26435, -1000, 36477, -10685, 3024,
               2855, 140, -7, 15500, -14600, 6000)


class Bus:
    def __init__(self):
        self.registers = {
            (0x53, 0): b"\xe5", (0x53, 0x30): b"\x80",
            (0x53, 0x32): struct.pack("<hhh", 256, -256, 0),
            (0x68, 0): b"\x68", (0x68, 0x1A): b"\x01",
            (0x68, 0x1D): struct.pack(">hhh", 14375, -14375, 0),
            (0x0C, 0): struct.pack("<hhh", 123, -456, 789),
            (0x77, 0xD0): b"\x58", (0x77, 0xF3): b"\x00",
            (0x77, 0x88): struct.pack("<HhhHhhhhhhhh", *CALIBRATION),
            (0x77, 0xF7): (415148 << 4).to_bytes(3, "big")
                          + (519888 << 4).to_bytes(3, "big"),
        }
        self.writes = []
        self.reads = []
        self.failed = set()

    def read_i2c_block_data(self, address, register, count):
        self.reads.append((address, register, count))
        if address in self.failed or (address, register) not in self.registers:
            raise OSError("No response")
        return list(self.registers[address, register][:count])

    def write_byte_data(self, address, register, value):
        if address in self.failed:
            raise OSError("No response")
        self.writes.append((address, register, value))


class Sen0140Tests(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.imu = Sen0140(self.bus)
        self.now = 10.0
        timer = patch("raspberry_pi.sen0140.time.monotonic", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)

    def cycle(self):
        for _ in range(4):
            self.imu.poll_next()
        self.now += 1.0

    def test_all_four_chips_decode_with_correct_units_and_bosch_reference(self):
        self.cycle()  # Configuration without publishing unsettled data.
        self.assertTrue(all(value is None for value in self.imu.readings.values()))
        self.cycle()
        self.assertEqual(self.imu.readings["accel"].values, (0.9984, -0.9984, 0.0))
        self.assertEqual(self.imu.readings["gyro"].values, (1000.0, -1000.0, 0.0))
        self.assertEqual(self.imu.readings["mag"].values, (-123, 456, -789))
        temperature, pressure = self.imu.readings["barometer"].values
        self.assertAlmostEqual(temperature, 25.08248, places=4)
        self.assertAlmostEqual(pressure, 1006.53267, places=4)
        self.assertIn((0x53, 0x31, 8), self.bus.writes)
        self.assertIn((0x0C, 0x0A, 0x4D), self.bus.writes)
        self.assertFalse(any(address == 0x32 for address, _, _ in self.bus.reads))

    def test_missing_component_does_not_hide_working_components_and_retries(self):
        self.bus.failed.add(0x68)
        self.cycle()
        self.cycle()
        self.assertTrue(self.imu.readings["gyro"].error)
        self.assertTrue(self.imu.readings["accel"].values)
        self.assertTrue(self.imu.readings["barometer"].values)
        self.bus.failed.clear()
        self.cycle()  # Retry initialization.
        self.cycle()
        self.assertFalse(self.imu.readings["gyro"].error)
        self.assertEqual(self.imu.readings["gyro"].values[0], 1000)

    def test_read_failure_clears_old_value_and_no_new_data_preserves_age(self):
        self.cycle()
        self.cycle()
        previous = self.imu.readings["accel"]
        self.bus.registers[0x53, 0x30] = b"\x00"
        self.cycle()
        self.assertIs(self.imu.readings["accel"], previous)
        self.bus.failed.add(0x53)
        self.cycle()
        self.assertTrue(self.imu.readings["accel"].error)
        self.assertEqual(self.imu.readings["accel"].values, ())

    def test_short_read_is_reported_instead_of_publishing_broken_axes(self):
        self.cycle()
        self.bus.registers[0x53, 0x32] = b"\x01\x02"
        self.cycle()
        self.assertIn("Incomplete", self.imu.readings["accel"].error)

    def test_alternate_addresses_and_legacy_hmc_axis_order(self):
        for (address, register), value in list(self.bus.registers.items()):
            alternate = {0x53: 0x1D, 0x68: 0x69, 0x77: 0x76}.get(address)
            if alternate is not None:
                del self.bus.registers[address, register]
                self.bus.registers[alternate, register] = value
        self.bus.failed.add(0x0C)
        self.bus.registers.update({(0x1E, 0x0A): b"H43", (0x1E, 9): b"\x01",
                                   (0x1E, 3): struct.pack(">hhh", 123, 789, 456)})
        self.cycle()
        self.cycle()
        self.assertEqual(self.imu.devices["accel"][0], 0x1D)
        self.assertEqual(self.imu.devices["gyro"][0], 0x69)
        self.assertEqual(self.imu.devices["barometer"][0], 0x76)
        self.assertEqual(self.imu.readings["mag"].values, (123, 456, 789))

    def test_qmc_detection_and_overflow(self):
        self.bus.failed.add(0x0C)
        self.bus.registers.update({(0x0D, 0x0D): b"\xff", (0x0D, 6): b"\x01",
                                   (0x0D, 0): struct.pack("<hhh", 12, -34, 56)})
        self.cycle()
        self.cycle()
        self.assertEqual(self.imu.readings["mag"].values, (12, -34, 56))
        self.bus.registers[0x0D, 6] = b"\x03"
        self.cycle()
        self.assertIn("overflow", self.imu.readings["mag"].error)

    def test_wrong_chip_identity_is_not_configured(self):
        self.bus.registers[0x53, 0] = b"\x00"
        self.bus.registers[0x77, 0xD0] = b"\x55"  # BMP085 is not a BMP280.
        self.cycle()
        self.assertTrue(self.imu.readings["accel"].error)
        self.assertTrue(self.imu.readings["barometer"].error)
        self.assertFalse(any(address in (0x53, 0x77) for address, _, _ in self.bus.writes))

    def test_bad_bmp_calibration_is_rejected(self):
        self.bus.registers[0x77, 0x88] = bytes(24)
        self.cycle()
        self.assertIn("calibration", self.imu.readings["barometer"].error)

    def test_tilt_is_acceleration_based_and_freefall_is_unavailable(self):
        self.assertEqual(Sen0140.tilt((0, 0, 1)), (0, 0))
        roll, pitch = Sen0140.tilt((0, 1, 1))
        self.assertAlmostEqual(roll, 45)
        self.assertAlmostEqual(pitch, 0)
        self.assertIsNone(Sen0140.tilt((0, 0, 0)))
