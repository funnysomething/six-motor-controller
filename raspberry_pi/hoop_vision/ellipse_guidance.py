"""White-boundary local tracking with flow guidance and robust arc refitting."""
import cv2
import numpy as np
from .geometry import ellipse_points,bbox,overlap,sample
from .color import white_mask,WHITE_SATURATION_MAX,WHITE_VALUE_MIN
from .types import Candidate


def robust_fit(points):
    """Trim large geometric residuals, rather than fitting every selected edge."""
    kept=np.ones(len(points),bool)
    for _ in range(3):
        if kept.sum()<30:return None
        try:e=cv2.fitEllipseDirect(points[kept].astype(np.float32).reshape(-1,1,2))
        except cv2.error:return None
        if min(e[1])<=0 or not np.isfinite([*e[0],*e[1],e[2]]).all():return None
        t=np.deg2rad(e[2]);c,s=np.cos(t),np.sin(t)
        xy=(points-np.array(e[0]))@np.array([[c,-s],[s,c]])
        residual=np.abs(np.linalg.norm(xy/(np.array(e[1])/2),axis=1)-1)*min(e[1])/2
        median=np.median(residual[kept]);mad=np.median(np.abs(residual[kept]-median))
        updated=residual<=max(1.5,median+2.5*1.4826*mad)
        if np.array_equal(updated,kept):break
        kept=updated
    if kept.sum()<30:return None
    try:e=cv2.fitEllipseDirect(points[kept].astype(np.float32).reshape(-1,1,2))
    except cv2.error:return None
    if min(e[1])<=0 or not np.isfinite([*e[0],*e[1],e[2]]).all():return None
    return e,kept


def flow_shift(gray,previous,track,scale):
    """Forward/backward-checked sparse flow proposes translation, not identity."""
    if previous is None or previous.shape!=gray.shape:return None
    mask=np.zeros_like(gray)
    old=track['last_candidate']
    pts=np.rint(ellipse_points((old.center_px,old.axes_px,old.image_rotation_deg))*scale).astype(np.int32)
    cv2.polylines(mask,[pts],True,255,16)
    corners=cv2.goodFeaturesToTrack(previous,60,.02,5,mask=mask)
    if corners is None or len(corners)<6:return None
    forward,status,_=cv2.calcOpticalFlowPyrLK(previous,gray,corners,None,winSize=(21,21),maxLevel=3)
    if forward is None:return None
    back,reverse,_=cv2.calcOpticalFlowPyrLK(gray,previous,forward,None,winSize=(21,21),maxLevel=3)
    if back is None:return None
    valid=(status.ravel()!=0)&(reverse.ravel()!=0)&(np.linalg.norm((back-corners).reshape(-1,2),axis=1)<1.5)
    shifts=(forward-corners).reshape(-1,2)[valid]
    if len(shifts)<6:return None
    median=np.median(shifts,axis=0)
    inliers=np.linalg.norm(shifts-median,axis=1)<3
    if inliers.sum()<6 or inliers.mean()<.6:return None
    return np.median(shifts[inliers],axis=0)/scale


def guided_candidates(image,tracker,timestamp,color_config=None):
    if timestamp is None or tracker.last_time is None or timestamp<=tracker.last_time:return []
    h,w=image.shape[:2];scale=min(1.,640/max(h,w))
    gray=cv2.cvtColor(cv2.resize(image,(round(w*scale),round(h*scale))),cv2.COLOR_BGR2GRAY)
    previous=getattr(tracker,'flow_gray',None)
    previous_time=getattr(tracker,'flow_time',None)
    tracker.flow_gray=gray;tracker.flow_time=timestamp
    result=[]
    for track in tracker.tracks:
        old=track.get('last_candidate')
        dt=timestamp-track['observed']
        if old is None or dt>tracker.search_horizon(track):continue
        shift=np.array(track['x'][4:6])*[w,h]*dt
        if (previous_time is not None and 0<timestamp-previous_time<=.3 and
                abs(track['observed']-previous_time)<1e-6):
            motion=flow_shift(gray,previous,track,scale)
            if motion is not None and np.linalg.norm(motion/[w,h])<.1:shift=motion
        center=np.array(old.center_px)+shift
        box=np.array(old.bbox_xyxy)+np.tile(shift,2)
        pad=max(16.,.25*max(box[2]-box[0],box[3]-box[1]))
        x0,y0=np.maximum(0,np.floor(box[:2]-pad)).astype(int)
        x1,y1=np.minimum([w,h],np.ceil(box[2:]+pad)).astype(int)
        if x1-x0<32 or y1-y0<32:continue
        local_scale=min(1.,800/max(x1-x0,y1-y0))
        frame=cv2.resize(image[y0:y1,x0:x1],(round((x1-x0)*local_scale),round((y1-y0)*local_scale)))
        # Exact scales preserve coordinates even for odd crop dimensions.
        mapping=np.array([frame.shape[1]/(x1-x0),frame.shape[0]/(y1-y0)])
        white=white_mask(cv2.cvtColor(frame,cv2.COLOR_BGR2HSV),
            getattr(color_config,'saturation_max',WHITE_SATURATION_MAX),getattr(color_config,'value_min',WHITE_VALUE_MIN))
        blurred=cv2.GaussianBlur(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),(3,3),0)
        gx=cv2.Sobel(blurred,cv2.CV_32F,1,0);gy=cv2.Sobel(blurred,cv2.CV_32F,0,1)
        points=(ellipse_points((tuple(center),old.axes_px,old.image_rotation_deg))-[x0,y0])*mapping
        tangent=np.roll(points,-1,axis=0)-np.roll(points,1,axis=0)
        normals=np.column_stack((-tangent[:,1],tangent[:,0]));normals/=np.maximum(1e-6,np.linalg.norm(normals,axis=1))[:,None]
        expected=(old.verification or {}).get('white_side')
        scores=[];probes=[]
        for offset in range(-12,13):
            probe=points+offset*normals
            x,valid=sample(gx,probe);y,_=sample(gy,probe)
            signed=x*normals[:,0]+y*normals[:,1];strength=np.abs(signed)
            left,vl=sample(white,probe+4*normals);right,vr=sample(white,probe-4*normals)
            material=left if expected==1 else right if expected==-1 else np.maximum(left,right)
            polarity=(signed*expected>0) if expected in (-1,1) else np.ones(len(points),bool)
            alignment=strength/np.maximum(np.hypot(x,y),1e-6)
            supported=valid&vl&vr&(material>.5)&polarity&(strength>=40)&(alignment>=.7)
            scores.append(np.where(supported,strength*(.5+.5*np.abs(left-right))/(1+.18*abs(offset)),0));probes.append(probe)
        scores=np.array(scores);valid=scores.max(axis=0)>0
        if valid.sum()<40 or sum(s.mean()>=.4 for s in np.array_split(valid,8))<4:continue
        selected=np.array(probes)[scores.argmax(axis=0),np.arange(len(points))][valid]
        fitted=robust_fit(selected)
        if fitted is None:continue
        ellipse,inliers=fitted
        arc_inliers=np.zeros(len(points),bool);arc_inliers[np.flatnonzero(valid)[inliers]]=True
        if sum(s.mean()>=.4 for s in np.array_split(arc_inliers,8))<4:continue
        restored_points=ellipse_points(ellipse)/mapping+[x0,y0]
        restored=cv2.fitEllipse(restored_points.astype(np.float32).reshape(-1,1,2))
        box=bbox(restored)
        if min(restored[1])<20 or overlap(box,old.bbox_xyxy)<.35:continue
        coverage=float(arc_inliers.mean())
        result.append(Candidate(tuple(map(float,restored[0])),tuple(map(float,restored[1])),float(restored[2]),tuple(map(float,box)),
            float(.4*coverage+.2*old.material_support+.4*min(1.,np.pi*np.prod(restored[1])/4/(w*h*.2))),
            coverage,old.material_support,old.interior_white_fraction,
            bool(box[0]<2 or box[1]<2 or box[2]>=w-2 or box[3]>=h-2),old.kind,
            source='confirmed_temporal_refit' if track['ever_confirmed'] else 'temporal_refit'))
    return result
