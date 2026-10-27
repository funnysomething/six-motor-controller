"""GPIO chip selection and startup failure tests without Pi hardware."""

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
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
        pwm.set_percentages.assert_called_once_with((0,) * 6)

    def test_diagnose_reports_pin_owner_without_claiming_any_lines(self):
        lgpio = Mock()
        lgpio.gpiochip_open.return_value = 10
        lgpio.gpio_get_line_info.side_effect = lambda handle, pin: [0, pin, 1, f"GPIO{pin}", "spi0"]
        output = io.StringIO()
        with patch.dict("sys.modules", {"lgpio": lgpio}), \
                patch.object(motors, "scan_gpio_chips", return_value=[chip(1, "pinctrl-bcm2711")]), \
                patch.object(motors, "print_gpio_claim_details") as details, \
                patch.object(Path, "read_text", return_value="Raspberry Pi 4 Model B\x00"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(motors.diagnose_gpio(), 1)
        self.assertIn("Header GPIO chip: /dev/gpiochip1", output.getvalue())
        self.assertIn("Motor1 GPIO26: line 26 (GPIO26), CLAIMED, consumer='spi0'", output.getvalue())
        details.assert_called_once_with("gpiochip1")
        self.assertEqual(lgpio.gpio_get_line_info.call_count, 6)
        lgpio.gpio_claim_input.assert_not_called()
        lgpio.gpio_claim_output.assert_not_called()
        lgpio.gpiochip_close.assert_called_once_with(10)

    def test_blank_consumer_is_reported_as_unknown_not_as_free(self):
        lgpio = Mock()
        lgpio.gpiochip_open.return_value = 10
        lgpio.gpio_get_line_info.side_effect = lambda handle, pin: [0, pin, int(pin == 13), f"GPIO{pin}", ""]
        output = io.StringIO()
        with patch.dict("sys.modules", {"lgpio": lgpio}), \
                patch.object(motors, "scan_gpio_chips", return_value=[chip(0, "pinctrl-bcm2711")]), \
                patch.object(motors, "print_gpio_claim_details"), \
                patch.object(Path, "read_text", return_value="Raspberry Pi 4 Model B\x00"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(motors.diagnose_gpio(), 1)
        self.assertIn("GPIO13: line 13 (GPIO13), CLAIMED, consumer=<not supplied by kernel>", output.getvalue())
        self.assertIn("Claimed motor pins: GPIO13", output.getvalue())

    def test_preflight_lists_all_claimed_motor_pins_without_claiming_more(self):
        lgpio = Mock()
        lgpio.gpiochip_open.return_value = 10
        lgpio.gpio_get_line_info.side_effect = lambda handle, pin: [0, pin, int(pin in (13, 5, 6, 11)), f"GPIO{pin}", ""]
        with self.assertRaises(RuntimeError) as caught:
            motors.ensure_motor_gpio_available(lgpio, 0)
        for pin in (13, 5, 6, 11):
            self.assertIn(f"GPIO{pin}", str(caught.exception))
        lgpio.gpio_claim_input.assert_not_called()
        lgpio.gpio_claim_output.assert_not_called()
        lgpio.gpiochip_close.assert_called_once_with(10)

    def test_fdinfo_parser_tracks_repeated_gpio_lines(self):
        text = "pos:\t0\ngpio-chip:\tgpiochip4\ngpio-line:\t13\ngpio-line:\t5\n"
        self.assertEqual(motors.parse_gpio_fdinfo(text), ("gpiochip4", [13, 5]))
        self.assertEqual(motors.parse_gpio_fdinfo("pos: 0\ngpio-line: invalid\n"), (None, []))

    def test_unused_direction_pins_are_not_inspected_or_claimed(self):
        lgpio = Mock()
        lgpio.gpiochip_open.return_value = 10
        unused_pins = {4, 9, 10, 17, 22, 27}
        lgpio.gpio_get_line_info.side_effect = lambda handle, pin: [
            0, pin, int(pin in unused_pins), f"GPIO{pin}", "spi0" if pin in unused_pins else ""]
        motors.ensure_motor_gpio_available(lgpio, 0)
        self.assertEqual({call.args[1] for call in lgpio.gpio_get_line_info.call_args_list},
                         {26, 19, 13, 6, 5, 11})

    def test_process_scan_identifies_motor_line_holder_and_ignores_other_chip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "123"
            (process / "fdinfo").mkdir(parents=True)
            (process / "comm").write_text("python3\n")
            (process / "fdinfo" / "7").write_text("gpio-chip: gpiochip0\ngpio-line: 13\ngpio-line: 5\n")
            (process / "fdinfo" / "8").write_text("gpio-chip: gpiochip1\ngpio-line: 4\n")
            holders, unreadable = motors.gpio_process_holders("gpiochip0", root)
        self.assertEqual(holders, [dict(pid=123, process="python3", pins=[5, 13])])
        self.assertEqual(unreadable, 0)

    def test_boot_settings_report_uart_and_overlay_clues_without_editing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.txt"
            text = "#dtoverlay=w1-gpio\ndtoverlay=uart3\ndtparam=spi=on\ndtoverlay=w1-gpio\ninclude usercfg.txt\n"
            config.write_text(text)
            settings = motors.gpio_boot_settings([config])
            self.assertEqual(config.read_text(), text)
        self.assertEqual(len(settings), 4)
        self.assertIn(":2: dtoverlay=uart3", settings[0])

    def test_claimed_gpio_aborts_before_gpiozero_factory_creation(self):
        lgpio = Mock()
        factory_class = Mock()
        module = SimpleNamespace(LGPIOFactory=factory_class)
        with patch.dict("sys.modules", {"lgpio": lgpio, "gpiozero": SimpleNamespace(),
                                        "gpiozero.pins": SimpleNamespace(), "gpiozero.pins.lgpio": module}), \
                patch.object(motors, "scan_gpio_chips", return_value=[chip(0, "pinctrl-bcm2711")]), \
                patch.object(motors, "ensure_motor_gpio_available", side_effect=RuntimeError("GPIO13 claimed")):
            with self.assertRaisesRegex(RuntimeError, "GPIO13 claimed"):
                motors.make_gpio_factory()
        factory_class.assert_not_called()

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
