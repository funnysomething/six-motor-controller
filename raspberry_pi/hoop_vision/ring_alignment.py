"""Current-image boundary pairing and advisory alignment cues, never a passage clearance."""
import math
import cv2
import numpy as np
from .color import white_mask
from .geometry import ellipse_points,sample,overlap


def canonical(c):
    a,b=sorted(c.axes_px,reverse=True)
    angle=(c.image_rotation_deg+(90 if c.axes_px[0]<c.axes_px[1] else 0))%180
    return (c.center_px,(a,b),angle)


def packed(c):
    center,axes,angle=canonical(c)
    return dict(center_px=list(center),axes_px=list(axes),image_rotation_deg=float(angle))


def inside(points,ellipse):
    center,axes,angle=ellipse;t=math.radians(angle);c,s=math.cos(t),math.sin(t)
    xy=(points-center)@np.array([[c,-s],[s,c]])
    return np.sum((xy/(np.array(axes)/2))**2,axis=1)<=1.05


def annotate_alignment(image,observation,tracker,timestamp,config,age_seconds=0.):
    result=dict(view='unknown',pair_status='unresolved',near_front_on=False,
                alignment_valid=False,passage_clearance_valid=False,axes=None,
                reason='No current supported hoop geometry')
    observation.alignment=result
    if (timestamp is None or not math.isfinite(timestamp) or not math.isfinite(age_seconds) or
            age_seconds<0 or age_seconds>tracker.config.max_age_seconds):
        tracker.alignment_memory=None
        return result
    h,w=image.shape[:2];scale=min(1.,640/max(w,h))
    frame=cv2.resize(image,(round(w*scale),round(h*scale)))
    mapping=np.array([frame.shape[1]/w,frame.shape[0]/h])
    mask=white_mask(cv2.cvtColor(frame,cv2.COLOR_BGR2HSV),config.saturation_max,config.value_min)
    target=observation.accepted_target
    if target is None:
        tracker.alignment_memory=None
        # A narrow white strip alone is not proof of a hoop. It is useful only
        # as a short-lived advisory near an independently established track.
        prior=next((t for t in tracker.tracks if t['id']==tracker.selected_id and t['ever_confirmed'] and
                    timestamp-t['observed']<=tracker.search_horizon(t)),None)
        if prior is not None:
            count,labels,stats,centers=cv2.connectedComponentsWithStats((mask*255).astype(np.uint8))
            for index in range(1,count):
                x,y,bw,bh,area=stats[index]
                if area<frame.shape[0]*frame.shape[1]*.003:continue
                ys,xs=np.nonzero(labels[y:y+bh,x:x+bw]==index)
                pts=np.column_stack((xs+x,ys+y)).astype(np.float32)
                rect=cv2.minAreaRect(pts);long=max(rect[1]);short=min(rect[1])
                box=np.array([x,y,x+bw,y+bh])/np.tile(mapping,2)
                previous=np.array(prior['last_candidate'].bbox_xyxy)
                intersection=np.maximum(0,np.minimum(box[2:],previous[2:])-np.maximum(box[:2],previous[:2]))
                contained=float(np.prod(intersection)/max(1.,np.prod(box[2:]-box[:2])))
                if (short>0 and long/short>=3.5 and contained>.8 and
                        x>1 and y>1 and x+bw<frame.shape[1]-2 and y+bh<frame.shape[0]-2):
                    result.update(view='side_view_candidate',reason='Narrow white band near the previous hoop; aperture unresolved',
                                  side_band_box_xyxy=box.tolist())
                    break
        return result
    candidates=[c for c in observation.candidates if (c.verification or {}).get('passed') is True and not c.clipped]
    pairs=[]
    for outer in candidates:
        eo=canonical(outer)
        if overlap(outer.bbox_xyxy,target['bbox_xyxy'])<.2:continue
        for inner in candidates:
            if inner is outer:continue
            ei=canonical(inner)
            ratios=np.array(ei[1])/eo[1]
            if not (np.all(ratios<.96) and np.all(ratios>.3)):continue
            if np.linalg.norm(np.array(ei[0])-eo[0])>.25*eo[1][0]:continue
            if inside(ellipse_points(ei),eo).mean()<.9:continue
            difference=abs((eo[2]-ei[2]+90)%180-90)
            if min(ei[1])/max(ei[1])<.9 and difference>18:continue
            # Material must occupy the gap, not merely the area on the far side
            # of either ellipse. Perspective does not require common centers.
            po=ellipse_points(eo);pi=ellipse_points(ei);support=[]
            for fraction in (.25,.5,.75):
                values,valid=sample(mask,(pi*(1-fraction)+po*fraction)*mapping)
                support.append((values>.5)&valid)
            white=np.sum(support,axis=0)>=2
            sectors=sum(s.mean()>=.5 for s in np.array_split(white,8))
            if white.mean()<.65 or sectors<6:continue
            # Boundary polarity distinguishes white band from a dark ring
            # surrounded by a white background.
            if outer.verification.get('white_side')!=1 or inner.verification.get('white_side')!=-1:continue
            score=(float(white.mean())+.2*(outer.score+inner.score)-.2*np.linalg.norm(np.array(ei[0])-eo[0])/eo[1][0]
                   -3*abs(ei[1][1]/ei[1][0]-eo[1][1]/eo[1][0]))
            pairs.append((score,inner,outer,float(white.mean())))
    if pairs:
        _,inner,outer,support=max(pairs,key=lambda p:p[0])
        selected=packed(inner)
        result.update(pair_status='image_inner_outer_pair',inner=packed(inner),outer=packed(outer),band_white_support=support)
    else:
        selected=target['ellipse']
    axes=selected['axes_px'];ratio=min(axes)/max(axes)
    previous=getattr(tracker,'alignment_memory',None)
    same=previous is not None and previous['id']==target['track_id'] and 0<timestamp-previous['time']<=.3
    front_threshold=.96 if same and previous['front'] else .98
    outer_ratio=(min(result['outer']['axes_px'])/max(result['outer']['axes_px']) if pairs else 0.)
    front=(result['pair_status']=='image_inner_outer_pair' and min(ratio,outer_ratio)>=front_threshold and
           target.get('boundary_stable',True))
    dwell=(previous['since'] if same and previous['front'] and front else timestamp)
    tracker.alignment_memory=dict(id=target['track_id'],time=timestamp,front=front,since=dwell)
    confirmed_front=front and timestamp-dwell>=.1-1e-9
    side_threshold=.4 if same and previous.get('side',False) else .3
    side=ratio<side_threshold;tracker.alignment_memory['side']=side
    view='mostly_side_on' if side else 'near_front_on' if confirmed_front else 'oblique' if pairs else 'unpaired'
    center=np.array(selected['center_px']);angle=0. if confirmed_front else math.radians(selected['image_rotation_deg'])
    directions=np.array([[math.cos(angle),math.sin(angle)],[-math.sin(angle),math.cos(angle)]])
    endpoints=[[(center-directions[i]*axes[i]/2).tolist(),(center+directions[i]*axes[i]/2).tolist()] for i in (0,1)]
    error=center-np.array([w/2,h/2])
    result.update(view=view,near_front_on=confirmed_front,axis_ratio=float(ratio),
                  center_error_px=error.tolist(),center_error_normalized=(error/[w,h]).tolist(),
                  centered_image=bool(np.linalg.norm(error/[w,h])<.02),
                  axes=dict(center_px=center.tolist(),diameters_px=list(axes),lines_px=endpoints),
                  reason='Image shape cue only; physical rim, calibrated pose and vehicle clearance remain unverified')
    return result
