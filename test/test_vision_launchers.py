"""Check launcher paths, model overrides and input-size checks without Pi hardware."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.vision import launch


class VisionLauncherTests(unittest.TestCase):
    def test_process_output_and_exit_status_are_saved_for_export(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "export.log"
            result = launch.execute([sys.executable, "-c",
                                     "import sys; print('export stage', flush=True); sys.exit(3)"],
                                    False, log)
            self.assertEqual(result, 3)
            self.assertIn("export stage", log.read_text())
            self.assertIn("Process return code: 3", log.read_text())

    def test_native_signal_is_reported_with_conventional_exit_status(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch.object(launch.subprocess, "call", return_value=-9), \
                contextlib.redirect_stderr(output):
            self.assertEqual(launch.execute(["/fake/python"], False), 137)
        self.assertIn("SIGKILL", output.getvalue())
        self.assertIn("out of memory", output.getvalue())

    def test_every_shell_profile_can_be_dry_run_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for profile in launch.PROFILES:
                with self.subTest(profile=profile):
                    result = subprocess.run(
                        ["bash", str(launch.ROOT / "scripts/vision" / f"{profile}.sh"), "--dry-run"],
                        cwd=directory, text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(str(launch.ROOT), result.stdout)

    def test_ncnn_uses_selected_export_metadata_and_rejects_conflicting_size(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)
            (model / "metadata.yaml").write_text("imgsz:\n- 320\n- 320\nnames:\n  0: testudo\n")
            with patch.object(launch, "environment_python", return_value=Path("/fake/python")):
                command = launch.build_command("detect-ncnn", ["--model", str(model)])
                self.assertEqual(command[command.index("--imgsz") + 1], "320")
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    launch.build_command("detect-ncnn", ["--model", str(model), "--imgsz", "640"])

    def test_missing_camera_model_fails_before_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "packerOut.zip"):
                launch.build_command("detect-imx500", ["--model-dir", directory])

    def test_mac_imx_export_stops_before_running_any_conversion(self):
        with patch.object(launch.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(RuntimeError, "requires Linux"):
                launch.build_command("export-imx500", [])

    def test_export_refuses_to_replace_an_existing_ncnn_model(self):
        from testudo_training import export_ncnn
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            weights = Path(directory) / "best.pt"
            weights.touch()
            (weights.parent / "best_ncnn_model").mkdir()
            output = io.StringIO()
            with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
                export_ncnn.main(["--weights", str(weights)])
            self.assertIn("Export already exists", output.getvalue())


if __name__ == "__main__":
    unittest.main()
