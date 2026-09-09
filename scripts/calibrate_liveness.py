"""Calibrate video liveness against clips recorded on the devices in use.

    python -m scripts.calibrate_liveness --live DIR --spoof DIR

WHAT IS ALREADY MEASURED, AND WHAT IS NOT
-----------------------------------------
The signal is parallax: a photograph is a plane, so under camera motion every
point on it maps through one homography, while a real face leaves a residual
because the nose is nearer the lens than the ears.

Measured on a MATCHED corpus - 150 real clips (real people, real camera, frames
~200ms apart) against 150 photograph clips built to the same frame count and the
same measured motion, all through real VP8:

    photographs   0.00060 - 0.00197
    real faces    0.00313 - 0.256     (median 0.0111)

LIVENESS_MIN_DEPTH sits at 0.0025, inside that gap: 0 photographs accepted, 0
real people refused, over 270 judged clips.

MATCH THE MOTION, OR MEASURE NOTHING. Earlier versions of this file quoted
figures from clips whose motion ranges barely overlapped - photographs that
happened to move gently against faces that happened to move a lot - and the
apparent separation was mostly that mismatch. Compared fairly, the old
algorithm had NO separation at any motion: a photograph waved hard scored above
a real face, because matching frame 0 against frame 17 directly asks more of
Lucas-Kanade than it can do, and the residual then measures the tracker failing
rather than the subject's shape. If you collect your own clips, sample both
classes across the same range of movement, or you will re-learn this the hard
way.

Two regimes remain unmeasured, and both matter for a pilot:

  distance   every number above comes from faces 165-313px wide. Real group
             photographs from this centre have faces of 20-74px, where parallax
             is below the tracker's noise. Those clips are reported as
             unmeasurable rather than judged - see LIVENESS_MIN_FACE_PX - so a
             group capture is currently NOT liveness-checked. Clips of a real
             squad, and of a phone held up showing one, are the data that would
             let that change.
  real spoof a photograph warped in software is a faithful plane, but a real
             screen filmed by a real phone adds moire and its own pixel grid.
             --spoof is how you check that those do not move the flat side.

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
