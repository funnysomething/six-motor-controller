"""Deterministic small-angle plant checks, not a hydrodynamic certification."""
import unittest
from dataclasses import replace
from raspberry_pi.stabilization import VehicleConfig, Attitude, RangeSample, Stabilizer


class SimulationTests(unittest.TestCase):
    def run_plant(self, compass=True):
        c=VehicleConfig(verified=True,pwm_limit=100,rate_kp=.05)
        initial=Attitude(10,0,0,0,(0,0,0),True,compass,0 if compass else None,10 if compass else None)
        control=Stabilizer(c)
        sample=RangeSample(10,0,1,1.)
        control.enable(initial,sample,10,True)
        angles=[0.,0.,0.]; rates=[0.,0.,0.]; height=1.; velocity=0.
        states=[]
        for step in range(1,601):
            dt=.05; now=10+step*dt
            if step in (20,300):
                direction=1 if step == 20 else -1
                angles=[direction*10.]*3
                height=1+direction*.1
            attitude=replace(initial,at=now,roll=angles[0],pitch=angles[1],heading=angles[2],rates=tuple(rates))
            if step%6 == 0:
                sample=RangeSample(now,step//6%64,1,height)
            pwm=control.update(attitude,sample,now)
            u=[x/100 for x in pwm]
            upward=(u[2]+u[3]+u[4])/1.9
            roll=u[3]-u[2]; pitch=upward-u[4]; yaw=(u[1]-u[0])/2
            for i,moment in enumerate((roll,pitch,yaw)):
                rates[i]+=(50*moment-1.5*rates[i])*dt
                angles[i]+=rates[i]*dt
            velocity+=(.4*upward-velocity)*dt
            height+=velocity*dt
            states.append((angles[:],height))
            self.assertEqual(pwm[5],0)
            self.assertTrue(control.active)
        return control,states

    def test_recovers_both_sign_disturbances(self):
        control,states=self.run_plant()
        for index in (219,599):
            angles,height=states[index]
            self.assertLess(abs(angles[0]),5)
            self.assertLess(abs(angles[1]),5)
            self.assertLess(abs(angles[2]),10)
            self.assertLess(abs(height-1),.05)
        self.assertEqual(control.yaw_mode,'heading')

    def test_without_compass_leveling_and_clearance_recover_but_heading_not_claimed(self):
        control,states=self.run_plant(False)
        angles,height=states[-1]
        self.assertLess(abs(angles[0]),5)
        self.assertLess(abs(angles[1]),5)
        self.assertLess(abs(height-1),.05)
        self.assertGreater(abs(angles[2]),5)
        self.assertEqual(control.yaw_mode,'damping')
