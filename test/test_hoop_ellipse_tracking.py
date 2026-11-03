"""Faster ellipse-first acquisition and edge-refitting regression checks."""
import json
import unittest
import cv2
import numpy as np

from raspberry_pi.hoop_vision import detect,DetectorConfig
from raspberry_pi.hoop_vision.types import Observation
from raspberry_pi.hoop_vision.tracker import HoopTracker,TrackerConfig
from raspberry_pi.hoop_vision.verification import verify,VerificationConfig
from raspberry_pi.hoop_vision.ellipse_guidance import guided_candidates
from test.test_hoop_tracking import candidate,obs


def qualified(x=300,quality=.9,kind='material_outline'):
    c=candidate(x,kind=kind)
    c.verification['quality_score']=quality
    return c


class EllipseTrackingTests(unittest.TestCase):
    def test_acquires_in_two_frames_and_retains_current_ellipse(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        self.assertEqual(tracker.update(obs(qualified()),0.).decision,'unknown')
        result=tracker.update(obs(qualified(302)),.1)
        self.assertEqual(result.decision,'accepted')
        self.assertIn('ellipse',result.accepted_target)
        self.assertEqual(result.image_orientation['status'],'uncalibrated_shape_cue')
        self.assertFalse(result.pose_valid)

    def test_kind_changes_preserve_object_history_without_indefinite_covariance(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        for i in range(100):
            c=qualified(300+i*.1,kind='material_outline' if i%2 else 'apparent_opening')
            result=tracker.update(obs(c),i*.1)
            for track in tracker.tracks:
                self.assertGreaterEqual(np.linalg.eigvalsh(track['p']).min(),-1e-10)
            json.dumps(result.to_dict(),allow_nan=False)
        self.assertEqual(result.decision,'accepted')

    def test_weaker_distractor_does_not_reset_an_established_track(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        tracker.update(obs(qualified()),0.)
        first=tracker.update(obs(qualified()),.1)
        result=tracker.update(obs(qualified(),qualified(580,.67)),.2)
        self.assertEqual(result.decision,'accepted')
        self.assertEqual(first.accepted_target['track_id'],result.accepted_target['track_id'])

    def test_far_opening_does_not_steal_an_established_identity(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        tracker.update(obs(qualified(kind='apparent_opening')),0.)
        tracker.update(obs(qualified(kind='apparent_opening')),.1)
        result=tracker.update(obs(qualified(kind='apparent_opening'),qualified(580,kind='apparent_opening')),.2)
        self.assertEqual(result.decision,'accepted')
        self.assertLess(result.accepted_target['bbox_xyxy'][0],400)

    def test_two_distinct_openings_cannot_initialize_an_identity(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        for i in range(10):
            result=tracker.update(obs(qualified(kind='apparent_opening'),qualified(580,kind='apparent_opening')),i*.1)
            self.assertEqual(result.decision,'unknown')

    def test_guidance_requires_current_edges_and_emits_serializable_candidates(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        image=np.full((480,640,3),(120,30,10),np.uint8)
        cv2.ellipse(image,(320,240),(120,170),0,0,360,(240,240,240),20)
        observation=verify(image,detect(image),VerificationConfig())
        c=next(c for c in observation.candidates if c.verification['strong'])
        tracker.update(obs(c),0.)
        guides=guided_candidates(image,tracker,.1)
        self.assertTrue(guides)
        json.dumps(Observation((640,480),guides).to_dict(),allow_nan=False)
        self.assertEqual(guided_candidates(np.zeros_like(image),tracker,.1),[])

    def test_fast_profile_does_not_turn_a_filled_disk_into_a_hoop(self):
        image=np.zeros((480,640,3),np.uint8)
        cv2.circle(image,(320,240),150,(240,240,240),-1)
        o=verify(image,detect(image,DetectorConfig(rim_hypotheses=True,max_candidates=18)),VerificationConfig(profile='ellipse'))
        self.assertFalse(any(c.verification['passed'] for c in o.candidates))


if __name__=='__main__':unittest.main()
