"""Check plotting repair environment isolation and stopping on installation failure."""

import contextlib
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from raspberry_pi import repair_imx500_plotting as repair


class PlotRepairTests(unittest.TestCase):
    def test_wrong_environment_cannot_be_modified(self):
        with patch.object(repair.sys, "prefix", "/another/environment"), \
                patch.object(repair.subprocess, "run") as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(repair.main(), 1)
        run.assert_not_called()

    def test_install_failure_stops_before_import_tests(self):
        with patch.object(repair.sys, "prefix", str(repair.ENVIRONMENT)), \
                patch.object(repair.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pip")) as run, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(repair.main(), 1)
        run.assert_called_once()
        self.assertIn("--no-deps", run.call_args.args[0])
        self.assertIn("matplotlib==3.9.4", run.call_args.args[0])
