"""Local continuity, weaker current evidence, expiry and obstruction regression tests."""

import json
import unittest

import cv2
import numpy as np

from raspberry_pi.hoop_vision.detector import DetectorConfig, detect
from raspberry_pi.hoop_vision.ellipse_guidance import guided_candidates
from raspberry_pi.hoop_vision.geometry import bbox
from raspberry_pi.hoop_vision.types import Candidate
from raspberry_pi.hoop_vision.tracker import HoopTracker, TrackerConfig
from raspberry_pi.hoop_vision.verification import VerificationConfig, verify
from test.test_hoop_ellipse_tracking import qualified
from test.test_hoop_tracking import obs


def maintenance(x=300):
    c = qualified(x,quality=.55)
    c.verification.update(passed=False,strong=False,maintenance_passed=True,white_color_passed=True,
                          oriented_coverage=.4,sectors=4,material_support=.7,polarity=.25,
                          reasons=['insufficient_rim_edges'])
    return c


class ContinuityTests(unittest.TestCase):
    def acquire(self, tracker, x=300):
        tracker.update(obs(qualified(x)),0.)
        result=tracker.update(obs(qualified(x)),.11)
        self.assertEqual(result.decision,'accepted')
        return result.accepted_target['track_id']

    def test_nearby_weaker_current_fit_beats_stronger_distant_target(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        identity=self.acquire(tracker)
        for i in range(2,6):
            result=tracker.update(obs(maintenance(300+i),qualified(570,quality=.99)),i*.11)
            self.assertEqual(result.decision,'accepted')
            self.assertEqual(result.accepted_target['track_id'],identity)
            self.assertEqual(result.accepted_target['evidence'],'local_maintenance')
            self.assertLess(result.accepted_target['ellipse']['center_px'][0],320)
            self.assertFalse(result.pose_valid)

    def test_weak_evidence_never_initializes_a_track(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        for i in range(20):
            self.assertEqual(tracker.update(obs(maintenance()),i*.1).decision,'unknown')
        self.assertEqual(tracker.tracks,[])

    def test_local_lock_selects_same_boundary_instead_of_larger_high_score_ellipse(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        identity=self.acquire(tracker)
        for i in range(2,10):
            large=qualified(300,quality=.99,kind='apparent_opening')
            large.axes_px=(245,310)
            large.bbox_xyxy=(177.5,85,422.5,395)
            result=tracker.update(obs(large,qualified(301,quality=.65)),i*.1)
            self.assertEqual(result.accepted_target['track_id'],identity)
            self.assertEqual(result.accepted_target['kind'],'material_outline')
            self.assertEqual(result.accepted_target['ellipse']['axes_px'],[180,240])

    def test_blank_frame_withholds_output_but_internal_track_survives_short_gap(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        identity=self.acquire(tracker)
        missing=tracker.update(obs(),.25)
        self.assertEqual(missing.decision,'unknown')
        self.assertIsNone(missing.accepted_target)
        self.assertEqual(missing.tracks[0]['state'],'coasting')
        self.assertEqual(tracker.update(obs(qualified()),.5).decision,'unknown')
        result=tracker.update(obs(qualified()),.61)
        self.assertEqual(result.decision,'accepted')
        self.assertEqual(result.accepted_target['track_id'],identity)

    def test_distant_target_cannot_steal_lock_when_original_is_obstructed(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        self.acquire(tracker)
        for t in (.2,.3,.5,.7):
            result=tracker.update(obs(qualified(580)),t)
            self.assertEqual(result.decision,'unknown')
        # Once the bounded lock expires, a different target needs acquisition.
        self.assertEqual(tracker.update(obs(qualified(580)),1.).decision,'unknown')
        result=tracker.update(obs(qualified(580)),1.11)
        self.assertEqual(result.decision,'accepted')
        self.assertGreater(result.accepted_target['ellipse']['center_px'][0],500)

    def test_two_local_current_weak_observations_can_reconfirm_after_a_short_miss(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        identity=self.acquire(tracker)
        self.assertEqual(tracker.update(obs(),.2).decision,'unknown')
        self.assertEqual(tracker.update(obs(maintenance()),.3).decision,'unknown')
        result=tracker.update(obs(maintenance()),.4)
        self.assertEqual(result.decision,'accepted')
        self.assertEqual(result.accepted_target['track_id'],identity)

    def test_local_matching_does_not_fail_because_of_old_box_filter_covariance(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        identity=self.acquire(tracker)
        for t,x in ((.22,330),(.33,360),(.44,390)):
            result=tracker.update(obs(qualified(x)),t)
            self.assertEqual(result.decision,'accepted')
            self.assertEqual(result.accepted_target['track_id'],identity)

    def test_color_failure_clipping_and_absent_edges_cannot_use_maintenance(self):
        for change in ('color','clipped','no_measurement'):
            tracker=HoopTracker(TrackerConfig.ellipse())
            self.acquire(tracker)
            c=maintenance()
            if change=='color': c.verification['white_color_passed']=False
            if change=='clipped': c.clipped=True
            if change=='no_measurement': c.verification['maintenance_passed']=False
            result=tracker.update(obs(c),.22)
            self.assertEqual(result.decision,'unknown',change)

    def test_weak_updates_have_a_time_limit_and_cannot_refresh_their_own_authority(self):
        tracker=HoopTracker(TrackerConfig.ellipse(maintenance_seconds=.35))
        self.acquire(tracker)
        self.assertEqual(tracker.update(obs(maintenance()),.22).decision,'accepted')
        self.assertEqual(tracker.update(obs(maintenance()),.4).decision,'accepted')
        self.assertEqual(tracker.update(obs(maintenance()),.5).decision,'unknown')
        self.assertEqual(tracker.update(obs(maintenance()),.6).decision,'unknown')

    def test_strong_observation_refreshes_maintenance_budget(self):
        tracker=HoopTracker(TrackerConfig.ellipse(maintenance_seconds=.3))
        self.acquire(tracker)
        tracker.update(obs(maintenance()),.3)
        tracker.update(obs(qualified()),.4)
        self.assertEqual(tracker.update(obs(maintenance()),.6).decision,'accepted')

    def test_weak_matches_cannot_gradually_walk_away_from_last_strong_hoop(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        self.acquire(tracker)
        self.assertEqual(tracker.update(obs(maintenance(330)),.22).decision,'accepted')
        self.assertEqual(tracker.update(obs(maintenance(360)),.33).decision,'accepted')
        self.assertEqual(tracker.update(obs(maintenance(390)),.44).decision,'unknown')

    def test_closer_current_fit_wins_over_high_scoring_nearby_distractor(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        identity=self.acquire(tracker)
        result=tracker.update(obs(qualified(350,quality=.99),qualified(302,quality=.65)),.22)
        self.assertEqual(result.accepted_target['track_id'],identity)
        self.assertEqual(result.accepted_target['ellipse']['center_px'],[302,240])

    def test_strong_local_boundary_change_keeps_object_but_withholds_tilt(self):
        tracker=HoopTracker(TrackerConfig.ellipse())
        identity=self.acquire(tracker)
        def larger():
            c=qualified(kind='apparent_opening')
            c.axes_px=(300,360);c.bbox_xyxy=(150,60,450,420)
            return c
        result=tracker.update(obs(larger()),.22)
        self.assertEqual(result.decision,'accepted')
        self.assertEqual(result.accepted_target['track_id'],identity)
        self.assertFalse(result.accepted_target['boundary_stable'])
        self.assertIsNone(result.image_orientation)
        self.assertIsNone(tracker.update(obs(larger()),.33).image_orientation)
        self.assertIsNotNone(tracker.update(obs(larger()),.44).image_orientation)

    def test_strict_and_explicitly_disabled_profiles_do_not_use_weaker_gate(self):
        tracker=HoopTracker(TrackerConfig.ellipse(continuity_lock=False))
        self.acquire(tracker)
        self.assertEqual(tracker.update(obs(maintenance()),.22).decision,'unknown')
        self.assertFalse(TrackerConfig().continuity_lock)

    def test_slow_motion_at_five_ten_and_fifteen_fps(self):
        for fps in (5,10,15):
            tracker=HoopTracker(TrackerConfig.ellipse())
            ids=[]
            for i in range(fps*2):
                result=tracker.update(obs(qualified(300+i*2)),i/fps)
                if result.accepted_target: ids.append(result.accepted_target['track_id'])
            self.assertGreater(len(ids),fps)
            self.assertEqual(len(set(ids)),1)

    def test_bad_timestamps_and_stale_measurements_do_not_inherit_lock(self):
        for timestamp,provenance,age in ((.2,'decoder_pts',.3),(.11,'decoder_pts',0),(.2,'guessed',0)):
            tracker=HoopTracker(TrackerConfig.ellipse())
            self.acquire(tracker)
            self.assertEqual(tracker.update(obs(maintenance()),timestamp,provenance,age).decision,'unknown')

    def test_stale_strong_frame_cannot_renew_weak_maintenance_budget(self):
        tracker=HoopTracker(TrackerConfig.ellipse(maintenance_seconds=.35))
        self.acquire(tracker)
        tracker.update(obs(qualified()),.4,age_seconds=.3)
        self.assertEqual(tracker.update(obs(maintenance()),.5).decision,'unknown')
        self.assertEqual(tracker.update(obs(maintenance()),.6).decision,'unknown')

    def test_verifier_does_not_enable_maintenance_for_blank_colored_or_clipped_rings(self):
        for color in ((0,0,0),(0,0,240),(235,235,235)):
            image=np.full((480,640,3),35,np.uint8)
            if color!=(0,0,0):
                cv2.ellipse(image,(300,240),(80,110),0,0,360,color,20)
            c=qualified()
            if color==(235,235,235): c.clipped=True
            check=verify(image,obs(c),VerificationConfig(profile='ellipse')).candidates[0].verification
            self.assertFalse(check['maintenance_passed'])

    def test_slightly_inaccurate_white_ring_fit_is_retained_using_actual_pixels(self):
        image=np.full((480,640,3),35,np.uint8)
        cv2.ellipse(image,(320,240),(120,150),0,0,360,(235,235,235),20)
        def measurement(dx=0,frame=image):
            e=((320+dx,240),(260,320),0)
            c=Candidate(*e,bbox(e),.8,.8,.8,.1,False,'material_outline')
            return verify(frame,obs(c),VerificationConfig(profile='ellipse'))
        tracker=HoopTracker(TrackerConfig.ellipse())
        tracker.update(measurement(),0.)
        identity=tracker.update(measurement(),.11).accepted_target['track_id']
        inaccurate=measurement(8)
        self.assertFalse(inaccurate.candidates[0].verification['passed'])
        self.assertTrue(inaccurate.candidates[0].verification['maintenance_passed'])
        result=tracker.update(inaccurate,.22)
        self.assertEqual(result.decision,'accepted')
        self.assertEqual(result.accepted_target['track_id'],identity)
        self.assertEqual(result.accepted_target['evidence'],'local_maintenance')
        hidden=measurement(8,np.full_like(image,35))
        self.assertEqual(tracker.update(hidden,.33).decision,'unknown')

    def test_guidance_can_search_retained_lock_but_never_returns_a_blank_prediction(self):
        image=np.full((480,640,3),35,np.uint8)
        cv2.ellipse(image,(320,240),(100,140),0,0,360,(235,235,235),20)
        tracker=HoopTracker(TrackerConfig.ellipse())
        def measurement():
            return verify(image,detect(image,DetectorConfig(rim_hypotheses=True)),VerificationConfig(profile='ellipse'))
        tracker.update(measurement(),0.)
        self.assertEqual(tracker.update(measurement(),.11).decision,'accepted')
        tracker.update(obs(),.3)
        self.assertTrue(guided_candidates(image,tracker,.6))
        self.assertFalse(guided_candidates(np.zeros_like(image),tracker,.6))

    def test_validation_and_observations_stay_json_finite(self):
        for changes in (dict(continuity_lock=1),dict(lock_center_gate=0),dict(lock_hold_seconds=3),dict(maintenance_seconds=float('nan'))):
            with self.assertRaises(ValueError): TrackerConfig.ellipse(**changes)
        tracker=HoopTracker(TrackerConfig.ellipse())
        self.acquire(tracker)
        json.dumps(tracker.update(obs(maintenance()),.22).to_dict(),allow_nan=False)


if __name__=='__main__': unittest.main()
