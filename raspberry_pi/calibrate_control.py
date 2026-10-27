#!/usr/bin/env python3
"""Fit calibration from explicitly collected raw control JSONL recordings.

Use separate recordings: stationary for gyro, six stationary faces for accel,
and slow full 3-D rotations with motors stopped for magnetometer. This tool never
marks wiring or magnetic interference testing verified and never drives motors.
"""
import argparse
import json
from pathlib import Path
import numpy as np

if __package__:
    from .stabilization import Calibration, VehicleConfig
else:
    from stabilization import Calibration, VehicleConfig


def samples(path, group):
    rows = []
    times = []
    for line in Path(path).read_text().splitlines():
        item = json.loads(line)
        if item.get('type') == 'raw' and item.get('group') == group and not item.get('error'):
            values = item.get('values', [])
            if len(values) == 3 and np.isfinite(values).all() and np.isfinite(item['at']):
                rows.append(values)
                times.append(item['at'])
    if len(rows) < 100:
        raise ValueError(f'{group}: collect at least 100 valid samples')
    return np.array(rows, dtype=float), np.array(times, dtype=float)


def fit_gyro(values, times):
    if np.ptp(times) < 5 or np.max(np.std(values,axis=0)) > .25:
        raise ValueError('Gyro calibration needs at least five stationary seconds, std <=0.25 deg/s')
    if np.max(np.abs(np.mean(values, axis=0))) > 10:
        raise ValueError('Gyro mean exceeds 10 deg/s; verify stationary recording and sensor')
    return np.mean(values,axis=0), np.eye(3)


def fit_accel(values):
    low, high = np.percentile(values, (1,99), axis=0)
    span = (high-low)/2
    if np.any(span < .7) or np.any(span > 1.3):
        raise ValueError('Accelerometer recording must cover all six +/-1g faces')
    bias = (high+low)/2
    matrix = np.diag(1/span)
    corrected = np.einsum("ij,kj->ik", values-bias, matrix)
    if np.mean(np.abs(np.linalg.norm(corrected,axis=1)-1) < .15) < .9:
        raise ValueError('Accelerometer residual too high; collect stationary face samples')
    return bias, matrix


def fit_mag(values):
    # Condition the least-squares fit before solving the full rotated ellipsoid.
    origin = np.mean(values,axis=0)
    scale = np.sqrt(np.mean((values-origin)**2))
    if scale <= 0:
        raise ValueError('Magnetometer has no variation')
    v = (values-origin)/scale
    x,y,z = v.T
    design = np.column_stack((x*x,y*y,z*z,2*x*y,2*x*z,2*y*z,x,y,z))
    coefficients, _, rank, _ = np.linalg.lstsq(design,np.ones(len(v)),rcond=None)
    if rank < 9:
        raise ValueError('Magnetometer needs full 3-D rotation coverage')
    a,b,c,d,e,f,g,h,i = coefficients
    quadratic = np.array(((a,d,e),(d,b,f),(e,f,c)))
    center = -.5*np.linalg.solve(quadratic,np.array((g,h,i)))
    shape = quadratic/(1+center @ quadratic @ center)
    eigenvalues,eigenvectors = np.linalg.eigh(shape)
    if np.min(eigenvalues) <= 0:
        raise ValueError('Magnetometer ellipsoid is not positive definite')
    matrix = (eigenvectors @ np.diag(np.sqrt(eigenvalues)) @ eigenvectors.T)/scale
    bias = origin+center*scale
    corrected = np.einsum("ij,kj->ik", values-bias, matrix)
    norms = np.linalg.norm(corrected,axis=1)
    coverage = np.linalg.eigvalsh(np.cov((corrected/norms[:,None]).T))
    if np.min(coverage) < .1 or np.percentile(np.abs(norms-1),95) > .1:
        raise ValueError('Magnetometer fit fails coverage or 10% residual test')
    return bias, matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--gyro-log',type=Path)
    parser.add_argument('--accel-log',type=Path)
    parser.add_argument('--mag-log',type=Path)
    args=parser.parse_args(argv)
    if not any((args.gyro_log,args.accel_log,args.mag_log)):
        parser.error('Provide at least one calibration recording')
    if args.output.exists():
        parser.error('Output already exists; choose a new file to preserve earlier calibration')
    try:
        config=VehicleConfig.load(args.config)
        for group,path in (('gyro',args.gyro_log),('accel',args.accel_log),('mag',args.mag_log)):
            if path is None:
                continue
            values,times=samples(path,group)
            if group == 'gyro': bias,matrix=fit_gyro(values,times)
            elif group == 'accel': bias,matrix=fit_accel(values)
            else: bias,matrix=fit_mag(values)
            setattr(config,group,Calibration(tuple(float(x) for x in bias),
                    tuple(tuple(float(x) for x in row) for row in matrix),
                    getattr(config,group).body_rotation))
        config.verified=False
        config.magnetic_verified=False
        config.save(args.output)
        print(f'Saved {args.output}. Verify axes, wiring and submerged motor interference before enabling.')
        return 0
    except (ValueError, OSError, np.linalg.LinAlgError) as error:
        parser.exit(1,f'Calibration failed: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
