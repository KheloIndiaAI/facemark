"""Benchmark a fully permissively-licensed stack against the current one.

WHY THIS EXISTS
---------------
The current pipeline cannot be deployed without resolving two licences:

  Ultralytics YOLO11   AGPL-3.0. Its network clause triggers when users reach
                       the software over a network, which is exactly what
                       deploying to AWS does. You would owe every user the
                       complete source of the whole application under AGPL.
  InsightFace models   "ALL models are available for non-commercial research
                       purposes only" - covering det_10g, glintr100, w600k_r50.

The alternative uses only OpenCV's own models:

  YuNet   face detection    MIT
  SFace   face recognition  Apache-2.0

Both ship inside OpenCV (Apache-2.0) and total 37 MB against 1,022 MB.

This measures the accuracy cost on the same photos and ground truth used
everywhere else, so the licence decision is made against numbers rather than
guesses.

    python -m scripts.permissive_bench
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database  # noqa: E402
from backend.detector import get_detector  # noqa: E402
from backend.metaheuristics import GlobalMatchOptimizer  # noqa: E402
from backend.recognizer import fuse_scores, get_recognizer  # noqa: E402
from scripts.evaluate import load_gallery  # noqa: E402

YUNET = config.MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE = config.MODELS_DIR / "face_recognition_sface_2021dec.onnx"

DELHI = "data/uploads/group_20260820_182245_858.jpg"
STRANGERS = "data/uploads/group_20260820_182331_818.jpg"


def centre_students(code: str) -> list:
    with database.connect() as c:
        row = c.execute("SELECT id FROM centres WHERE code=?", (code,)).fetchone()
        if not row:
            return []
        return [(r[0], r[1], r[2]) for r in c.execute(
            "SELECT id,name,photo_path FROM students WHERE centre_id=?", (row[0],))]


# --------------------------------------------------------------- permissive

def yunet_detect(img, score=0.6, nms=0.3):
    h, w = img.shape[:2]
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), score, nms, 5000)
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    return faces if faces is not None else np.empty((0, 15), np.float32)


def sface_embed(img, faces):
    """faces: YuNet rows (x,y,w,h, 5 landmarks..., score) -> L2-normalised vectors."""
    rec = cv2.FaceRecognizerSF.create(str(SFACE), "")
    out = []
    for row in faces:
        aligned = rec.alignCrop(img, row)
        v = rec.feature(aligned).flatten().astype(np.float32)
        out.append(v / (np.linalg.norm(v) + 1e-10))
    return np.stack(out) if out else np.zeros((0, 128), np.float32)


def permissive_gallery(students):
    ids, vecs = [], []
    for sid, name, path in students:
        im = cv2.imread(path)
        if im is None:
            continue
        # Passport thumbnails are small; give the detector more pixels to work with.
        if max(im.shape[:2]) < 400:
            im = cv2.resize(im, (im.shape[1] * 3, im.shape[0] * 3), interpolation=cv2.INTER_CUBIC)
        f = yunet_detect(im)
        if len(f) == 0:
            continue
        biggest = f[np.argmax(f[:, 2] * f[:, 3])][None, :]
        v = sface_embed(im, biggest)
        if len(v):
            ids.append(sid); vecs.append(v[0])
    return np.array(ids), (np.stack(vecs) if vecs else np.zeros((0, 128), np.float32))


def run_permissive(photo, gal_ids, gal_vecs, present, threshold):
    img = cv2.imread(photo)
    t0 = time.perf_counter()
    faces = yunet_detect(img)
    q = sface_embed(img, faces)
    dt = time.perf_counter() - t0
    if not len(q) or not len(gal_vecs):
        return {"faces": len(faces), "matched": 0, "right": 0, "secs": dt}
    sims = q @ gal_vecs.T
    m = GlobalMatchOptimizer.optimize_assignments(sims, list(gal_ids), threshold=threshold)
    right = sum(1 for _, sid, _ in m if sid in present)
    return {"faces": len(faces), "matched": len(m), "right": right, "secs": dt}


# ------------------------------------------------------------------ current

def run_current(photo, gallery, present):
    det, rec = get_detector(), get_recognizer()
    img = cv2.imread(photo)
    t0 = time.perf_counter()
    faces = det.detect(img, "fused")
    q = rec.embed_faces(img, faces)
    fused, gids = fuse_scores(q, gallery, {m.name: m.weight for m in rec.models})
    dt = time.perf_counter() - t0
    if fused is None or not len(gids):
        return {"faces": len(faces), "matched": 0, "right": 0, "secs": dt}
    thr = np.full(fused.shape, config.MATCH_THRESHOLD)
    for i, f in enumerate(faces):
        if min(f.width, f.height) < config.SMALL_FACE_PX:
            thr[i, :] += config.SMALL_FACE_THRESHOLD_BUMP
    m = GlobalMatchOptimizer.optimize_assignments(fused, gids, threshold=thr)
    right = sum(1 for _, sid, _ in m if sid in present)
    return {"faces": len(faces), "matched": len(m), "right": right, "secs": dt}


def main() -> int:
    if not YUNET.exists() or not SFACE.exists():
        print("YuNet/SFace models missing from data/models/.")
        return 1

    delhi = centre_students("DEMO-DL-01")
    present = {s[0] for s in delhi}
    if not delhi:
        print("Delhi centre not found - it is the only set with ground truth.")
        return 1

    gallery = load_gallery(["id", "restored", "live"])
    gallery = {m: (t[0][np.isin(t[1], list(present))],
                   t[1][np.isin(t[1], list(present))],
                   t[2][np.isin(t[1], list(present))]) for m, t in gallery.items()}

    print("Building the permissive gallery (YuNet + SFace) ...")
    gid, gvec = permissive_gallery(delhi)
    print(f"  {len(gid)} of {len(delhi)} enrolled, {gvec.shape[1] if len(gvec) else 0}-d embeddings")
    print()

    # SFace's published operating point is around 0.363, but that is tuned for
    # 1:1 verification. Open-set attendance needs a much higher bar - measured
    # here, everything below 0.55 lets strangers through. Sweeping wide matters:
    # a range that stopped at 0.50 would have wrongly concluded the model was
    # unusable, when the clean point sits just above it.
    print("Permissive stack, threshold sweep on the ground-truth photo:")
    best = (0, None)
    for t in (0.363, 0.45, 0.50, 0.55, 0.575, 0.60, 0.65, 0.70):
        d = run_permissive(DELHI, gid, gvec, present, t)
        s = run_permissive(STRANGERS, gid, gvec, present, t)
        flag = "  <-- false matches" if s["matched"] else ""
        print(f"   thr {t:.3f}: {d['right']:>2}/13 correct, strangers {s['matched']}{flag}")
        if not s["matched"] and d["right"] > best[0]:
            best = (d["right"], t)
    print()

    print("=" * 62)
    print(f"  {'stack':<22}{'correct':>9}{'strangers':>11}{'secs':>8}{'MB':>7}")
    print("=" * 62)
    cur_d = run_current(DELHI, gallery, present)
    cur_s = run_current(STRANGERS, gallery, present)
    print(f"  {'current (AGPL + NC)':<22}{cur_d['right']:>6}/13{cur_s['matched']:>11}"
          f"{cur_d['secs']:>8.1f}{1022:>7}")
    if best[1] is not None:
        p_d = run_permissive(DELHI, gid, gvec, present, best[1])
        p_s = run_permissive(STRANGERS, gid, gvec, present, best[1])
        print(f"  {'permissive (MIT+Apc)':<22}{p_d['right']:>6}/13{p_s['matched']:>11}"
              f"{p_d['secs']:>8.1f}{37:>7}")
        print(f"\n  best permissive threshold: {best[1]:.3f}")
    else:
        print(f"  {'permissive (MIT+Apc)':<22}{'--':>9}{'--':>11}   no clean operating point")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
