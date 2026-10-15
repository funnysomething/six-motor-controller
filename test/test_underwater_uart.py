"""Run the firmware's UART decoder on the host without ESP32 hardware."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UnderwaterUartTests(unittest.TestCase):
    def test_sensor_frames_and_recovery(self):
        compiler = shutil.which("c++")
        if compiler is None:
            self.skipTest("A host C++ compiler is required for the firmware decoder test")
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "underwater_uart_test"
            subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(project / "include"),
                            str(project / "test" / "underwater_uart_test.cpp"),
                            "-o", str(executable)], check=True, capture_output=True, text=True)
            subprocess.run([str(executable)], check=True, capture_output=True, text=True)
