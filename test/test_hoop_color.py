"""HSV range boundaries, white-background distractors and current-frame gating."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from raspberry_pi.hoop_vision.color import white_mask
from raspberry_pi.hoop_vision.detector import DetectorConfig, detect
from raspberry_pi.hoop_vision.geometry import bbox
from raspberry_pi.hoop_vision.live import main as live_main
from raspberry_pi.hoop_vision.replay import main as replay_main
from raspberry_pi.hoop_vision.tracker import HoopTracker, TrackerConfig
from raspberry_pi.hoop_vision.types import Candidate, Observation
from raspberry_pi.hoop_vision.verification import VerificationConfig, verify


def scene(color=(235,235,235), background=35, end=360):
    image = np.full((480,640,3), background, np.uint8)
    cv2.ellipse(image, (320,240), (120,150), 20, 0, end, color, 20)
    return image


def pipeline(image):
    return verify(image, detect(image, DetectorConfig(rim_hypotheses=True, max_candidates=18)),
                  VerificationConfig(profile="ellipse"))


class WhiteColorTests(unittest.TestCase):
    def test_white_range_includes_all_hues_and_exact_sv_boundaries(self):
        hsv = np.array([[[h,60,160] for h in (0,30,60,90,120,150,179)]+[[0,61,255],[0,0,159]]], np.uint8)
        np.testing.assert_array_equal(white_mask(hsv), [[1,1,1,1,1,1,1,0,0]])

    def test_well_lit_white_and_small_white_balance_shift_can_confirm(self):
        for color in ((235,235,235),(160,160,160),(205,215,230)):
            with self.subTest(color=color):
                tracker = HoopTracker(TrackerConfig.ellipse())
                tracker.update(pipeline(scene(color)), 0.)
                result = tracker.update(pipeline(scene(color)), .11)
                self.assertEqual(result.decision, "accepted")
                self.assertFalse(result.pose_valid)

    def test_saturated_colors_and_dark_gray_are_rejected_on_dark_and_white_backgrounds(self):
        for background in (35,245):
            for color in ((0,0,240),(0,240,0),(240,0,0),(0,240,240),
                          (240,0,240),(240,240,0),(120,120,120),(170,170,240)):
                with self.subTest(color=color, background=background):
                    self.assertFalse(detect(scene(color, background), DetectorConfig(rim_hypotheses=True)).candidates)

    def test_geometry_scores_and_confirmed_refit_cannot_override_current_color(self):
        for profile in ("strict","ellipse"):
            for source in ("detector","confirmed_temporal_refit"):
                for kind in ("material_outline","apparent_opening","rim_arc"):
                    ellipse = ((320,240),(260,320),20)
                    c = Candidate(*ellipse, bbox(ellipse), .99,.99,.99,.1,False,kind,source=source)
                    observation = Observation((640,480),[c])
                    check = verify(scene((0,0,240)), observation, VerificationConfig(profile=profile)).candidates[0].verification
                    self.assertFalse(check["passed"], (profile,source,kind,check))
                    self.assertFalse(check["white_color_passed"])
                    self.assertIn("outside_white_hsv_range",check["reasons"])

    def test_paired_boundaries_cannot_rescue_a_colored_rim(self):
        candidates = []
        for axes in ((260,320),(220,280)):
            ellipse = ((320,240),axes,20)
            candidates.append(Candidate(*ellipse,bbox(ellipse),.99,.99,.99,.1,False,"rim_arc"))
        result = verify(scene((0,240,0)), Observation((640,480),candidates), VerificationConfig(profile="ellipse"))
        self.assertTrue(all(not c.verification["passed"] for c in result.candidates))

    def test_confirmed_refit_cannot_borrow_a_white_background(self):
        for profile in ("strict","ellipse"):
            ellipse = ((320,240),(260,320),20)
            candidate = Candidate(*ellipse,bbox(ellipse),.99,.99,.99,.1,False,
                                  "rim_arc",source="confirmed_temporal_refit")
            result = verify(scene((0,0,240),245), Observation((640,480),[candidate]),
                            VerificationConfig(profile=profile))
            check = result.candidates[0].verification
            self.assertFalse(check["passed"])
            self.assertIn("unbounded_white_background", check["reasons"])
            self.assertFalse(check["white_color_passed"])

    def test_sparse_white_highlights_on_a_colored_ring_are_not_a_white_band(self):
        image = scene((0,0,240))
        cv2.ellipse(image,(320,240),(120,150),20,0,75,(235,235,235),20)
        self.assertFalse(detect(image, DetectorConfig(rim_hypotheses=True)).candidates)

    def test_current_color_loss_immediately_withholds_an_established_track(self):
        tracker = HoopTracker(TrackerConfig.ellipse())
        tracker.update(pipeline(scene()), 0.)
        self.assertEqual(tracker.update(pipeline(scene()), .11).decision, "accepted")
        result = tracker.update(pipeline(scene((0,0,240))), .22)
        self.assertEqual(result.decision,"unknown")
        self.assertIsNone(result.accepted_target)

    def test_custom_hsv_thresholds_are_used_in_replay_proposals_and_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory)/"ring.png"
            output = Path(directory)/"result"
            cv2.imwrite(str(image),scene())
            with contextlib.redirect_stdout(io.StringIO()):
                replay_main([str(image),"--output",str(output),"--tracking","--saturation-max","35","--value-min","200"])
            summary = json.loads((output/"summary.json").read_text())
            for config in (summary["config"],summary["verification_config"]):
                self.assertEqual(config["saturation_max"],35)
                self.assertEqual(config["value_min"],200)

    def test_invalid_hsv_settings_fail_before_camera_discovery(self):
        with patch("raspberry_pi.hoop_vision.live.build_bridge") as build:
            for flag,value in (("--saturation-max","256"),("--value-min","-1"),("--min-white-support","nan")):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(live_main(["--camera-name","iPhone",flag,value]),1)
            build.assert_not_called()
        for config in (DetectorConfig,VerificationConfig):
            for changes in (dict(value_min=True),dict(saturation_max=1.5),dict(min_white_support=0),dict(min_white_sectors=9)):
                with self.assertRaises(ValueError):
                    config(**changes)


if __name__ == "__main__":
    unittest.main()
