"""Deterministic digital-filter comparisons; no learned image reconstruction."""
import argparse
import hashlib
import json
from pathlib import Path
import textwrap
import zipfile

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


SPECS = [
    ('testudo', '01-oblique', 960, (300, 100, 820, 540)),
    ('testudo', '02-shaded', 1280, (200, 240, 840, 720)),
    ('testudo', '03-close', 2080, (360, 0, 1100, 560)),
    ('hoop', '01-oblique', 960, (650, 80, 1280, 720)),
    ('hoop', '02-shadow-and-blue-background', 1280, (690, 270, 1280, 720)),
    ('hoop', '03-close-shaded-band', 1920, (300, 0, 1180, 720)),
]


def font(size):
    for name in ['/System/Library/Fonts/Supplemental/Arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def u8(image):
    return np.clip(image, 0, 255).astype(np.uint8)


def clahe(gray):
    return cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8)).apply(gray)


def lab_contrast(image):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lab[:, :, 0] = clahe(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def gradient(gray):
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    x = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    y = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    return u8(np.hypot(x, y) * 4)


def transforms(image, target):
    b, g, r = [a.astype(np.float32) for a in cv2.split(image)]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    means = image.mean(axis=(0, 1))
    gains = np.clip(means.mean() / np.maximum(means, 1), .5, 2)
    balanced = u8(image.astype(np.float32) * gains)
    unsharp = cv2.addWeighted(image, 1.5, cv2.GaussianBlur(image, (0, 0), 1.2), -.5, 0)
    gamma = np.array([round(255 * (i / 255) ** (1 / 1.4)) for i in range(256)], np.uint8)
    appearance = [
        ('01-original', 'Original color', 'Unmodified decoded pixels', image),
        ('02-grayscale', 'Grayscale', 'OpenCV BGR-to-gray luminance', gray),
        ('03-red', 'Red channel', 'R shown as grayscale; fixed 0-255 scale', u8(r)),
        ('04-green', 'Green channel', 'G shown as grayscale; fixed 0-255 scale', u8(g)),
        ('05-blue', 'Blue channel', 'B shown as grayscale; fixed 0-255 scale', u8(b)),
        ('06-white-balance', 'Gray-world white balance', 'Full-frame channel means; gains clipped 0.5-2', balanced),
        ('07-gray-clahe', 'Grayscale + CLAHE', 'Local contrast; clip limit 2, grid 8x8', clahe(gray)),
        ('08-lab-clahe', 'Color + CLAHE on Lab L', 'Local brightness contrast; original a/b retained', lab_contrast(image)),
        ('09-wb-clahe', 'White balance + Lab CLAHE', 'Same white balance and CLAHE parameters', lab_contrast(balanced)),
        ('10-unsharp', 'Mild unsharp mask', '1.5 original - 0.5 Gaussian; sigma 1.2', unsharp),
        ('11-gamma', 'Gamma brighten', 'Output = 255 * (input/255)^(1/1.4)', cv2.LUT(image, gamma)),
        ('12-bilateral-clahe', 'Bilateral denoise + Lab CLAHE', 'Bilateral d=5, sigmaColor=25, sigmaSpace=3', lab_contrast(cv2.bilateralFilter(image, 5, 25, 3))),
    ]
    if target == 'testudo':
        material = [
            ('09-red-opponent', 'Red emphasis', 'max(R - max(G,B), 0) * 3; clipped to 255', u8(np.maximum(r - np.maximum(g, b), 0) * 3)),
            ('10-normalized-red', 'Normalized red', '255 * R / max(R+G+B,1); fixed scale', u8(255 * r / np.maximum(r+g+b, 1))),
        ]
    else:
        material = [
            ('09-white-current', 'Current white HSV mask', 'Raw color: S <= 60 and V >= 160; white=pass', u8(((hsv[:, :, 1] <= 60) & (hsv[:, :, 2] >= 160)) * 255)),
            ('10-white-relaxed', 'Relaxed white HSV mask', 'EXPERIMENT: S <= 90 and V >= 100; white=pass', u8(((hsv[:, :, 1] <= 90) & (hsv[:, :, 2] >= 100)) * 255)),
        ]
    diagnostic = [
        ('01-hsv-value', 'HSV value', 'V = max(R,G,B); fixed 0-255 scale', hsv[:, :, 2]),
        ('02-hsv-saturation', 'HSV saturation', 'White means highly saturated color', hsv[:, :, 1]),
        ('03-inverse-saturation', 'Inverse saturation', '255-S; white means neutral, including gray/dark', 255-hsv[:, :, 1]),
        ('04-lab-lightness', 'Lab lightness', 'OpenCV encoded L; fixed 0-255 scale', cv2.cvtColor(image, cv2.COLOR_BGR2LAB)[:, :, 0]),
        ('05-red-green-average', 'Red + green average', '(R+G)/2; fixed 0-255 scale', u8((r+g)/2)),
        ('06-green-blue-average', 'Green + blue average', '(G+B)/2; fixed 0-255 scale', u8((g+b)/2)),
        ('07-red-minus-green', 'Red minus green', '128 + 2*(R-G); neutral=128; clipped', u8(128+2*(r-g))),
        ('08-red-minus-blue', 'Red minus blue', '128 + 2*(R-B); neutral=128; clipped', u8(128+2*(r-b))),
        *material,
        ('11-edges-original', 'Edges from original grayscale', 'Sobel magnitude x4 after Gaussian 3x3', gradient(gray)),
        ('12-edges-clahe', 'Edges after grayscale CLAHE', 'Same Sobel scale; contrast can amplify false edges', gradient(clahe(gray))),
    ]
    return appearance, diagnostic, gains.tolist()


def pil(image):
    if image.ndim == 2:
        return Image.fromarray(image).convert('RGB')
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def tile(image, title, description, size=(480, 360)):
    w, h = size
    panel = Image.new('RGB', (w, h+80), '#eeeeee')
    fitted = pil(image)
    fitted.thumbnail((w, h), Image.Resampling.LANCZOS)
    panel.paste(fitted, ((w-fitted.width)//2, 80+(h-fitted.height)//2))
    d = ImageDraw.Draw(panel)
    d.text((10, 7), title, font=font(19), fill='#111111')
    for i, line in enumerate(textwrap.wrap(description, width=55)):
        d.text((10, 33+i*18), line, font=font(14), fill='#333333')
    return panel


def sheet(variants, title, subtitle, destination):
    result = Image.new('RGB', (1920, 80+3*440+36), 'white')
    d = ImageDraw.Draw(result)
    d.text((16, 9), title, font=font(25), fill='black')
    d.text((16, 45), subtitle, font=font(17), fill='black')
    for i, (_, name, desc, image) in enumerate(variants):
        result.paste(tile(image, name, desc), (i%4*480, 80+i//4*440))
    d.text((16, result.height-26), 'Digital transforms only. Identical crop and scale in every panel. Black/white masks are not detections.', font=font(17), fill='black')
    result.save(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=True, parents=True)
    wanted = {spec[2] for spec in SPECS}
    capture = cv2.VideoCapture(str(args.source))
    if not capture.isOpened():
        raise RuntimeError('Cannot decode source')
    fps = capture.get(cv2.CAP_PROP_FPS)
    frames = {}
    index = 0
    # Sequential decoding avoids inaccurate seeks in this source's timestamps.
    while wanted:
        ok, image = capture.read()
        if not ok:
            break
        if index in wanted:
            frames[index] = image
            wanted.remove(index)
        index += 1
    capture.release()
    if wanted:
        raise RuntimeError(f'Missing frames {wanted}')
    manifest = dict(source=str(args.source), source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
                    nominal_fps=fps, time_note='Labels use decoded frame index / nominal FPS; approximate source time.', views=[])
    overviews = {'testudo': [], 'hoop': []}
    for target, name, frame, box in SPECS:
        directory = args.output / target / name
        directory.mkdir(parents=True, exist_ok=True)
        appearance, diagnostic, gains = transforms(frames[frame], target)
        x0, y0, x1, y1 = box
        groups = []
        for group_name, variants in [('appearance', appearance), ('channels-and-masks', diagnostic)]:
            cropped = [(key, label, desc, array[y0:y1, x0:x1]) for key, label, desc, array in variants]
            for key, label, desc, array in cropped:
                if not cv2.imwrite(str(directory / f'{group_name}-{key}.png'), array):
                    raise RuntimeError('PNG write failed')
            sheet(cropped, f'{target.upper()} | {name} | {group_name}',
                  f'Frame {frame} | approximately {frame/fps:.1f}s | crop {box} | parameters applied to full frame before crop',
                  args.output / f'{target}-{name}-{group_name}.png')
            groups.append(cropped)
        a, d = groups
        chosen = [a[0], a[8], a[6], d[8], d[9], d[11]]
        overviews[target].append((f'{name} | ~{frame/fps:.1f}s', chosen))
        cv2.imwrite(str(directory / 'source-full-frame.png'), frames[frame])
        manifest['views'].append(dict(target=target, view=name, frame_index=frame, approximate_time_s=frame/fps,
            crop_xyxy=box, gray_world_bgr_gains=gains,
            transforms={group: [dict(key=k, label=l, formula=desc) for k, l, desc, _ in variants]
                        for group, variants in [('appearance', appearance), ('channels-and-masks', diagnostic)]}))
    for target, rows in overviews.items():
        result = Image.new('RGB', (6*320, 80+len(rows)*330+36), 'white')
        draw = ImageDraw.Draw(result)
        draw.text((16, 10), f'{target.upper()}: six useful comparisons across three views', font=font(25), fill='black')
        draw.text((16, 46), 'Open the larger per-view sheets for all 24 transforms and native-size PNGs for pixel inspection.', font=font(17), fill='black')
        for i, (name, variants) in enumerate(rows):
            draw.text((12, 80+i*330), name, font=font(17), fill='black')
            for j, (_, title, _, image) in enumerate(variants):
                panel = tile(image, title, '', (320, 220))
                result.paste(panel, (j*320, 110+i*330))
        draw.text((16, result.height-25), 'No automatic contrast stretching of channel images. Enhanced appearance does not establish detection accuracy.', font=font(16), fill='black')
        result.save(args.output / f'{target}-overview.png')
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'Created 2 overview sheets, 12 full comparison sheets and 144 native-size transformed crops in {args.output}')


if __name__ == '__main__':
    main()
