"""Measure candidate accuracy upgrades against ground truth and a control.

Any change that raises recall is only worth having if it does not also start
matching strangers, so every configuration is scored on three photos at once:

  Delhi     13 enrolled athletes, ground truth known    -> recall must not drop
  WL        22 enrolled, small faces, the hard case     -> the recall we want
  Strangers 16 faces, nobody enrolled                   -> MUST stay at zero

A configuration that gains on WL while gaining on Strangers has gained nothing.

    python -m scripts.tune
    python -m scripts.tune --only baseline,restore_query
"""
from __future__ import annotations

import argparse
import copy
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

DELHI = "data/uploads/group_20260820_182245_858.jpg"
STRANGERS = "data/uploads/group_20260820_182331_818.jpg"
WL = r"C:\Users\Jatin Singh\Downloads\WhatsApp Image 2026-08-21 at 2.03.51 PM.jpeg"


def centre_ids(code: str) -> set:
    with database.connect() as c:
        row = c.execute("SELECT id FROM centres WHERE code=?", (code,)).fetchone()
        if not row:
            return set()
        return {r[0] for r in c.execute(
            "SELECT id FROM students WHERE centre_id=?", (row[0],))}


def scoped_gallery(ids: set) -> dict:
    g = load_gallery(["id", "restored", "live"])
    out = {}
    for m, (tid, sid, mat) in g.items():
        mask = np.isin(sid, list(ids))
        if mask.any():
            out[m] = (tid[mask], sid[mask], mat[mask])
    return out


def run_photo(path, gallery, cfg, det, rec, enhancer=None):
    img = cv2.imread(path)
    if img is None:
        return None
    if cfg.get("upscale", 1.0) != 1.0:
        f = cfg["upscale"]
        img = cv2.resize(img, (int(img.shape[1] * f), int(img.shape[0] * f)),
                         interpolation=cv2.INTER_CUBIC)
    faces = det.detect(img, cfg.get("mode", "fused"))
    if not faces:
        return {"faces": 0, "matched": 0, "ids": set()}

    if cfg.get("restore_query") and enhancer is not None and enhancer.restoration_enabled:
        # Restore every query face and average its embedding with the raw one.
        from backend.detector import Face, estimate_landmarks
        from backend.enhancer import FFHQ_LANDMARKS
        raw = rec.embed_faces(img, faces)
        restored_vecs = {m.name: [] for m in rec.models}
        for f in faces:
            lm = f.landmarks if f.landmarks is not None else estimate_landmarks(f.box)
            r = enhancer.restore(img, lm)
            if r is None:
                for m in rec.models:
                    restored_vecs[m.name].append(None)
                continue
            rf = Face(box=(0, 0, 512, 512), conf=1.0, landmarks=FFHQ_LANDMARKS, source="gfpgan")
            e = rec.embed_faces(r, [rf])
            for m in rec.models:
                restored_vecs[m.name].append(e[m.name][0] if len(e[m.name]) else None)
        q = {}
        for m in rec.models:
            rows = []
            for i in range(len(faces)):
                v = raw[m.name][i]
                rv = restored_vecs[m.name][i]
                if rv is not None:
                    v = v + rv
                    v = v / (np.linalg.norm(v) + 1e-10)
                rows.append(v)
            q[m.name] = np.stack(rows)
    else:
        q = rec.embed_faces(img, faces)

    fused, gids = fuse_scores(q, gallery, {m.name: m.weight for m in rec.models})
    if fused is None or not len(gids):
        return {"faces": len(faces), "matched": 0, "ids": set()}

    thr = np.full(fused.shape, cfg.get("threshold", config.MATCH_THRESHOLD))
    bump = cfg.get("small_bump", config.SMALL_FACE_THRESHOLD_BUMP)
    small_px = cfg.get("small_px", config.SMALL_FACE_PX)
    for i, f in enumerate(faces):
        if min(f.width, f.height) < small_px:
            thr[i, :] += bump
    m = GlobalMatchOptimizer.optimize_assignments(fused, gids, threshold=thr)
    return {"faces": len(faces), "matched": len(m), "ids": {sid for _, sid, _ in m}}


CONFIGS = {
    "baseline":            {},
    "no_small_bump":       {"small_bump": 0.0},
    "half_small_bump":     {"small_bump": 0.04},
    "accurate_mode":       {"mode": "accurate"},
    "upscale_1.5x":        {"upscale": 1.5},
    "upscale_2x":          {"upscale": 2.0},
    "restore_query":       {"restore_query": True},
    "upscale2x_no_bump":   {"upscale": 2.0, "small_bump": 0.0},
    "restore_no_bump":     {"restore_query": True, "small_bump": 0.0},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    det, rec = get_detector(), get_recognizer()
    from backend.enhancer import get_enhancer
    enh = get_enhancer()

    delhi_ids = centre_ids("DEMO-DL-01")
    wl_ids = centre_ids("KISCE-WL")
    g_delhi = scoped_gallery(delhi_ids)
    g_wl = scoped_gallery(wl_ids)

    names = args.only.split(",") if args.only else list(CONFIGS)
    print(f"Delhi roster {len(delhi_ids)}   WL roster {len(wl_ids)}")
    print()
    print(f"  {'config':<20}{'Delhi':>14}{'WL':>14}{'Strangers':>12}{'secs':>7}")
    print(f"  {'':<20}{'(13 truth)':>14}{'(22 roster)':>14}{'(must be 0)':>12}")
    print("  " + "-" * 67)

    for name in names:
        cfg = CONFIGS.get(name.strip())
        if cfg is None:
            continue
        t0 = time.perf_counter()
        d = run_photo(DELHI, g_delhi, cfg, det, rec, enh)
        w = run_photo(WL, g_wl, cfg, det, rec, enh)
        s = run_photo(STRANGERS, g_delhi, cfg, det, rec, enh)
        dt = time.perf_counter() - t0
        flag = ""
        if s["matched"] > 0:
            flag = "  <-- FALSE MATCHES"
        elif d["matched"] >= 13 and w["matched"] > 6:
            flag = "  <-- better"
        print(f"  {name:<20}{d['matched']:>6}/13{'':>5}{w['matched']:>6}/22{'':>5}"
              f"{s['matched']:>8}{'':>4}{dt:>7.0f}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
