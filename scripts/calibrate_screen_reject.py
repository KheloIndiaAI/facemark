"""Calibrate the screen-replay rejection against photographs you take yourself.

    python -m scripts.calibrate_screen_reject --real DIR --screen DIR

WHY THIS EXISTS
---------------
config.SCREEN_MAX_MOIRE_PEAK is calibrated on one side only. It was measured
over 267 real faces from this project's own photographs, so it provably rejects
none of them - but nothing in the stored corpus is a photograph of a screen, so
the other side, whether a held-up phone actually trips it, has never been
measured. A threshold calibrated on negatives alone can be perfectly safe and
still catch nothing.

HOW TO COLLECT THE TWO SETS
---------------------------
  real/    ordinary attendance photos of people standing in the room, taken on
           the same devices and in the same lighting as real sessions.
  screen/  the spoof you are defending against: someone holding up a phone or
           laptop showing an enrolled athlete's photograph, photographed by the
           attendance device. Vary the distance, the angle and the screen
           brightness - moire depends on all three, and a single easy example
           will flatter the result.

Twenty of each is enough to see whether the two populations separate. If they
overlap, this test cannot be made reliable by moving the threshold, and the
honest answer is that it needs a different signal rather than a bolder number.

Nothing here writes to config.py; it prints what the values should be.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, detector  # noqa: E402

IMAGE_GLOBS = ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG")


def measure(folder: Path, label: str) -> tuple:
    """Return (moire, bezel) arrays for every measurable face in `folder`."""
    files: list = []
    for g in IMAGE_GLOBS:
        files += sorted(folder.glob(g))
    if not files:
        print(f"  {label}: no images found in {folder}")
        return np.array([]), np.array([])

    det = detector.get_detector()
    moires, bezels, skipped = [], [], 0
    for p in files:
        img = cv2.imread(str(p))
        if img is None:
            continue
        try:
            faces = det.detect(img, mode="fused")
        except Exception as e:  # noqa: BLE001
            print(f"    ! {p.name}: {e}")
            continue
        for f in faces:
            x1, y1, x2, y2 = [int(v) for v in f.box]
            if min(x2 - x1, y2 - y1) < config.SCREEN_MIN_FACE_PX:
                skipped += 1
                continue
            crop = img[max(0, y1):y2, max(0, x1):x2]
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            moires.append(detector.moire_peakiness(gray))
            bezels.append(detector.bezel_ratio(img, f.box))

    print(f"  {label}: {len(files)} image(s), {len(moires)} face(s) measured, "
          f"{skipped} skipped under {config.SCREEN_MIN_FACE_PX}px")
    return np.array(moires), np.array(bezels)


def describe(name: str, a: np.ndarray) -> None:
    if a.size == 0:
        print(f"    {name:<12} (none)")
        return
    print(f"    {name:<12} min={a.min():7.2f}  median={np.median(a):7.2f}  "
          f"p95={np.percentile(a, 95):7.2f}  max={a.max():7.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", type=Path, required=True,
                    help="folder of ordinary photos of people in the room")
    ap.add_argument("--screen", type=Path, required=True,
                    help="folder of photos OF A SCREEN showing a face")
    args = ap.parse_args()

    for d in (args.real, args.screen):
        if not d.is_dir():
            print(f"Not a directory: {d}")
            return 1

    # Measure the whole population, including faces the filter would remove.
    config.REJECT_SCREEN_FACES = False

    print("Measuring")
    rm, rb = measure(args.real, "real  ")
    sm, sb = measure(args.screen, "screen")
    print()

    if rm.size == 0 or sm.size == 0:
        print("Both folders need at least one measurable face.")
        return 1

    print("Real faces (must NOT be rejected):")
    describe("moire", rm)
    describe("bezel", rb)
    print()
    print("Screen faces (SHOULD be rejected):")
    describe("moire", sm)
    describe("bezel", sb)
    print()

    # A threshold only works if the highest real value sits below the lowest
    # screen value. Anything else means some real face scores like a spoof.
    print("Separation")
    gap_m = float(sm.min()) - float(rm.max())
    gap_b = float(sb.min()) - float(rb.max())
    print(f"  moire: highest real {rm.max():.2f}  vs  lowest screen {sm.min():.2f}"
          f"   -> {'separated by %.2f' % gap_m if gap_m > 0 else 'OVERLAP'}")
    print(f"  bezel: highest real {rb.max():.2f}  vs  lowest screen {sb.min():.2f}"
          f"   -> {'separated by %.2f' % gap_b if gap_b > 0 else 'OVERLAP'}")
    print()

    if gap_m > 0:
        # Midpoint: as much headroom against a bright real face as against a
        # weak spoof, rather than hugging whichever set happened to be smaller.
        print(f"  SCREEN_MAX_MOIRE_PEAK = {(rm.max() + sm.min()) / 2:.1f}")
    else:
        print("  moire does NOT separate these sets. Lowering the threshold to")
        print("  catch these spoofs would also reject real faces. Collect more")
        print("  examples before concluding, then consider a different signal")
        print("  (screen-edge geometry, or a trained liveness model) rather than")
        print("  a threshold that cannot work.")
    if gap_b > 0:
        print(f"  SCREEN_MIN_BEZEL_RATIO = {(rb.max() + sb.min()) / 2:.2f}")
    else:
        print("  bezel does NOT separate these sets.")
    print()

    # Both conditions are required to reject, so report the pair as it will run.
    caught = int(np.sum((sm > config.SCREEN_MAX_MOIRE_PEAK)
                        & (sb > config.SCREEN_MIN_BEZEL_RATIO)))
    lost = int(np.sum((rm > config.SCREEN_MAX_MOIRE_PEAK)
                      & (rb > config.SCREEN_MIN_BEZEL_RATIO)))
    print(f"With the CURRENT settings "
          f"(moire>{config.SCREEN_MAX_MOIRE_PEAK}, bezel>{config.SCREEN_MIN_BEZEL_RATIO}):")
    print(f"  spoof faces caught : {caught}/{sm.size}")
    print(f"  real faces WRONGLY rejected : {lost}/{rm.size}")
    if lost:
        print("  ^ any number above zero here means genuine athletes are being")
        print("    refused attendance. Fix that before improving the catch rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
