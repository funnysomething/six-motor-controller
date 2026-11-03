"""Circle-plane orientation hypotheses. Never equate ellipse image rotation with yaw."""

import math
import cv2
import numpy as np

from .geometry import ellipse_points


def validate_calibration(data):
    size=data.get('image_size',[])
    k=np.asarray(data.get('camera_matrix'),dtype=float)
    d=np.asarray(data.get('dist_coeffs',[]),dtype=float)
    if (len(size)!=2 or any(type(x) is not int or x<=0 for x in size) or
            k.shape!=(3,3) or not np.isfinite(k).all() or k[0,0]<=0 or k[1,1]<=0 or
            not np.allclose(k[2],[0,0,1]) or d.size not in (0,4,5,8,12,14) or not np.isfinite(d).all()):
        raise ValueError('Calibration requires image_size [w,h], valid 3x3 camera_matrix and OpenCV dist_coeffs')
    return data


def circle_conic(ellipse):
    center=np.array(ellipse[0]);axes=np.array(ellipse[1])/2
    theta=math.radians(ellipse[2]);c,s=math.cos(theta),math.sin(theta)
    rotation=np.array([[c,-s],[s,c]])
    a=np.einsum('ij,jk,lk->il',rotation,np.diag(1/axes**2),rotation)
    q=np.zeros((3,3));q[:2,:2]=a
    q[:2,2]=-a@center;q[2,:2]=q[:2,2];q[2,2]=center@a@center-1
    return q


def estimate_orientation(ellipse, frame_size, calibration=None):
    axes=ellipse[1]
    if min(axes)<=0 or not all(math.isfinite(v) for v in (*ellipse[0],*axes,ellipse[2])):
        return {'status':'invalid_geometry','orientation_valid':False}
    ratio=min(axes)/max(axes)
    result=dict(status='uncalibrated_shape_cue',orientation_valid=False,
                axis_ratio=float(ratio),approx_obliqueness_deg=math.degrees(math.acos(min(1.,ratio))),
                major_axis_image_deg=float((ellipse[2]+(90 if axes[1]>axes[0] else 0))%180),
                assumption='one circular rim; mixed rims and perspective can bias this cue',
                normal_hypotheses_camera=[],signed_direction_resolved=False)
    if calibration is None:
        return result
    validate_calibration(calibration)
    if tuple(calibration['image_size'])!=tuple(frame_size):
        result['status']='calibration_size_mismatch'
        return result
    k=np.asarray(calibration['camera_matrix'],float)
    distortion=np.asarray(calibration.get('dist_coeffs',[]),float)
    points=ellipse_points(ellipse).astype(np.float64).reshape(-1,1,2)
    normalized=cv2.undistortPoints(points,k,distortion if distortion.size else None)
    fitted=cv2.fitEllipseDirect(normalized.astype(np.float32))
    values,vectors=np.linalg.eigh(circle_conic(fitted))
    low,middle,high=values
    if not low<0<middle<=high or high-low<=1e-12:
        result['status']='degenerate_conic'
        return result
    s=math.sqrt(max(0.,(high-middle)/(high-low)))
    c=math.sqrt(max(0.,(middle-low)/(high-low)))
    hypotheses=[]
    for sign in (-1,1):
        normal=sign*s*vectors[:,2]+c*vectors[:,0]
        if normal[2]<0:normal=-normal
        if any(np.linalg.norm(normal-np.array(h['normal']))<1e-6 for h in hypotheses):continue
        hypotheses.append(dict(normal=[float(x) for x in normal],
            yaw_deg=math.degrees(math.atan2(normal[0],normal[2])),
            elevation_deg=math.degrees(math.atan2(-normal[1],math.hypot(normal[0],normal[2]))),
            optical_axis_tilt_deg=math.degrees(math.acos(np.clip(normal[2],-1,1)))))
    result.update(status='calibrated_circle_hypotheses',normal_hypotheses_camera=hypotheses,
                  assumption='calibrated single circular rim; physical rim identity still unverified')
    return result
