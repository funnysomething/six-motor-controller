import unittest
import tempfile
import json
from pathlib import Path
import numpy as np
from raspberry_pi.calibrate_control import fit_gyro, fit_accel, fit_mag, main
from raspberry_pi.stabilization import VehicleConfig


class CalibrationTests(unittest.TestCase):
    def test_stationary_gyro_bias(self):
        bias,matrix=fit_gyro(np.tile((.4,-.2,.8),(300,1)),np.linspace(0,6,300))
        np.testing.assert_allclose(bias,(.4,-.2,.8))
        with self.assertRaises(ValueError):
            fit_gyro(np.tile((.4,-.2,.8),(100,1)),np.linspace(0,1,100))
        with self.assertRaises(ValueError):
            fit_gyro(np.arange(900).reshape(300,3),np.linspace(0,6,300))

    def test_accel_six_faces(self):
        points=np.repeat(np.concatenate((np.eye(3),-np.eye(3))),100,axis=0)
        measured=points*np.array((.9,1.1,1.05))+np.array((.1,.02,-.03))
        bias,matrix=fit_accel(measured)
        np.testing.assert_allclose(np.einsum("ij,kj->ik",measured-bias,matrix),points,atol=1e-8)
        with self.assertRaises(ValueError): fit_accel(measured[:100])

    def test_full_soft_iron_fit(self):
        rng=np.random.default_rng(10)
        directions=rng.normal(size=(2000,3))
        directions/=np.linalg.norm(directions,axis=1)[:,None]
        distortion=np.array(((200,30,10),(30,300,-20),(10,-20,250)))
        measured=np.einsum("ij,kj->ik",directions,distortion)+np.array((100,-200,50))
        bias,matrix=fit_mag(measured)
        np.testing.assert_allclose(bias,(100,-200,50),atol=1e-6)
        np.testing.assert_allclose(np.einsum("ij,kj->ik",measured-bias,matrix),directions,atol=1e-6)

    def test_flat_mag_coverage_rejected(self):
        angle=np.linspace(0,2*np.pi,200)
        points=np.column_stack((np.cos(angle),np.sin(angle),np.zeros(200)))
        with self.assertRaises(ValueError): fit_mag(points)

    def test_cli_preserves_mounting_and_never_marks_verified(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            config=VehicleConfig(verified=True,magnetic_verified=True)
            config.gyro.body_rotation=((0,1,0),(-1,0,0),(0,0,1))
            config.save(d/'in.json')
            with (d/'gyro.jsonl').open('w') as f:
                for n in range(301):
                    f.write(json.dumps(dict(type='raw',group='gyro',at=n*.02,values=(.1,.2,.3)))+'\n')
            self.assertEqual(main(['--config',str(d/'in.json'),'--output',str(d/'out.json'),
                                   '--gyro-log',str(d/'gyro.jsonl')]),0)
            calibrated=VehicleConfig.load(d/'out.json')
            self.assertFalse(calibrated.verified)
            self.assertFalse(calibrated.magnetic_verified)
            self.assertEqual(calibrated.gyro.body_rotation,[list(x) for x in config.gyro.body_rotation])
