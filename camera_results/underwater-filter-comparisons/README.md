# Underwater filter comparisons

Start with [Testudo overview](testudo-overview.png) and [hoop overview](hoop-overview.png).

Each target has three views and 24 digital transforms per view. Open PNGs at full size; native-size crops are in the target/view subdirectories. Parameters are printed on sheets and recorded in manifest.json.

## Complete comparison sheets

### Testudo

- 01-oblique, frame 960: [appearance](testudo-01-oblique-appearance.png), [channels and masks](testudo-01-oblique-channels-and-masks.png)
- 02-shaded, frame 1280: [appearance](testudo-02-shaded-appearance.png), [channels and masks](testudo-02-shaded-channels-and-masks.png)
- 03-close, frame 2080: [appearance](testudo-03-close-appearance.png), [channels and masks](testudo-03-close-channels-and-masks.png)

### Hoop

- 01-oblique, frame 960: [appearance](hoop-01-oblique-appearance.png), [channels and masks](hoop-01-oblique-channels-and-masks.png)
- 02-shadow-and-blue-background, frame 1280: [appearance](hoop-02-shadow-and-blue-background-appearance.png), [channels and masks](hoop-02-shadow-and-blue-background-channels-and-masks.png)
- 03-close-shaded-band, frame 1920: [appearance](hoop-03-close-shaded-band-appearance.png), [channels and masks](hoop-03-close-shaded-band-channels-and-masks.png)

## Reading the comparisons

- All views come from underwater_test1.mp4, decoded sequentially. Times are approximate frame-index/nominal-FPS values because the source has timestamp irregularities. Some hoop boundaries are clipped in the original frame; no missing image area was reconstructed.
- Every panel in a per-view sheet uses the same crop and display scale. Each individual PNG retains the crop's native pixel dimensions.
- RGB channels and channel averages use fixed 0–255 scaling. Differences use the fixed offset/gain printed on the sheet. There is no unreported per-panel histogram stretch.
- White balance is a gray-world estimate from the entire source frame, not measured camera white balance or a calibrated reference. Its gains are stored in the manifest.
- CLAHE uses clipLimit=2 and an 8x8 grid on the full frame before cropping. Comparisons include grayscale CLAHE, Lab lightness CLAHE, bilateral denoising plus CLAHE, mild sharpening and gamma brightening.
- Current HSV mask means only the pixel test S<=60, V>=160. It does not include the detector's perimeter/bounded-band/temporal checks. Relaxed HSV mask S<=90, V>=100 is an experimental comparison, not a recommended detector setting. White pixels mean the pixel passed, not that a hoop was detected.
- Inverse saturation is bright for neutral colors, including dark gray. It is not a white detector.
- Red emphasis is max(R-max(G,B),0)*3, clipped. Normalized red is 255*R/max(R+G+B,1). Red reflections and other red objects can also respond.
- Edge maps use the same fixed Sobel magnitude multiplier. Brighter edges do not mean better true target localization; background texture and noise can also increase.
- These are deterministic software transforms. They do not simulate a physical optical filter, polarizer, UV illumination or fluorescence. They cannot recover detail lost to substantial defocus.
- Compare the target against background/reflections in all three views. Detection accuracy and video stability still need separate tests.

Regenerate using generate_comparisons.py SOURCE.mp4 OUTPUT_DIRECTORY with OpenCV, NumPy and Pillow installed.
