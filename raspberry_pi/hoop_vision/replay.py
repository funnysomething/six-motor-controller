"""File replay with display orientation, source timestamps, and bounded previews."""

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import platform
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .detector import DetectorConfig, detect
from .geometry import ellipse_points
from .tracker import HoopTracker, TrackerConfig
from .verification import verify, VerificationConfig
from .types import Candidate, Observation
from .ellipse_guidance import guided_candidates
from .orientation import estimate_orientation, validate_calibration
from .color import WHITE_SATURATION_MAX, WHITE_VALUE_MIN, WHITE_MIN_SUPPORT


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".avi", ".mkv", ".m4v"}


def frames(path, sample_fps=10.):
    """Yield oriented BGR, source seconds and timestamp provenance.

    Sampling drops frames by timestamp; it does not alter source time. No sleep
    or frame queue is used. VideoCapture applies display rotation explicitly.
    """
    if not math.isfinite(sample_fps) or sample_fps < 0:
        raise ValueError("sample_fps must be finite and >= 0; zero processes every frame")
    path = Path(path)
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        with Image.open(path) as raw:
            rgb = np.asarray(ImageOps.exif_transpose(raw).convert("RGB"))
        yield cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), None, "still_image"
        return
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported media: {path}")
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError(f"Cannot decode video: {path}")
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
        fps = capture.get(cv2.CAP_PROP_FPS)
        next_time, previous, index = 0., -1., 0
        while True:
            ok, image = capture.read()
            if not ok:
                if index == 0:
                    raise ValueError(f"No decodable frames: {path}")
                break
            timestamp = capture.get(cv2.CAP_PROP_POS_MSEC)/1000.
            provenance = "decoder_pts"
            if not math.isfinite(timestamp) or timestamp < 0 or (index and timestamp <= previous):
                if not math.isfinite(fps) or fps <= 0:
                    raise ValueError("Video has neither usable timestamps nor FPS")
                timestamp = max(index/fps, previous+1/fps)
                provenance = "frame_index_fps_fallback"
            previous = timestamp
            index += 1
            if sample_fps and timestamp+1e-9 < next_time:
                continue
            if sample_fps:
                next_time = (math.floor(timestamp*sample_fps+1e-8)+1)/sample_fps
            yield image, timestamp, provenance
    finally:
        capture.release()


def overlay(image, observation, label, max_dimension=800, debug=False, show_caption=True):
    h, w = image.shape[:2]
    scale = min(1., max_dimension/max(h,w))
    view = cv2.resize(image, (round(w*scale), round(h*scale)))
    sx, sy = view.shape[1]/w, view.shape[0]/h
    for i, candidate in enumerate(observation.candidates):
        if observation.schema_version == 2 and not debug:
            continue
        ellipse = (candidate.center_px, candidate.axes_px, candidate.image_rotation_deg)
        points = np.rint(ellipse_points(ellipse)*[sx,sy]).astype(np.int32)
        color = (0,220,255) if i == 0 else (255,170,40)
        cv2.polylines(view, [points], True, color, 2, cv2.LINE_AA)
        x, y = np.rint(np.array(candidate.center_px)*[sx,sy]).astype(int)
        cv2.putText(view, f"P{i} {candidate.score:.2f}", (x,y), cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1, cv2.LINE_AA)
    if observation.schema_version == 2:
        if debug:
            for track in observation.tracks:
                x0,y0,x1,y1 = np.rint(np.array(track["predicted_bbox_xyxy"])*[sx,sy,sx,sy]).astype(int)
                cv2.rectangle(view,(x0,y0),(x1,y1),(160,160,160),1)
                cv2.putText(view,f"T{track['track_id']} {track['state']}",(x0,max(12,y0)),0,.4,(180,180,180),1)
        if observation.accepted_target:
            x0,y0,x1,y1 = np.rint(np.array(observation.accepted_target["bbox_xyxy"])*[sx,sy,sx,sy]).astype(int)
            cv2.rectangle(view,(x0,y0),(x1,y1),(40,230,40),2)
            ellipse=observation.accepted_target.get('filtered_ellipse') or observation.accepted_target.get('ellipse')
            if ellipse:
                points=np.rint(ellipse_points((ellipse['center_px'],ellipse['axes_px'],ellipse['image_rotation_deg']))*[sx,sy]).astype(np.int32)
                cv2.polylines(view,[points],True,(40,230,40),2,cv2.LINE_AA)
    if not show_caption:
        return view
    # A separate caption strip never obscures the target.
    caption = np.zeros((66, view.shape[1], 3), np.uint8)
    status = (observation.decision+" | "+observation.tracking_reason if observation.schema_version == 2
              else f"{len(observation.candidates)} proposals | POSE UNAVAILABLE")
    legend = ("Green=current image measurement; gray=prediction; NO POSE" if observation.schema_version == 2
              else "Yellow = rank 1, not verified target | No motor output")
    if observation.image_orientation:
        orientation=observation.image_orientation
        legend=f"Approx tilt {orientation.get('approx_obliqueness_deg',0):.1f} deg | SIGN UNRESOLVED"
        if orientation['status']=='calibrated_circle_hypotheses':
            legend='Yaw hypotheses '+', '.join(f"{h['yaw_deg']:+.0f}" for h in orientation['normal_hypotheses_camera'])+' deg | UNRESOLVED'
    for line, text in enumerate((label, status, legend)):
        cv2.putText(caption, text, (8,17+20*line), cv2.FONT_HERSHEY_SIMPLEX, .4, (240,240,240), 1, cv2.LINE_AA)
    return np.vstack((caption,view))


def contact_sheet(images, destination, columns=4):
    """Bounded review pages; the caller limits the number of images."""
    sheet = Image.new("RGB", (columns*280, math.ceil(len(images)/columns)*380), "#202020")
    for i, path in enumerate(images):
        with Image.open(path) as image:
            thumb = image.copy()
        thumb.thumbnail((274,350))
        x,y = i%columns*280, i//columns*380
        sheet.paste(thumb,(x+(280-thumb.width)//2,y))
        ImageDraw.Draw(sheet).text((x+5,y+355),path.stem[:35],fill="white")
    sheet.save(destination)


def run(paths, output, config, sample_fps=10., save_video=False, tracking=False,
        debug=False, tracker_config=None, verification_config=None, verification_only=False, calibration=None):
    if calibration and not tracking:
        raise ValueError('Camera orientation calibration requires tracking mode')
    output = Path(output)
    timings, counts, media = [], Counter(), []
    tracker_config = tracker_config or TrackerConfig()
    verification_config = verification_config or VerificationConfig(
        profile=tracker_config.profile, saturation_max=config.saturation_max, value_min=config.value_min,
        min_white_support=config.min_white_support, min_white_sectors=config.min_white_sectors)
    if any(getattr(config,key)!=getattr(verification_config,key) for key in
           ("saturation_max","value_min","min_white_support","min_white_sectors")):
        raise ValueError("Detector and verification HSV white settings must match")
    output.mkdir(parents=True, exist_ok=False)
    temporal_timings, verification_timings, decisions = [], [], Counter()
    photo_previews, video_previews = [], []
    with (output/"observations.jsonl").open("w") as log:
        for path in paths:
            tracker = HoopTracker(tracker_config)
            identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]
            source_id = f"{path.stem}-{identity}"
            writer = None
            source_count = 0
            last_review = -math.inf
            frame_size = None
            try:
                for index, (image,timestamp,provenance) in enumerate(frames(path,sample_fps)):
                    started = time.perf_counter()
                    observation = detect(image,config)
                    if tracking and tracker_config.profile=="ellipse":
                        guides=guided_candidates(image,tracker,timestamp,verification_config)
                        observation.candidates=guides+observation.candidates
                    elapsed = (time.perf_counter()-started)*1000
                    timings.append(elapsed)
                    started = time.perf_counter()
                    if tracking or verification_only:
                        verify(image, observation, verification_config)
                    verification_ms = (time.perf_counter()-started)*1000
                    verification_timings.append(verification_ms)
                    started = time.perf_counter()
                    if tracking:
                        tracker.update(observation,timestamp,provenance)
                        if (calibration and observation.accepted_target and
                                observation.accepted_target.get('boundary_stable',True)):
                            e=observation.accepted_target['ellipse']
                            observation.image_orientation=estimate_orientation((e['center_px'],e['axes_px'],e['image_rotation_deg']),observation.frame_size,calibration)
                    elif verification_only:
                        apply_verification_decision(observation)
                    tracking_ms = (time.perf_counter()-started)*1000
                    temporal_timings.append(tracking_ms)
                    decisions[observation.decision] += 1
                    counts[observation.status] += 1
                    data = observation.to_dict()
                    data.update(source=path.name, source_id=source_id, frame_index=index,
                                source_time_s=timestamp, timestamp_provenance=provenance,
                                processing_ms=elapsed,verification_ms=verification_ms,tracking_ms=tracking_ms)
                    log.write(json.dumps(data,allow_nan=False)+"\n")
                    source_count += 1
                    frame_size = observation.frame_size
                    label = f"{path.name} | " + ("still" if timestamp is None else f"{timestamp:.3f}s")
                    view = overlay(image,observation,label,debug=debug)
                    save_review = timestamp is None or timestamp-last_review >= .98
                    if save_review:
                        filename = output/f"{source_id}-{index:05d}.jpg"
                        if not cv2.imwrite(str(filename),view):
                            raise OSError(f"Cannot write {filename}")
                        if timestamp is None:
                            photo_previews.append(filename)
                        else:
                            video_previews.append(filename)
                            last_review = timestamp
                    if save_video and timestamp is not None:
                        if writer is None:
                            if sample_fps <= 0:
                                raise ValueError("--save-video requires positive --sample-fps")
                            # MJPEG is portable on the Pi and desktop. Output is
                            # a review preview, not a source-timing measurement.
                            size = (view.shape[1]//2*2, view.shape[0]//2*2)
                            writer = cv2.VideoWriter(str(output/f"{source_id}.avi"),cv2.VideoWriter_fourcc(*"MJPG"),sample_fps,size)
                            if not writer.isOpened():
                                raise OSError("Cannot create MJPEG preview")
                        writer.write(view[:size[1],:size[0]])
            finally:
                if writer is not None:
                    writer.release()
            media.append(dict(source=path.name,source_id=source_id,processed_frames=source_count,frame_size=frame_size))
    if not timings:
        raise ValueError("No images or video frames processed")
    for prefix, previews in (("photos",photo_previews),("video",video_previews)):
        for start in range(0,len(previews),16):
            contact_sheet(previews[start:start+16],output/f"{prefix}-review-{start//16+1}.jpg")
    summary = dict(schema_version=1,config=asdict(config),sample_fps=sample_fps,
                   camera_calibration=calibration,
                   tracking=tracking,verification_only=verification_only,
                   tracker_config=asdict(tracker_config),verification_config=asdict(verification_config),
                   decision_counts=dict(decisions),
                   verification_ms={f"p{p}":float(np.percentile(verification_timings,p)) for p in (50,95,99)},
                   tracking_ms={f"p{p}":float(np.percentile(temporal_timings,p)) for p in (50,95,99)},
                   runtime=dict(python=platform.python_version(),opencv=cv2.__version__,numpy=np.__version__,machine=platform.machine()),
                   media=media,processed_frames=len(timings),status_counts=dict(counts),
                   processing_ms={f"p{p}":float(np.percentile(timings,p)) for p in (50,95,99)},
                   timing_scope="detect() only on this host; excludes decode, overlay, capture and transport",
                   pose_valid_frames=0, target_identity_verified_frames=0,
                   caveat="Development footage from a different camera outside the water. Scores are heuristic, not probabilities. No underwater or metric-pose validation.")
    (output/"summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n")
    return summary


def apply_verification_decision(observation):
    """Ablation mode: same hard image checks without temporal confirmation."""
    observation.schema_version = 2
    observation.decision = "unknown"
    observation.accepted_target = None
    eligible = [c for c in observation.candidates if (c.verification or {}).get("strong")]
    ambiguous = any(not HoopTracker._same_object(a,b) for i,a in enumerate(eligible) for b in eligible[i+1:])
    observation.tracking_reason = "ambiguous_targets" if ambiguous else "no_verified_measurement"
    if eligible and not ambiguous:
        c = eligible[0]
        observation.decision = "accepted"
        observation.accepted_target = dict(track_id=None,kind=c.kind,bbox_xyxy=list(c.bbox_xyxy))
        observation.tracking_reason = "strict_per_frame_image_only"


def replay_candidates(path, output, config=None):
    """Reproduce tracking from frozen, verified detections; v1 logs abstain."""
    output = Path(output)
    output.mkdir(parents=True,exist_ok=False)
    tracker = HoopTracker(config)
    source = None
    counts = Counter()
    with Path(path).open() as rows, (output/"observations.jsonl").open("w") as log:
        for line in rows:
            if not line.strip():
                continue
            row = json.loads(line)
            identity = row.get("source_id",row["source"])
            if identity != source:
                tracker.reset()
                source = identity
            obs = Observation(tuple(row["frame_size"]),[Candidate(**c) for c in row["candidates"]])
            obs.status = row.get("status","uncalibrated_candidates" if obs.candidates else "no_candidate")
            obs.reason = row.get("reason",obs.reason)
            if row.get("scene_cut"):
                tracker.reset()
            started = time.perf_counter()
            tracker.update(obs,row["source_time_s"],row.get("timestamp_provenance","untrusted"),
                           row.get("delivery_age_seconds",0.))
            row.update(obs.to_dict())
            row["tracking_ms"] = (time.perf_counter()-started)*1000
            row["candidate_replay"] = True
            log.write(json.dumps(row,allow_nan=False)+"\n")
            counts[obs.decision] += 1
    if not counts:
        raise ValueError("No candidate observations in input")
    result = dict(schema_version=2,tracking=True,decision_counts=dict(counts),
                  tracker_config=asdict(tracker.config),mode="frozen_candidate_replay")
    (output/"summary.json").write_text(json.dumps(result,indent=2)+"\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input",type=Path,help="image, video, or media directory")
    parser.add_argument("--output",required=True,type=Path,help="new output directory; existing results are never overwritten")
    parser.add_argument("--sample-fps",type=float,default=10.,help="video timestamp sampling, 0 for all frames")
    parser.add_argument("--max-dimension",type=int,default=640)
    parser.add_argument("--saturation-max",type=int,default=WHITE_SATURATION_MAX)
    parser.add_argument("--value-min",type=int,default=WHITE_VALUE_MIN)
    parser.add_argument("--min-white-support",type=float,default=WHITE_MIN_SUPPORT)
    parser.add_argument("--save-video",action="store_true")
    parser.add_argument("--tracking",action="store_true",help="verify boundaries and track; image decisions only")
    parser.add_argument("--tracking-profile",choices=("ellipse","strict"),default="ellipse")
    parser.add_argument("--no-continuity-lock",action="store_true",help="Disable the ellipse profile's slow-motion lock for comparison")
    parser.add_argument("--tracking-debug",action="store_true",help="also draw rejected candidates and predictions")
    parser.add_argument("--verification-only",action="store_true",help="strict per-frame comparison without tracking")
    parser.add_argument("--candidate-json",action="store_true",help="replay frozen candidate JSONL instead of media")
    parser.add_argument("--tracker-config",type=Path,help="JSON TrackerConfig overrides")
    parser.add_argument("--verification-config",type=Path,help="JSON VerificationConfig overrides; media replay only")
    parser.add_argument("--camera-calibration",type=Path,help="JSON image_size, camera_matrix, dist_coeffs for circle normal hypotheses")
    args = parser.parse_args(argv)
    try:
        overrides=json.loads(args.tracker_config.read_text()) if args.tracker_config else {}
        if args.no_continuity_lock:
            overrides['continuity_lock'] = False
        tracker_config = TrackerConfig.ellipse(**overrides) if args.tracking_profile=="ellipse" else TrackerConfig(**overrides)
        white_options = dict(saturation_max=args.saturation_max, value_min=args.value_min,
                             min_white_support=args.min_white_support)
        verification_options = dict(profile=args.tracking_profile, **white_options)
        if args.verification_config:
            verification_options.update(json.loads(args.verification_config.read_text()))
        verification_config = VerificationConfig(**verification_options)
        calibration=validate_calibration(json.loads(args.camera_calibration.read_text())) if args.camera_calibration else None
        if calibration and not args.tracking:
            raise ValueError('--camera-calibration requires --tracking')
        if tracker_config.profile!=args.tracking_profile or verification_config.profile!=args.tracking_profile:
            raise ValueError('Tracker and verification configuration profiles must match --tracking-profile')
        if args.candidate_json:
            if args.save_video or args.verification_only or args.verification_config or args.camera_calibration:
                raise ValueError("Candidate replay cannot render video or recompute verification")
            print(json.dumps(replay_candidates(args.input,args.output,tracker_config),indent=2))
            return 0
        if args.tracking and args.verification_only:
            raise ValueError("Choose --tracking or --verification-only")
        if not math.isfinite(args.sample_fps) or args.sample_fps < 0:
            raise ValueError("--sample-fps must be finite and >= 0")
        if args.save_video and args.sample_fps == 0:
            raise ValueError("--save-video requires positive --sample-fps")
        if args.input.is_dir():
            paths = sorted(p for p in args.input.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS and p.is_file())
        elif args.input.is_file():
            paths = [args.input]
        else:
            raise ValueError(f"Input does not exist: {args.input}")
        if not paths:
            raise ValueError("Input contains no supported media")
        summary = run(paths,args.output,DetectorConfig(max_dimension=args.max_dimension,
                      saturation_max=verification_config.saturation_max,value_min=verification_config.value_min,
                      min_white_support=verification_config.min_white_support,min_white_sectors=verification_config.min_white_sectors,
                      rim_hypotheses=(args.tracking or args.verification_only) and args.tracking_profile=="ellipse",
                      max_candidates=18 if (args.tracking or args.verification_only) and args.tracking_profile=="ellipse" else 6),args.sample_fps,args.save_video,
                      tracking=args.tracking,debug=args.tracking_debug,tracker_config=tracker_config,
                      verification_only=args.verification_only,verification_config=verification_config,calibration=calibration)
    except (ValueError,TypeError,OSError,cv2.error) as error:
        parser.exit(1,f"hoop replay: {error}\n")
    print(json.dumps(summary,indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
