#!/usr/bin/env python3
"""Measure coarse proposal overlap, not pose accuracy or detection reliability."""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

from raspberry_pi.hoop_vision.geometry import overlap
from raspberry_pi.hoop_vision import DetectorConfig, detect
from raspberry_pi.hoop_vision.replay import frames, apply_verification_decision
from raspberry_pi.hoop_vision.verification import verify, VerificationConfig
from raspberry_pi.hoop_vision.tracker import HoopTracker, TrackerConfig


def evaluate(observations, annotations, tolerance=.11):
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Timestamp tolerance must be finite and nonnegative")
    groups = defaultdict(list)
    for row in observations:
        groups[row["source"]].append(row)
    results = []
    for label in annotations["labels"]:
        rows = groups[label["source"]]
        if not rows:
            raise ValueError(f"No observations for {label['source']}")
        if label["time_s"] is None:
            matches = [r for r in rows if r["source_time_s"] is None]
            if len(matches) != 1:
                raise ValueError("Still label requires exactly one observation")
            row = matches[0]
        else:
            video_rows = [r for r in rows if r["source_time_s"] is not None]
            if not video_rows:
                raise ValueError("Video label has no timestamped observations")
            row = min(video_rows,key=lambda r:abs(r["source_time_s"]-label["time_s"]))
            if abs(row["source_time_s"]-label["time_s"]) > tolerance:
                raise ValueError(f"No observation within {tolerance}s of {label}")
        w,h = row["frame_size"]
        box = [v*size for v,size in zip(label["box"],(w,h,w,h))]
        ious = [overlap(c["bbox_xyxy"],box) for c in row["candidates"]]
        accepted = row.get("accepted_target") if row.get("decision") == "accepted" else None
        accepted_box = box
        accepted_reference = "coarse_silhouette"
        if accepted and accepted["kind"] in label.get("measurement_boxes",{}):
            accepted_box = [v*s for v,s in zip(label["measurement_boxes"][accepted["kind"]],(w,h,w,h))]
            accepted_reference = accepted["kind"]
        results.append(dict(source=label["source"],time_s=row["source_time_s"],condition=label["condition"],
                            proposals=len(ious),top1_iou=ious[0] if ious else 0.,best_iou=max(ious,default=0.),
                            accepted=accepted is not None,
                            accepted_iou=overlap(accepted["bbox_xyxy"],accepted_box) if accepted else None,
                            accepted_reference=accepted_reference,
                            acceptance_allowed=label.get("acceptance_allowed",True)))
    def aggregate(rows):
        return dict(labeled_views=len(rows),top1_iou_ge_03=sum(r["top1_iou"]>=.3 for r in rows),
                    any_proposal_iou_ge_03=sum(r["best_iou"]>=.3 for r in rows),
                    any_proposal_iou_ge_05=sum(r["best_iou"]>=.5 for r in rows))
    accepted_views=[r for r in results if r["accepted"]]
    temporal=tracking_metrics(observations)
    temporal.update(labeled_accepted=len(accepted_views),labeled_total=len(results),
                    labeled_accepted_coarse_matches=sum(r["accepted_iou"]>=.3 for r in accepted_views),
                    labeled_forbidden_accepts=sum(not r["acceptance_allowed"] for r in accepted_views))
    return dict(annotation_method=annotations["annotation_method"],tracking=temporal,
                caveat="Coarse band/region proposal overlap on development footage, not detector precision, pose accuracy, or independent generalization. A matched box can still have incorrect rim geometry.",
                photos=aggregate([r for r in results if r["time_s"] is None]),
                video=aggregate([r for r in results if r["time_s"] is not None]),
                by_condition={k:aggregate([r for r in results if r["condition"]==k]) for k in sorted({r["condition"] for r in results})},
                views=results)


def tracking_metrics(observations):
    counts=defaultdict(int)
    segments=[]
    current=None
    previous=None
    last_id=None
    id_changes=0
    reasons=defaultdict(int)
    for row in observations:
        decision=row.get("decision","unknown")
        counts[decision]+=1
        reasons[row.get("tracking_reason","tracking_disabled")]+=1
        timestamp=row.get("source_time_s")
        target=row.get("accepted_target")
        identity=row.get("source_id",row["source"])
        if previous is not None and identity != previous:
            last_id=None
        previous=identity
        if decision=="accepted" and target and timestamp is not None:
            track_id=target.get("track_id")
            if last_id is not None and track_id != last_id:
                id_changes+=1
            last_id=track_id
            if current is None or current["source_id"]!=identity or current["track_id"]!=track_id:
                current=dict(source_id=identity,track_id=track_id,start_s=timestamp,end_s=timestamp,frames=0)
                segments.append(current)
            current["end_s"]=timestamp
            current["frames"]+=1
        else:
            current=None
    return dict(decision_counts=dict(counts),rejection_reasons=dict(reasons),accepted_segments=segments,
                accepted_track_id_changes=id_changes,
                caveat="Segments cover consecutive accepted sampled frames, not inferred validity during gaps. ID changes are diagnostic, not ground-truth identity errors. Unlabeled frames do not establish precision.")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations",type=Path)
    parser.add_argument("--labels",type=Path,default=Path(__file__).resolve().parents[2]/"test/fixtures/hoop-review-labels.json")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--tolerance",type=float,default=.11,help="maximum time difference in seconds")
    parser.add_argument("--media-root",type=Path,help="also test labeled target-free crops using the run's summary.json config")
    args=parser.parse_args()
    rows=[json.loads(line) for line in args.observations.read_text().splitlines() if line.strip()]
    annotations=json.loads(args.labels.read_text())
    result=evaluate(rows,annotations,args.tolerance)
    if args.media_root:
        summary=json.loads((args.observations.parent/"summary.json").read_text())
        config=DetectorConfig(**summary["config"])
        negatives=[]
        for region in annotations.get("negative_regions",[]):
            image,_,_=next(frames(args.media_root/region["source"]))
            h,w=image.shape[:2]
            x0,y0,x1,y1=[round(v*s) for v,s in zip(region["box"],(w,h,w,h))]
            proposal=detect(image[y0:y1,x0:x1],config)
            accepted=0
            if summary.get("tracking") or summary.get("verification_only"):
                verify(image[y0:y1,x0:x1],proposal,VerificationConfig(**summary["verification_config"]))
                if summary.get("tracking"):
                    tracker=HoopTracker(TrackerConfig(**summary["tracker_config"]))
                    for i in range(15):
                        tracker.update(proposal,i*.1)
                        accepted += proposal.decision=="accepted"
                else:
                    apply_verification_decision(proposal)
                    accepted=int(proposal.decision=="accepted")
            negatives.append(dict(**region,proposals=len(proposal.candidates),max_score=max((c.score for c in proposal.candidates),default=None),
                                  accepted_frames=accepted))
        result["negative_region_diagnostics"]=dict(crops=len(negatives),crops_with_proposals=sum(bool(n["proposals"]) for n in negatives),regions=negatives,
            crops_with_accepted_outputs=sum(n["accepted_frames"]>0 for n in negatives),
            caveat="Hand-selected development crops with no physical target. Tracking repeats each still 15 times at 10 fps; this is not natural video or a deployment false-positive rate.")
    with args.output.open("x") as file:
        file.write(json.dumps(result,indent=2,allow_nan=False)+"\n")
    print(json.dumps({k:result[k] for k in ("photos","video","by_condition")},indent=2))


if __name__=="__main__":
    main()
