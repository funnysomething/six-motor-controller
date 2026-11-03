"""Refit a remembered ellipse to current edges; never emit a prediction as a detection."""

import cv2
import numpy as np

from .geometry import ellipse_points, bbox, overlap, sample
from .types import Candidate


def guided_candidates(image, tracker, timestamp):
    if timestamp is None or tracker.last_time is None or timestamp <= tracker.last_time:
        return []
    h,w=image.shape[:2]
    scale=min(1.,640/max(h,w))
    frame=cv2.resize(image,(round(w*scale),round(h*scale)))
    gray=cv2.GaussianBlur(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),(3,3),0)
    gx=cv2.Sobel(gray,cv2.CV_32F,1,0);gy=cv2.Sobel(gray,cv2.CV_32F,0,1)
    result=[]
    for track in tracker.tracks:
        old=track.get("last_candidate",track.get("candidate"))
        if old is None or timestamp-track['observed']>tracker.config.max_gap_seconds:
            continue
        points=ellipse_points((old.center_px,old.axes_px,old.image_rotation_deg))*scale
        dt=timestamp-track['observed']
        points += np.array(track['x'][4:6])*[w,h]*scale*dt
        tangent=np.roll(points,-1,axis=0)-np.roll(points,1,axis=0)
        normals=np.column_stack((-tangent[:,1],tangent[:,0]))
        normals/=np.maximum(1e-6,np.linalg.norm(normals,axis=1))[:,None]
        scores=[];probes=[]
        for offset in range(-10,11):
            probe=points+offset*normals
            x,valid=sample(gx,probe);y,_=sample(gy,probe)
            strength=np.abs(x*normals[:,0]+y*normals[:,1])
            scores.append(np.where(valid & (strength>=40),strength/(1+.12*abs(offset)),0))
            probes.append(probe)
        scores=np.array(scores);best=scores.argmax(axis=0)
        valid=scores.max(axis=0)>0
        if valid.sum()<40:
            continue
        chosen=np.array(probes)[best,np.arange(len(points))][valid]
        try:
            ellipse=cv2.fitEllipseDirect(chosen.astype(np.float32).reshape(-1,1,2))
        except cv2.error:
            continue
        restored=(tuple(v/scale for v in ellipse[0]),tuple(v/scale for v in ellipse[1]),ellipse[2])
        box=bbox(restored)
        if min(restored[1])<20/scale or overlap(box,old.bbox_xyxy)<.45:
            continue
        coverage=float(valid.mean())
        result.append(Candidate(restored[0],restored[1],restored[2],tuple(map(float,box)),
             float(.4*coverage+.2*old.material_support+.4*min(1.,np.pi*np.prod(restored[1])/4/(w*h*.2))),
             coverage,old.material_support,old.interior_white_fraction,
             bool(box[0]<0 or box[1]<0 or box[2]>=w or box[3]>=h),old.kind,
             source="confirmed_temporal_refit" if track['ever_confirmed'] else "temporal_refit"))
    return result
