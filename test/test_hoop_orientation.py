"""Known projected circles distinguish image rotation, tilt cues and 3D hypotheses."""
import math
import unittest
import cv2
import numpy as np

from raspberry_pi.hoop_vision.orientation import estimate_orientation, validate_calibration


class OrientationTests(unittest.TestCase):
    def test_uncalibrated_ratio_is_only_an_unsigned_shape_cue(self):
        result=estimate_orientation(((300,200),(100,200),70),(640,480))
        self.assertAlmostEqual(result['approx_obliqueness_deg'],60)
        self.assertFalse(result['orientation_valid'])
        self.assertFalse(result['signed_direction_resolved'])
        self.assertEqual(result['normal_hypotheses_camera'],[])

    def test_calibrated_projected_circles_include_true_camera_normal(self):
        k=np.array([[900.,0,640],[0,920,480],[0,0,1]])
        config={'image_size':[1280,960],'camera_matrix':k.tolist(),'dist_coeffs':[]}
        for yaw,pitch in ((0,0),(40,20),(-55,15),(65,-20)):
            y,p=map(math.radians,(yaw,pitch))
            ry=np.array([[math.cos(y),0,math.sin(y)],[0,1,0],[-math.sin(y),0,math.cos(y)]])
            rx=np.array([[1,0,0],[0,math.cos(p),-math.sin(p)],[0,math.sin(p),math.cos(p)]])
            rotation=np.einsum('ij,jk->ik',ry,rx)
            normal=rotation[:,2]
            t=np.linspace(0,2*np.pi,180,endpoint=False)
            circle=np.column_stack((.1524*np.cos(t),.1524*np.sin(t),np.zeros(len(t))))
            image,_=cv2.projectPoints(circle,cv2.Rodrigues(rotation)[0],np.array([.1,-.05,1.5]),k,None)
            ellipse=cv2.fitEllipseDirect(image.astype(np.float32))
            result=estimate_orientation(ellipse,(1280,960),config)
            normals=[np.array(h['normal']) for h in result['normal_hypotheses_camera']]
            error=min(math.degrees(math.acos(np.clip(np.dot(n,normal),-1,1))) for n in normals)
            self.assertLess(error,.1,(yaw,pitch,result))
            self.assertFalse(result['orientation_valid'])

    def test_wrong_resolution_and_invalid_intrinsics_do_not_claim_pose(self):
        config={'image_size':[640,480],'camera_matrix':[[500,0,320],[0,500,240],[0,0,1]]}
        result=estimate_orientation(((100,100),(80,100),0),(1280,960),config)
        self.assertEqual(result['status'],'calibration_size_mismatch')
        config['camera_matrix'][0][0]=0
        with self.assertRaises(ValueError):validate_calibration(config)


if __name__=='__main__':unittest.main()
