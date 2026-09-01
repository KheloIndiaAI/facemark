"""Calibrate video liveness against clips recorded on the devices in use.

    python -m scripts.calibrate_liveness --live DIR --spoof DIR

WHAT IS ALREADY MEASURED, AND WHAT IS NOT
-----------------------------------------
The signal is parallax: a photograph is a plane, so under camera motion every
point on it maps through one homography, while a real face leaves a residual
because the nose is nearer the lens than the ears.

Measured on this project's own data:

    flat photographs, warped through known homographies   0.00028 - 0.00085
    flat photograph filmed through a browser (VP8)        0.0046
    real faces, multi-view enrolment frames               0.17205 - 0.31982

LIVENESS_MIN_DEPTH sits at 0.010, between those. But note the middle row: video
compression roughly quintupled the flat score compared with clean warps, so the
margin against a COMPRESSED spoof is about 2x, not the 12x the synthetic clips
suggested. Different phones and bitrates will move that number, which is the
main reason to run this.

The real-face figures also come from deliberate head turns, a larger viewpoint
change than a casual two-second clip gives, so they are an optimistic bound.
Recording real clips the way coaches actually will is the point of --live.

HOW TO COLLECT
--------------
  live/   two-second clips of people standing in front of the camera, recorded
          on the phones that take attendance, in the rooms it is taken in.
          Include still subjects and poor light - those are the cases that
          produce a false rejection, and a false rejection denies a real
          athlete their attendance.
  spoof/  the attack: someone holding up a phone or laptop showing an enrolled
          athlete's photo, filmed by the attendance device. Vary distance,
          angle, screen brightness and how much the hand moves.

Twenty of each is enough to see whether the classes separate.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, liveness  # noqa: E402
from backend.detector import get_detector  # noqa: E402

VIDEO_GLOBS = ("*.webm", "*.mp4", "*.mov", "*.m4v", "*.avi",
               "*.WEBM", "*.MP4", "*.MOV")


def measure(folder: Path, label: str):
    files: list = []
    for g in VIDEO_GLOBS:
        files += sorted(folder.glob(g))
    if not files:
        print(f"  {label}: no video files in {folder}")
        return np.array([]), []

    det = get_detector()
    scores, rows = [], []
    for p in files:
        try:
            res = liveness.analyse(p.read_bytes(), det)
        except Exception as e:  # noqa: BLE001
            print(f"    ! {p.name}: {e}")
            continue
        rows.append((p.name, res))
        if res.verdict in ("live", "screen"):
            scores.append(res.depth_score)
        print(f"    {p.name[:38]:<40} {res.verdict:<13} "
              f"depth={res.depth_score:8.5f} motion={res.motion:7.4f} "
              f"pts={res.tracked_points:4d}")
    return np.array(scores), rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=Path, required=True,
                    help="clips of real people in front of the camera")
    ap.add_argument("--spoof", type=Path, required=True,
                    help="clips of a screen or print showing a face")
    args = ap.parse_args()

    for d in (args.live, args.spoof):
        if not d.is_dir():
            print(f"Not a directory: {d}")
            return 1

    print("LIVE clips (must NOT be rejected)")
    live, live_rows = measure(args.live, "live")
    print()
    print("SPOOF clips (SHOULD be rejected)")
    spoof, spoof_rows = measure(args.spoof, "spoof")
    print()

    if live.size == 0 or spoof.size == 0:
        print("Both folders need at least one clip that produced a verdict.")
        return 1

    print(f"live  depth : min={live.min():.5f}  median={np.median(live):.5f}  max={live.max():.5f}")
    print(f"spoof depth : min={spoof.min():.5f}  median={np.median(spoof):.5f}  max={spoof.max():.5f}")
    print()

    gap = float(live.min()) - float(spoof.max())
    if gap > 0:
        suggested = (live.min() + spoof.max()) / 2
        print(f"SEPARATED by {gap:.5f}")
        print(f"  suggested LIVENESS_MIN_DEPTH = {suggested:.4f}")
    else:
        print("OVERLAP - the lowest real face scores below the highest spoof.")
        print("  No threshold separates these clips. Before moving the number,")
        print("  check the overlapping live clips for near-zero motion: parallax")
        print("  needs a viewpoint change, and a perfectly still recording carries")
        print("  no depth information for any threshold to read.")
    print()

    # Report the pair as it will actually run, including the motion guard.
    fr = sum(1 for _, r in live_rows if r.verdict in ("screen",))
    incon = sum(1 for _, r in live_rows if r.verdict == "inconclusive")
    caught = sum(1 for _, r in spoof_rows if r.verdict == "screen")
    missed = sum(1 for _, r in spoof_rows if r.verdict == "live")
    print(f"With LIVENESS_MIN_DEPTH={config.LIVENESS_MIN_DEPTH}, "
          f"LIVENESS_MIN_MOTION={config.LIVENESS_MIN_MOTION}:")
    print(f"  real people wrongly refused    : {fr}/{len(live_rows)}")
    print(f"  real people called inconclusive: {incon}/{len(live_rows)}")
    print(f"  spoofs caught                  : {caught}/{len(spoof_rows)}")
    print(f"  spoofs that got through        : {missed}/{len(spoof_rows)}")
    if fr:
        print()
        print("  A wrongly refused athlete is the worse failure of the two.")
        print("  Fix that column before improving the catch rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
