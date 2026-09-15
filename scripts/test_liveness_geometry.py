"""The parallax check must measure shape, not tracking failure.

    python -m scripts.test_liveness_geometry

This is the regression guard for a bug that made the liveness check worse than
useless: it matched frame 0 against every later frame directly, which is more
displacement than Lucas-Kanade can follow, so on a clip with real movement the
points scattered and the leftover residual read as depth. A photograph waved
hard scored 1.15 - far above a real face - and no threshold separated the two
classes at all.

It needs no photographs of anybody. The property under test is geometric: a
plane maps through one homography however it moves, and a scene with depth does
not. Textures are generated, `box` is passed straight to _depth_from_parallax,
and the face detector never runs - which is what lets this live in CI, where the
athletes' photographs deliberately do not.

Three things are asserted:

  a plane stays flat at SMALL movement   the easy case, which always passed
  a plane stays flat at LARGE movement   the case that used to fail, and the
                                         only reason this file exists
  depth is still detected                a guard against "fixing" the first two
                                         by making the test blind to everything
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config  # noqa: E402
from backend.liveness import _depth_from_parallax  # noqa: E402

SIZE = 480
BOX = (60, 60, 420, 420)          # the region the residual is measured over
ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label} {extra}")
    else:
        fail += 1
        print(f"  [FAIL] {label} {extra}")


def texture(seed, size=SIZE):
    """Trackable texture. Blurred noise gives corners without aliasing, which
    would otherwise let the tracker lock onto sampling artefacts rather than
    the pattern."""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (size, size), dtype=np.uint8)
    img = cv2.GaussianBlur(img, (5, 5), 0)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def plane_clip(travel_px, n=12, seed=7):
    """One flat surface, translated and rotated - a photograph in a hand."""
    img = texture(seed)
    out = []
    for i in range(n):
        t = i / (n - 1)
        M = cv2.getRotationMatrix2D((SIZE / 2, SIZE / 2), 6.0 * t, 1.0 + 0.04 * t)
        M[0, 2] += travel_px * t
        M[1, 2] += travel_px * 0.4 * t
        out.append(cv2.warpAffine(img, M, (SIZE, SIZE), borderMode=cv2.BORDER_REPLICATE))
    return out


def depth_clip(travel_px, n=12, relief=0.6, spread=0.18):
    """A surface with a bump in it, viewed from a moving camera.

    Parallax is the whole of it: image displacement is inversely proportional to
    distance, so a raised centre - a nose - sweeps further across the sensor
    than the flat surround for the same camera travel, and no single homography
    can fit both. `relief` is how far the bump stands proud as a fraction of the
    viewing distance and `spread` is how broad it is.

    THIS IS A GUARD, NOT A MODEL OF A FACE. The values are chosen so the
    fixture lands unambiguously above the threshold, because its job is to fail
    if someone "fixes" a false accusation by making the test blind. Measured
    while choosing them: a NARROW bump never clears the threshold at any relief,
    because the residual is a median over tracked points and a small protrusion
    on an otherwise flat surface leaves the typical point exactly where the
    homography predicts. Real faces clear it comfortably - median 0.0111 against
    a 0.0025 threshold - because the whole surface is curved and heads turn. A
    nose alone would not be enough, which is worth knowing before anyone reaches
    for this test to reason about masks.

    Built as a smooth per-pixel displacement rather than a pasted patch. A
    pasted patch has a hard edge that stays put while its contents slide, which
    destroys tracked points along the seam and leaves one plane's worth of
    survivors - a single plane fits those perfectly, so the fixture would report
    NO depth while looking like it tested for some.
    """
    img = texture(11)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    r = np.hypot(xx - SIZE / 2, yy - SIZE / 2) / (SIZE / 2)
    # 1.0 far, 1-relief at the centre: a smooth rise toward the camera
    distance = 1.0 - relief * np.exp(-(r ** 2) / spread)
    out = []
    for i in range(n):
        t = i / (n - 1)
        shift = travel_px * t / distance          # nearer => moves further
        map_x = xx - shift
        map_y = yy - shift * 0.4
        out.append(cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE))
    return out


print(f"threshold in force: LIVENESS_MIN_DEPTH = {config.LIVENESS_MIN_DEPTH}, "
      f"round-trip tolerance {config.LIVENESS_FB_MAX_PX}px\n")

print("=== a flat surface reads as flat, however far it travels ===")
for travel in (8, 30, 80, 160):
    d, m, pts, measured = _depth_from_parallax(plane_clip(travel), BOX)
    verdict = "flat" if d < config.LIVENESS_MIN_DEPTH else "READ AS LIVE"
    check(f"travel {travel:>3}px -> {verdict}",
          (not measured) or d < config.LIVENESS_MIN_DEPTH,
          f"(depth {d:.5f} motion {m:.4f} pts {pts})")

print("\n=== depth is still detected, so the fix did not just blind the test ===")
found = 0
for travel in (30, 80, 160):
    d, m, pts, measured = _depth_from_parallax(depth_clip(travel), BOX)
    hit = measured and d >= config.LIVENESS_MIN_DEPTH
    found += bool(hit)
    print(f"    travel {travel:>3}px depth {d:.5f} motion {m:.4f} "
          f"pts {pts} measured={measured} -> {'depth seen' if hit else 'no'}")
check("a scene with depth is recognised as having depth", found == 3,
      f"({found}/3 travels)")

print("\n=== an unmeasurable clip says so rather than guessing ===")
still = [texture(7)] * 6                      # nothing moves at all
d, m, pts, measured = _depth_from_parallax(still, BOX)
check("a motionless clip reports no motion, not a low depth",
      m < config.LIVENESS_MIN_MOTION, f"(motion {m:.5f} depth {d:.5f})")
blank = [np.full((SIZE, SIZE, 3), 128, np.uint8) for _ in range(6)]
d, m, pts, measured = _depth_from_parallax(blank, BOX)
check("a textureless clip finds too few points to judge",
      pts < config.LIVENESS_MIN_POINTS and not measured, f"(points {pts})")

print(f"\n{ok}/{ok + fail} checks passed")
sys.exit(1 if fail else 0)
