"""Pixel-based checks reject fabricated support scores and incomplete arcs."""

import unittest

import cv2
import numpy as np

from raspberry_pi.hoop_vision.types import Candidate, Observation
from raspberry_pi.hoop_vision.verification import verify
from raspberry_pi.hoop_vision.replay import apply_verification_decision


class VerificationTests(unittest.TestCase):
    def scene(self, end=360, filled=False):
        image=np.full((480,640,3),(120,30,10),np.uint8)
        cv2.ellipse(image,(320,240),(120,170),0,0,end,(240,240,240),-1 if filled else 20)
        c=Candidate((320,240),(260,360),0,(190,60,450,420),.99,.99,.99,.1,False,"material_outline")
        return image,Observation((640,480),[c])

    def test_complete_band_has_distributed_boundary_evidence(self):
        image,obs=self.scene()
        check=verify(image,obs).candidates[0].verification
        self.assertTrue(check["passed"],check)
        self.assertTrue(check["strong"],check)
        self.assertFalse(obs.pose_valid)

    def test_short_arc_and_uniform_image_do_not_inherit_high_candidate_score(self):
        for end in (40,90):
            image,obs=self.scene(end)
            self.assertFalse(verify(image,obs).candidates[0].verification["passed"])
        image,obs=self.scene()
        image[:]=255
        self.assertFalse(verify(image,obs).candidates[0].verification["passed"])

    def test_solid_disk_is_not_a_visible_opening(self):
        image,obs=self.scene(filled=True)
        self.assertIn("no_visible_aperture_evidence",verify(image,obs).candidates[0].verification["reasons"])

    def test_clipped_and_coordinate_mismatch_rejected(self):
        image,obs=self.scene()
        obs.candidates[0].clipped=True
        self.assertFalse(verify(image,obs).candidates[0].verification["passed"])
        obs.frame_size=(1,1)
        with self.assertRaises(ValueError): verify(image,obs)

    def test_material_check_tolerates_modest_brightness_change(self):
        for shift in (-20,20):
            image,obs=self.scene()
            adjusted=np.clip(image.astype(np.int16)+shift,0,255).astype(np.uint8)
            self.assertTrue(verify(adjusted,obs).candidates[0].verification["passed"])

    def test_circular_boundary_does_not_need_a_stable_ellipse_angle(self):
        image=np.full((480,640,3),(120,30,10),np.uint8)
        cv2.circle(image,(320,240),140,(240,240,240),20)
        for angle in (0,89,179):
            c=Candidate((320,240),(300,300),angle,(170,90,470,390),.99,.99,.99,.1,False,"material_outline")
            observation=Observation((640,480),[c])
            self.assertTrue(verify(image,observation).candidates[0].verification["passed"])

    def test_reverification_clears_a_previously_accepted_frame(self):
        image,obs=self.scene()
        verify(image,obs)
        apply_verification_decision(obs)
        self.assertEqual(obs.decision,"accepted")
        image[:]=0
        verify(image,obs)
        apply_verification_decision(obs)
        self.assertEqual(obs.decision,"unknown")
        self.assertIsNone(obs.accepted_target)


if __name__=="__main__": unittest.main()
