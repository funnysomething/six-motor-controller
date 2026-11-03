"""Hardware-free geometry, rejection, source-orientation, and replay tests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from raspberry_pi.hoop_vision import DetectorConfig, detect
from raspberry_pi.hoop_vision.geometry import ellipse_points, overlap
from raspberry_pi.hoop_vision.replay import frames, main, run
from scripts.vision.evaluate_hoop import evaluate


def ring(center=(320,240), axes=(110,160), angle=0):
    image = np.full((480,640,3),(120,30,10),np.uint8)
    cv2.ellipse(image,center,axes,angle,0,360,(235,235,235),22)
    return image


class DetectorTests(unittest.TestCase):
    def test_ring_proposals_localize_target_without_inventing_pose(self):
        result = detect(ring())
        self.assertTrue(result.candidates)
        self.assertTrue(any(np.linalg.norm(np.array(c.center_px)-(320,240)) < 12 for c in result.candidates))
        self.assertFalse(result.pose_valid)
        self.assertFalse(result.target_identity_verified)
        self.assertIsNone(result.yaw_deg)
        self.assertIsNone(result.pitch_deg)
        self.assertIsNone(result.center_m)
        json.dumps(result.to_dict(),allow_nan=False)

    def test_image_rotation_is_not_yaw(self):
        result = detect(ring(angle=40))
        self.assertTrue(result.candidates)
        self.assertIsNone(result.yaw_deg)
        self.assertFalse(result.pose_valid)

    def test_two_rings_remain_unverified_alternatives(self):
        image = np.full((640,640,3),(120,30,10),np.uint8)
        for y in (170,470):
            cv2.ellipse(image,(320,y),(105,130),0,0,360,(240,240,240),18)
        result = detect(image)
        ys = [c.center_px[1] for c in result.candidates]
        self.assertTrue(any(y < 220 for y in ys))
        self.assertTrue(any(y > 420 for y in ys))
        self.assertFalse(result.target_identity_verified)

    def test_uniform_frames_have_no_proposals(self):
        for color in ((0,0,0),(255,255,255),(200,40,10)):
            with self.subTest(color=color):
                self.assertFalse(detect(np.full((480,640,3),color,np.uint8)).candidates)

    def test_solid_disk_does_not_supply_an_opening(self):
        image = np.zeros((480,640,3),np.uint8)
        cv2.circle(image,(320,240),150,(240,240,240),-1)
        self.assertFalse(detect(image).candidates)

    def test_resize_maps_ellipse_back_to_original_image(self):
        image = cv2.resize(ring(),(1281,961))
        result = detect(image)
        self.assertEqual(result.frame_size,(1281,961))
        self.assertTrue(any(np.linalg.norm(np.array(c.center_px)-(640.5,480.5)) < 25 for c in result.candidates))

    def test_clipped_views_never_supply_pose(self):
        result = detect(ring(center=(30,240)))
        self.assertFalse(result.pose_valid)
        self.assertIsNone(result.passage_axis)

    def test_invalid_config_and_input(self):
        for kwargs in ({"max_dimension":64},{"saturation_max":256},{"min_edge_coverage":0},{"max_candidates":0}):
            with self.assertRaises(ValueError):
                DetectorConfig(**kwargs)
        for image in (None,np.zeros((8,8),np.uint8),np.zeros((8,8,3),float)):
            with self.assertRaises(ValueError):
                detect(image)

    def test_ellipse_sampling_and_box_overlap(self):
        p = ellipse_points(((10,20),(8,4),0),count=4)
        np.testing.assert_allclose(p,((14,20),(10,22),(6,20),(10,18)))
        self.assertAlmostEqual(overlap((0,0,10,10),(5,0,15,10)),1/3)


class ReplayTests(unittest.TestCase):
    def test_exif_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"rotated.jpg"
            image = Image.new("RGB",(40,80),"blue")
            exif = image.getexif()
            exif[274] = 6
            image.save(path,exif=exif)
            frame,timestamp,kind = next(frames(path))
            self.assertEqual(frame.shape[:2],(40,80))
            self.assertIsNone(timestamp)
            self.assertEqual(kind,"still_image")

    def test_timestamp_sampling_preserves_irregular_source_times(self):
        times = [0.,.04,.105,.21,.22,.41]
        class Capture:
            released = False
            def __init__(self): self.index = -1
            def isOpened(self): return True
            def set(self,*args): return True
            def get(self,prop): return 25. if prop == cv2.CAP_PROP_FPS else times[self.index]*1000
            def read(self):
                self.index += 1
                return (True,ring()) if self.index < len(times) else (False,None)
            def release(self): self.released = True
        capture = Capture()
        with patch("raspberry_pi.hoop_vision.replay.cv2.VideoCapture",return_value=capture):
            sampled = list(frames(Path("fake.mov"),10))
        self.assertEqual([x[1] for x in sampled],[0.,.105,.21,.41])
        self.assertTrue(capture.released)

    def test_missing_timestamps_are_explicit_fallback(self):
        class Capture:
            def __init__(self): self.i=0
            def isOpened(self): return True
            def set(self,*args): pass
            def get(self,p): return 10. if p == cv2.CAP_PROP_FPS else 0.
            def read(self):
                self.i += 1
                return (True,ring()) if self.i <= 3 else (False,None)
            def release(self): pass
        with patch("raspberry_pi.hoop_vision.replay.cv2.VideoCapture",return_value=Capture()):
            result = list(frames("fake.mov",0))
        self.assertEqual([x[1] for x in result],[0.,.1,.2])
        self.assertEqual(result[-1][2],"frame_index_fps_fallback")

    def test_replay_exports_oriented_results_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"input.png"
            cv2.imwrite(str(path),ring())
            output = Path(directory)/"results"
            summary = run([path],output,DetectorConfig())
            self.assertEqual(summary["processed_frames"],1)
            row = json.loads((output/"observations.jsonl").read_text())
            self.assertIsNone(row["yaw_deg"])
            self.assertEqual(row["source"],"input.png")
            self.assertTrue((output/"photos-review-1.jpg").is_file())
            with self.assertRaises(FileExistsError):
                run([path],output,DetectorConfig())

    def test_cli_rejects_missing_input_and_nonfinite_fps(self):
        for argv in (["/nonexistent","--output","/unused"],
                     ["/nonexistent","--output","/unused","--sample-fps","nan"]):
            with self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertEqual(caught.exception.code,1)


class EvaluationTests(unittest.TestCase):
    def test_accepted_opening_uses_opening_reference_instead_of_band_silhouette(self):
        labels={"annotation_method":"test","labels":[{"source":"a.png","time_s":None,
                "box":[0,0,1,1],"measurement_boxes":{"apparent_opening":[.3,.3,.7,.7]},"condition":"test"}]}
        rows=[{"source":"a.png","source_time_s":None,"frame_size":[100,100],"candidates":[],
               "decision":"accepted","accepted_target":{"kind":"apparent_opening","bbox_xyxy":[30,30,70,70]}}]
        result=evaluate(rows,labels)
        self.assertEqual(result["tracking"]["labeled_accepted_coarse_matches"],1)
        self.assertEqual(result["views"][0]["accepted_reference"],"apparent_opening")

    def test_overlap_metrics_do_not_turn_proposals_into_pose_accuracy(self):
        labels={"annotation_method":"test", "labels":[{"source":"a.png","time_s":None,"box":[.1,.1,.9,.9],"condition":"test"}]}
        rows=[{"source":"a.png","source_time_s":None,"frame_size":[100,100],"candidates":[{"bbox_xyxy":[0,0,10,10]},{"bbox_xyxy":[10,10,90,90]}]}]
        result=evaluate(rows,labels)
        self.assertEqual(result["photos"]["top1_iou_ge_03"],0)
        self.assertEqual(result["photos"]["any_proposal_iou_ge_05"],1)

    def test_missing_or_distant_video_observations_fail_evaluation(self):
        labels={"annotation_method":"test","labels":[{"source":"a.mov","time_s":3.,"box":[0,0,1,1],"condition":"test"}]}
        with self.assertRaises(ValueError):
            evaluate([],labels)
        rows=[{"source":"a.mov","source_time_s":2.,"frame_size":[100,100],"candidates":[]}]
        with self.assertRaises(ValueError):
            evaluate(rows,labels)
        with self.assertRaises(ValueError):
            evaluate(rows,labels,float("nan"))


if __name__ == "__main__":
    unittest.main()
