"""Verify repair stops before enabling an environment when native probes fail."""

import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from raspberry_pi import setup_pi4_vision as setup


class PiVisionRepairTests(unittest.TestCase):
    def exercise(self, root, fail):
        pi = root / "raspberry_pi"
        pi.mkdir()
        target = pi / "repair"
        calls = []

        def execute(command, environment=None):
            calls.append(command)
            if "venv" in command:
                target.mkdir()
            if fail and "-c" in command and "Conv2d" in command[-1]:
                raise subprocess.CalledProcessError(-4, command)

        with patch.object(setup, "ROOT", pi), patch.object(setup.sys, "platform", "linux"), \
                patch.object(setup.sys, "version_info", (3, 13)), \
                patch.object(setup.platform, "machine", return_value="aarch64"), \
                patch.object(setup, "execute", side_effect=execute), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = setup.main(["--environment", str(target), "--cpu-only"])
        return code, target, calls

    def test_failed_convolution_never_marks_partial_environment_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            code, target, _ = self.exercise(Path(directory), True)
            self.assertEqual(code, 1)
            self.assertFalse((target / "vision-ready.txt").exists())
            self.assertTrue(target.exists())

    def test_success_uses_cpu_index_and_enables_environment_after_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            code, target, calls = self.exercise(Path(directory), False)
            self.assertEqual(code, 0)
            self.assertTrue((target / "vision-ready.txt").is_file())
            install = next(command for command in calls if "torch==2.7.1" in command)
            self.assertIn("https://download.pytorch.org/whl/cpu", install)


if __name__ == "__main__":
    unittest.main()
