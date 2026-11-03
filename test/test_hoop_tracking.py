"""Deterministic tracking sequences; no hardware or physical identity claims."""

import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from raspberry_pi.hoop_vision.types import Candidate, Observation
from raspberry_pi.hoop_vision.tracker import HoopTracker, TrackerConfig
from raspberry_pi.hoop_vision.replay import replay_candidates, run
from raspberry_pi.hoop_vision import DetectorConfig


def candidate(x=300, kind="material_outline", strong=True, passed=True):
    return Candidate((x,240),(180,240),0,(x-90,120,x+90,360),.95,.95,.9,.2,False,kind,
                     dict(passed=passed,strong=strong,oriented_coverage=.95))


def obs(*items):
    return Observation((640,480),list(items))


class TrackingTests(unittest.TestCase):
    def acquire(self, tracker, start=0):
        for i in range(6):
            result=tracker.update(obs(candidate()),start+i*.1)
        self.assertEqual(result.decision,"accepted")
        return result

    def test_dwell_uses_elapsed_time_not_just_frames(self):
        tracker=HoopTracker()
        for i in range(20):
            self.assertEqual(tracker.update(obs(candidate()),i*.01).decision,"unknown")
        for i in range(20,51):
            result=tracker.update(obs(candidate()),i*.01)
        self.assertEqual(result.decision,"accepted")
        self.assertFalse(result.pose_valid)
        self.assertFalse(result.detection_valid)
        self.assertIsNone(result.yaw_deg)

    def test_motion_and_rank_changes_keep_identity(self):
        tracker=HoopTracker()
        ids=[]
        for i in range(15):
            result=tracker.update(obs(candidate(300+i*2),candidate(570,passed=False)),i*.1)
            if result.accepted_target:
                ids.append(result.accepted_target["track_id"])
        self.assertTrue(ids)
        self.assertEqual(len(set(ids)),1)

    def test_miss_immediately_withholds_then_requires_reconfirmation(self):
        tracker=HoopTracker()
        first=self.acquire(tracker).accepted_target["track_id"]
        missing=tracker.update(obs(),.6)
        self.assertEqual(missing.decision,"unknown")
        self.assertIsNone(missing.accepted_target)
        self.assertEqual(missing.tracks[0]["state"],"coasting")
        self.assertEqual(tracker.update(obs(candidate()),.7).decision,"unknown")
        self.assertEqual(tracker.update(obs(candidate()),.8).decision,"unknown")
        result=tracker.update(obs(candidate()),.9)
        self.assertEqual(result.accepted_target["track_id"],first)

    def test_uncertainty_grows_during_gap_and_lost_tracks_expire(self):
        tracker=HoopTracker()
        result=self.acquire(tracker)
        uncertainty=result.tracks[0]["position_uncertainty"]
        gap=tracker.update(obs(),.7)
        self.assertGreater(gap.tracks[0]["position_uncertainty"],uncertainty)
        self.assertEqual(tracker.update(obs(),1.01).tracks[0]["state"],"lost")
        self.assertFalse(tracker.update(obs(),1.6).tracks)

    def test_competing_persistent_targets_never_force_selection(self):
        tracker=HoopTracker()
        for i in range(20):
            result=tracker.update(obs(candidate(160),candidate(500)),i*.1)
            self.assertEqual(result.decision,"unknown")
            self.assertIsNone(result.accepted_target)

    def test_new_distractor_interrupts_an_already_confirmed_track(self):
        tracker=HoopTracker()
        self.acquire(tracker)
        result=tracker.update(obs(candidate(),candidate(560)),.6)
        self.assertEqual(result.decision,"unknown")
        self.assertIn("ambiguous",result.tracking_reason)
        self.assertEqual(tracker.update(obs(candidate()),.7).decision,"unknown")

    def test_weak_candidates_cannot_acquire_but_can_maintain(self):
        tracker=HoopTracker()
        for i in range(8):
            self.assertEqual(tracker.update(obs(candidate(strong=False)),i*.1).decision,"unknown")
        self.assertFalse(tracker.tracks)
        self.acquire(tracker,start=1.)
        self.assertEqual(tracker.update(obs(candidate(strong=False)),1.6).decision,"accepted")

    def test_proposal_kind_changes_are_not_blended(self):
        tracker=HoopTracker()
        self.acquire(tracker)
        result=tracker.update(obs(candidate(kind="apparent_opening")),.6)
        self.assertEqual(result.decision,"unknown")
        self.assertEqual(len(result.tracks),2)

    def test_duplicate_proposals_do_not_create_false_ambiguity(self):
        tracker=HoopTracker()
        for i in range(8):
            result=tracker.update(obs(candidate(),candidate(302)),i*.1)
        self.assertEqual(result.decision,"accepted")
        self.assertEqual(len(result.tracks),1)

    def test_bad_timestamps_and_stale_delivery_never_accept(self):
        for bad in (None,math.nan,math.inf,-1.):
            tracker=HoopTracker()
            self.acquire(tracker)
            result=tracker.update(obs(candidate()),bad)
            self.assertEqual(result.decision,"unknown")
            self.assertFalse(result.tracks)
        tracker=HoopTracker()
        self.acquire(tracker)
        self.assertEqual(tracker.update(obs(candidate()),.6,age_seconds=.3).decision,"unknown")
        self.assertEqual(tracker.update(obs(candidate()),.6).tracking_reason,"timestamp_discontinuity")
        self.assertEqual(tracker.update(obs(candidate()),.7,"frame_index_fps_fallback").tracking_reason,"untrusted_timestamp")

    def test_source_reset_and_resolution_change_cannot_reuse_acceptance(self):
        tracker=HoopTracker()
        self.acquire(tracker)
        wrong=obs(candidate())
        wrong.frame_size=(1280,960)
        self.assertEqual(tracker.update(wrong,.6).tracking_reason,"coordinate_system_changed")
        tracker.reset()
        self.assertEqual(tracker.update(obs(candidate()),0.).decision,"unknown")

    def test_rejected_outlier_does_not_update_prediction(self):
        tracker=HoopTracker()
        self.acquire(tracker)
        result=tracker.update(obs(candidate(600,passed=False)),.6)
        box=result.tracks[0]["predicted_bbox_xyxy"]
        self.assertAlmostEqual((box[0]+box[2])/2,300)

    def test_irregular_intervals_at_multiple_rates(self):
        for fps in (5,10,15):
            tracker=HoopTracker()
            for i in range(fps+1):
                t=i/fps + (.007 if i%2 else 0)
                result=tracker.update(obs(candidate(300+t*5)),t)
            self.assertEqual(result.decision,"accepted")

    def test_config_validation(self):
        for kwargs in ({"max_tracks":0},{"acquire_seconds":math.nan},{"retain_seconds":.1}):
            with self.assertRaises(ValueError): TrackerConfig(**kwargs)

    def test_nonfinite_quality_and_invalid_coordinates_fail_closed(self):
        tracker=HoopTracker()
        bad=candidate()
        bad.verification["oriented_coverage"]=math.nan
        self.assertEqual(tracker.update(obs(bad),0.).decision,"unknown")
        self.assertFalse(tracker.tracks)
        wrong=obs(candidate())
        wrong.frame_size=(0,480)
        self.assertEqual(tracker.update(wrong,.1).tracking_reason,"invalid_frame_size")

    def test_consumption_expiry_when_camera_stops_or_a_newer_frame_rejects(self):
        tracker=HoopTracker()
        result=self.acquire(tracker)
        self.assertIsNotNone(tracker.target_at(result,.6))
        self.assertIsNone(tracker.target_at(result,.8))
        self.assertIsNone(tracker.target_at(result,.4))
        tracker.update(obs(),.6)
        self.assertIsNone(tracker.target_at(result,.61))

    def test_filter_reduces_measurement_jitter_without_fabricating_pose(self):
        tracker=HoopTracker()
        rng=np.random.default_rng(42)
        raw,filtered=[],[]
        for i in range(100):
            result=tracker.update(obs(candidate(300+i*.4+rng.normal(0,5))),i*.1)
            if result.accepted_target:
                a=result.accepted_target
                raw.append((a["bbox_xyxy"][0]+a["bbox_xyxy"][2])/2-(300+i*.4))
                filtered.append((a["filtered_bbox_xyxy"][0]+a["filtered_bbox_xyxy"][2])/2-(300+i*.4))
        self.assertGreater(len(filtered),50)
        self.assertLess(np.std(filtered),np.std(raw))

    def test_media_replay_keeps_one_tracker_per_video_and_exports_unknown_on_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            def fake_frames(path, sample_fps):
                for i in range(7):
                    yield np.zeros((480,640,3),np.uint8),i*.1,"decoder_pts"
            def fake_detect(image,config):
                return obs(candidate())
            with patch("raspberry_pi.hoop_vision.replay.frames",side_effect=fake_frames), \
                 patch("raspberry_pi.hoop_vision.replay.detect",side_effect=fake_detect), \
                 patch("raspberry_pi.hoop_vision.replay.verify",side_effect=lambda image,o,c:o):
                run([Path("a.mov"),Path("b.mov")],Path(directory)/"out",DetectorConfig(),tracking=True)
            rows=[json.loads(r) for r in (Path(directory)/"out/observations.jsonl").read_text().splitlines()]
            self.assertEqual(rows[5]["decision"],"accepted")
            self.assertEqual(rows[7]["decision"],"unknown")
            self.assertTrue(all(r["schema_version"]==2 and r["pose_valid"] is False for r in rows))

    def test_frozen_json_replay_resets_per_source_and_rejects_unverified_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            rows=[]
            for source in ("a","b"):
                for i in range(6):
                    row=obs(candidate()).to_dict()
                    row.update(source=source,source_id=source,source_time_s=i*.1,timestamp_provenance="decoder_pts")
                    rows.append(row)
            (root/"input.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            replay_candidates(root/"input.jsonl",root/"output")
            output=[json.loads(r) for r in (root/"output/observations.jsonl").read_text().splitlines()]
            self.assertEqual(output[5]["decision"],"accepted")
            self.assertEqual(output[6]["decision"],"unknown")
            for r in rows: r["candidates"][0].pop("verification",None)
            (root/"v1.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            result=replay_candidates(root/"v1.jsonl",root/"v1-output")
            self.assertEqual(result["decision_counts"],{"unknown":12})


if __name__=="__main__": unittest.main()
