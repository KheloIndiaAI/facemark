"""Robustness envelope: how far can conditions degrade before recognition fails?

Field photos are not studio photos. This applies realistic degradations to a
labelled group photo and measures where accuracy falls off, which is the
practical substitute for collecting more data: it tells a coach what the system
tolerates rather than asserting that it "works".

Axes, each chosen because it happens in real use:

  brightness   evening sessions, backlight, overexposed midday sun
  blur         athletes moving, unsteady hands, cheap phone optics
  scale        camera further back, so faces occupy fewer pixels
  jpeg         aggressive phone or WhatsApp recompression
  rotation     phone not held level

    python -m scripts.robustness
    python -m scripts.robustness --photo path.jpg --present 56,57,58
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database  # noqa: E402
from backend.detector import get_detector  # noqa: E402
from backend.metaheuristics import GlobalMatchOptimizer  # noqa: E402
from backend.recognizer import fuse_scores, get_recognizer  # noqa: E402
from scripts.evaluate import load_gallery  # noqa: E402


# --- degradations -------------------------------------------------------------

def adjust_brightness(img, factor):
    return np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def motion_blur(img, k):
    if k <= 1:
        return img
    kern = np.zeros((k, k), np.float32)
    kern[k // 2, :] = 1.0 / k          # horizontal motion
    return cv2.filter2D(img, -1, kern)


def rescale(img, factor):
    if factor == 1.0:
        return img
    h, w = img.shape[:2]
    small = cv2.resize(img, (max(1, int(w * factor)), max(1, int(h * factor))),
                       interpolation=cv2.INTER_AREA)
    # Back to original size: the face is now genuinely lower-resolution, which is
    # what standing further from the camera actually does.
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def jpeg(img, quality):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else img


def rotate(img, degrees):
    if degrees == 0:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


AXES = {
    "brightness": ("x", [0.35, 0.5, 0.7, 1.0, 1.4, 1.8, 2.2],
                   lambda im, v: adjust_brightness(im, v)),
    "motion blur": ("px", [1, 3, 5, 7, 9, 13, 17],
                    lambda im, v: motion_blur(im, int(v))),
    "downscale": ("x", [1.0, 0.75, 0.6, 0.5, 0.4, 0.3, 0.25],
                  lambda im, v: rescale(im, v)),
    "jpeg quality": ("q", [95, 80, 60, 40, 25, 15, 8],
                     lambda im, v: jpeg(im, int(v))),
    "rotation": ("deg", [0, 3, 6, 10, 15, 22, 30],
                 lambda im, v: rotate(im, v)),
}


def evaluate_variant(img, gallery, det, rec, present: set) -> tuple:
    faces = det.detect(img, config.DETECTION_MODE)
    if not faces:
        return 0, 0, 0
    q = rec.embed_faces(img, faces)
    weights = {m.name: m.weight for m in rec.models}
    fused, ids = fuse_scores(q, gallery, weights)
    if fused is None or not len(ids):
        return len(faces), 0, 0
    thr = np.full(fused.shape, config.MATCH_THRESHOLD)
    for i, f in enumerate(faces):
        if min(f.width, f.height) < config.SMALL_FACE_PX:
            thr[i, :] += config.SMALL_FACE_THRESHOLD_BUMP
    assign = GlobalMatchOptimizer.optimize_assignments(fused, ids, threshold=thr)
    right = sum(1 for _, sid, _ in assign if sid in present)
    wrong = len(assign) - right
    return len(faces), right, wrong


def sparkline(values, lo, hi, width_chars="▁▂▃▄▅▆▇█"):
    span = max(hi - lo, 1e-9)
    return "".join(width_chars[min(len(width_chars) - 1,
                   max(0, int((v - lo) / span * (len(width_chars) - 1))))] for v in values)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", default="data/uploads/group_20260820_182245_858.jpg")
    ap.add_argument("--present", default="",
                    help="comma-separated student ids present (default: all enrolled)")
    ap.add_argument("--sources", default="id,restored,live")
    args = ap.parse_args()

    img = cv2.imread(args.photo)
    if img is None:
        print(f"Cannot read {args.photo}")
        return 1

    present = ({int(x) for x in args.present.split(",") if x.strip()}
               if args.present else {s["id"] for s in database.list_students()})
    gallery = load_gallery([s.strip() for s in args.sources.split(",")])
    det, rec = get_detector(), get_recognizer()

    base_faces, base_right, base_wrong = evaluate_variant(img, gallery, det, rec, present)

    print("=" * 74)
    print("  ROBUSTNESS ENVELOPE")
    print("=" * 74)
    print(f"  photo    : {Path(args.photo).name}")
    print(f"  baseline : {base_faces} faces detected, {base_right} correct, {base_wrong} wrong")
    print(f"  gallery  : {args.sources}")
    print()

    summary = {}
    for axis, (unit, values, fn) in AXES.items():
        print(f"  {axis.upper()}")
        print(f"    {'level':>8}{'faces':>8}{'correct':>9}{'wrong':>7}   {'retained':<10}")
        rights = []
        for v in values:
            variant = fn(img, v)
            n, right, wrong = evaluate_variant(variant, gallery, det, rec, present)
            rights.append(right)
            pct = right / base_right if base_right else 0
            bar = "#" * int(round(pct * 10)) + "." * (10 - int(round(pct * 10)))
            flag = ""
            if base_right and right < base_right * 0.7:
                flag = "  <-- degraded"
            if wrong:
                flag += "  <-- FALSE MATCH"
            print(f"    {v:>6}{unit:<2}{n:>8}{right:>9}{wrong:>7}   {bar} {pct*100:4.0f}%{flag}")
        summary[axis] = rights
        # Usable range = levels holding at least 90% of baseline correct matches
        ok = [v for v, r in zip(values, rights) if base_right and r >= base_right * 0.9]
        print(f"    holds >=90% at: {ok if ok else 'nowhere'}")
        print()

    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
