"""Time-based image-ellipse smoothing; never supplies missing measurements."""
import math
import numpy as np


def update_geometry(track, candidate, timestamp, reset=False):
    a,b=sorted(candidate.axes_px)
    angle=math.radians(candidate.image_rotation_deg+(90 if candidate.axes_px[0]<candidate.axes_px[1] else 0))
    value=np.array([*candidate.center_px,math.log(a),math.log(b),math.cos(2*angle),math.sin(2*angle)])
    previous=track.get('ellipse_filter')
    quality=float(np.clip((candidate.verification or {}).get('oriented_coverage',.5),.1,1.))
    if previous is None or reset or timestamp-previous['time']>.3:
        filtered=value
    else:
        dt=max(0.,timestamp-previous['time'])
        alpha=1-math.exp(-dt/(.10+.20*(1-quality)))
        filtered=previous['value']*(1-alpha)+value*alpha
        # A circle carries essentially no useful axis-direction information.
        if a/b>.95:filtered[4:]=previous['value'][4:]
    track['ellipse_filter']=dict(value=filtered,time=timestamp)
    return dict(center_px=filtered[:2].tolist(),axes_px=np.exp(filtered[2:4]).tolist(),
                image_rotation_deg=float((math.degrees(math.atan2(filtered[5],filtered[4]))/2-90)%180))
