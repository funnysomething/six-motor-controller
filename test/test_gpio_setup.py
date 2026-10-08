"""GPIO chip selection and startup failure tests without Pi hardware."""

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from raspberry_pi import motor_control as motors


def chip(index, label, lines=54):
    return {"index": index, "path": f"/dev/gpiochip{index}",
            "label": label, "lines": lines, "name": f"gpiochip{index}"}


class GpioSetupTests(unittest.TestCase):
    def test_header_detected_even_when_chip_zero_is_an_expander(self):
        chips = [chip(0, "raspberrypi-exp-gpio", 8), chip(1, "pinctrl-bcm2711")]
        self.assertEqual(motors.select_gpio_chip(chips), 1)

    def test_pi5_rp1_detected_at_zero_or_four(self):
        for index in (0, 4):
            self.assertEqual(motors.select_gpio_chip([chip(index, "pinctrl-rp1")]), index)

    def test_wrong_or_ambiguous_chip_is_never_selected(self):
        for chips in ([chip(0, "expander")], [chip(0, "pinctrl-bcm2711", 8)],
                      [chip(0, "pinctrl-bcm2711"), chip(1, "pinctrl-bcm2835")],
                      [{"index": 0, "error": "permission denied"}], []):
            with self.assertRaisesRegex(RuntimeError, "--diagnose"):
                motors.select_gpio_chip(chips)
        with self.assertRaises(RuntimeError):
            motors.select_gpio_chip([chip(0, "expander", 8), chip(1, "pinctrl-bcm2711")], 0)

    def test_explicit_chip_resolves_ambiguity(self):
        chips = [chip(0, "pinctrl-bcm2711"), chip(1, "pinctrl-bcm2835")]
        self.assertEqual(motors.select_gpio_chip(chips, 1), 1)

    def test_unknown_label_detected_by_all_motor_gpio_names(self):
        header = chip(4, "rp1-gpio-controller")
        header["gpio_names"] = {pin: f"GPIO{pin}" for pin in motors.MOTOR_GPIO_PINS}
        chips = [chip(0, "internal-gpio"), chip(1, "expander", 8), header]
        self.assertEqual(motors.select_gpio_chip(chips), 4)

    def test_byte_labels_and_nul_padding_are_normalized(self):
        self.assertEqual(motors.select_gpio_chip([chip(0, b"pinctrl-bcm2711\x00 ")]), 0)

    def test_explicit_chip_accepts_unknown_label_without_line_names(self):
        self.assertEqual(motors.select_gpio_chip([chip(0, "different-kernel-label")], 0), 0)

    def test_explicit_chip_rejects_inaccessible_missing_or_wrong_offsets(self):
        wrong = chip(0, "internal-gpio")
        wrong["gpio_names"] = {26: "SD_CARD_DETECT"}
        for chips, index in (([{"index": 0, "path": "/dev/gpiochip0", "error": "denied"}], 0),
                             ([chip(0, "pinctrl-bcm2711")], 4), ([wrong], 0)):
            with self.assertRaises(RuntimeError):
                motors.select_gpio_chip(chips, index)

    def test_complete_motor_names_resolve_multiple_labels(self):
        header = chip(4, "pinctrl-rp1")
        header["gpio_names"] = {pin: f"GPIO{pin}" for pin in motors.MOTOR_GPIO_PINS}
        self.assertEqual(motors.select_gpio_chip([chip(0, "pinctrl-bcm2711"), header]), 4)

    def test_two_device_paths_for_same_controller_are_not_ambiguous(self):
        header = chip(0, "pinctrl-rp1")
        alias = chip(4, "pinctrl-rp1")
        alias["name"] = header["name"]
        self.assertEqual(motors.select_gpio_chip([alias, header]), 0)
        self.assertEqual(motors.select_gpio_chip([alias, header], 4), 4)

    def test_scan_reports_permissions_and_closes_metadata_handles(self):
        lgpio = Mock()
        lgpio.gpiochip_open.side_effect = [PermissionError("permission denied"), 10]
        lgpio.gpio_get_chip_info.return_value = [0, 54, "gpiochip1", "pinctrl-bcm2711"]
        lgpio.gpio_get_line_info.side_effect = lambda handle, pin: [0, pin, 0, f"GPIO{pin}", ""]
        chips = motors.scan_gpio_chips(lgpio, [Path("/dev/gpiochip0"), Path("/dev/gpiochip1")])
        self.assertIn("permission denied", chips[0]["error"])
        self.assertEqual(chips[1]["label"], "pinctrl-bcm2711")
        self.assertTrue(motors.matching_gpio_names(chips[1]))
        lgpio.gpiochip_close.assert_called_once_with(10)
        lgpio.gpio_claim_input.assert_not_called()
        lgpio.gpio_claim_output.assert_not_called()

    def test_output_failure_names_motor_pin_and_selected_chip(self):
        original = Exception("unexpected error")
        output_type = Mock(side_effect=original)
        factory = SimpleNamespace(chip=1)
        with self.assertRaisesRegex(RuntimeError, "Motor1 GPIO26 on gpiochip1") as caught:
            motors.make_output_factory(factory, output_type)(26)
        self.assertIs(caught.exception.__cause__, original)
        output_type.assert_called_once_with(26, initial_value=False, pin_factory=factory)

    def test_startup_cleanup_does_not_hide_original_pin_failure(self):
        output = Mock()
        output.off.side_effect = Exception("cleanup off failed")
        output.close.side_effect = Exception("cleanup close failed")
        original = RuntimeError("Cannot initialize GPIO19")
        output_factory = Mock(side_effect=[output, original])
        pwm = Mock()
        with self.assertRaises(RuntimeError) as caught:
            motors.MotorController(pwm, output_factory, start_heartbeat=False)
        self.assertIs(caught.exception, original)
        output.off.assert_called_once()
        output.close.assert_called_once()
        pwm.set_percentages.assert_not_called()

    def test_diagnose_reports_pin_owner_without_claiming_any_lines(self):
        lgpio = Mock()
        lgpio.gpiochip_open.return_value = 10
        lgpio.gpio_get_line_info.side_effect = lambda handle, pin: [0, pin, 1, f"GPIO{pin}", "spi0"]
        output = io.StringIO()
        with patch.dict("sys.modules", {"lgpio": lgpio}), \
                patch.object(motors, "scan_gpio_chips", return_value=[chip(1, "pinctrl-bcm2711")]), \
                patch.object(Path, "read_text", return_value="Raspberry Pi 4 Model B\x00"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(motors.diagnose_gpio(), 0)
        self.assertIn("Header GPIO chip: /dev/gpiochip1", output.getvalue())
        self.assertIn("Motor1 GPIO26: line 26 (GPIO26), spi0", output.getvalue())
        self.assertEqual(lgpio.gpio_get_line_info.call_count, 12)
        lgpio.gpio_claim_input.assert_not_called()
        lgpio.gpio_claim_output.assert_not_called()
        lgpio.gpiochip_close.assert_called_once_with(10)

    def test_diagnostic_command_does_not_initialize_motor_hardware(self):
        with patch("sys.argv", ["motor_control.py", "--diagnose"]), \
                patch.object(motors, "diagnose_gpio", return_value=0) as diagnose, \
                patch.object(motors, "make_gpio_factory") as factory:
            self.assertEqual(motors.main(), 0)
        factory.assert_not_called()
        diagnose.assert_called_once_with(None)

    def test_diagnostic_command_honors_explicit_chip(self):
        with patch("sys.argv", ["motor_control.py", "--diagnose", "--gpiochip", "4"]), \
                patch.object(motors, "diagnose_gpio", return_value=0) as diagnose:
            self.assertEqual(motors.main(), 0)
        diagnose.assert_called_once_with(4)


if __name__ == "__main__":
    unittest.main()
