"""Live test: does matching globally, then resolving the centre, hold up?

The system scoped the gallery to the selected centre before matching, which
made "wrong centre selected" indistinguishable from "recognition failed".
Matching globally removes that failure mode, but it enlarges the impostor
space: every face is now compared against every enrolled person at every
centre rather than one roster, so the chance of a confident wrong answer
rises. This measures both halves of that trade on the real photographs
rather than arguing it from theory.

    python -m scripts.live_test
    python -m scripts.live_test --sweep      # threshold behaviour
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database  # noqa: E402
from backend.detector import get_detector  # noqa: E402
from backend.recognizer import get_recognizer  # noqa: E402

TESTSET = Path("data/testset")

# What each photograph is, and what a correct system should say about it.
# `expect_centre` is the centre every recognisable person in the frame belongs
# to; `enrolled` is how many of them are actually in the gallery.
PHOTOS = [
    {"file": "delhi_a.jpg",   "centre": "DEMO-DL-01", "enrolled": 13,
     "note": "class group photo, all 13 enrolled present"},
    {"file": "delhi_b.jpg",   "centre": "DEMO-DL-01", "enrolled": 13,
     "note": "second frame of the same burst - repeatability, not independent"},
    {"file": "delhi_c.jpg",   "centre": "DEMO-DL-01", "enrolled": 13,
     "note": "same group, different session"},
    {"file": "wl_group.jpg",  "centre": "KISCE-WL",   "enrolled": None,
     "note": "weightlifting centre, enrolled from PDF passport photos"},
    {"file": "strangers.jpg", "centre": None,         "enrolled": 0,
     "note": "nobody in this photo is enrolled - open-set rejection test"},
]


def load_gallery():
    """Every template, with the centre its owner belongs to."""
    with database.connect() as c:
        rows = list(c.execute(
            "SELECT t.student_id, s.name, cn.code, t.vector "
            "FROM templates t JOIN students s ON s.id = t.student_id "
            "LEFT JOIN centres cn ON cn.id = s.centre_id"))
    out = []
    for sid, name, code, blob in rows:
        v = np.frombuffer(blob, dtype=np.float32).astype(np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
        out.append({"sid": sid, "name": name, "centre": code, "vec": v})
    return out


def match(vecs, gallery, thr, restrict_centre=None):
    """Best match per face. `restrict_centre` reproduces the old scoped behaviour."""
    pool = [g for g in gallery
            if restrict_centre is None or g["centre"] == restrict_centre]
    if not pool:
        return [None] * len(vecs)
    M = np.stack([g["vec"] for g in pool])           # (G, D)
    out = []
    for v in vecs:
        v = v / (np.linalg.norm(v) + 1e-9)
        sims = M @ v
        j = int(sims.argmax())
        # Max-pool per student: several templates may belong to one person, so
        # the best template for a person IS that person's score.
        out.append({"name": pool[j]["name"], "centre": pool[j]["centre"],
                    "sim": float(sims[j])} if sims[j] >= thr else None)
    return out


def run(thr, verbose=True):
    det, rec = get_detector(), get_recognizer()
    gallery = load_gallery()
    centres = sorted({g["centre"] for g in gallery if g["centre"]})
    if verbose:
        print(f"gallery: {len(gallery)} templates across {len(centres)} centres "
              f"({', '.join(centres)})")
        print(f"threshold: {thr}\n")

    totals = {"global_right": 0, "global_wrong_centre": 0, "global_stranger": 0,
              "scoped_right": 0, "faces": 0}

    for spec in PHOTOS:
        path = TESTSET / spec["file"]
        img = cv2.imread(str(path))
        if img is None:
            print(f"  MISSING  {spec['file']}")
            continue
        faces = det.detect(img, "fused")
        vecs = rec.embed_faces(img, faces)[config.SFACE_MODEL] if faces else []
        totals["faces"] += len(faces)

        glob = match(vecs, gallery, thr)
        scoped = match(vecs, gallery, thr, restrict_centre=spec["centre"]) \
            if spec["centre"] else [None] * len(vecs)

        # A global match is "right" when the person found belongs to the centre
        # the photo was taken at. On the strangers photo ANY match is wrong.
        g_hits = [m for m in glob if m]
        if spec["centre"] is None:
            g_right, g_wrong = 0, len(g_hits)
            totals["global_stranger"] += len(g_hits)
        else:
            g_right = sum(1 for m in g_hits if m["centre"] == spec["centre"])
            g_wrong = len(g_hits) - g_right
            totals["global_right"] += g_right
            totals["global_wrong_centre"] += g_wrong
        s_hits = sum(1 for m in scoped if m)
        totals["scoped_right"] += s_hits

        if verbose:
            print(f"{spec['file']:<16} {spec['note']}")
            print(f"   faces {len(faces):<3}  scoped-to-{str(spec['centre']):<11} "
                  f"recognised {s_hits:<3}   global recognised {g_right}"
                  + (f"  + {g_wrong} FROM ANOTHER CENTRE" if g_wrong else ""))
            for m in g_hits:
                if spec["centre"] is None or m["centre"] != spec["centre"]:
                    print(f"      false positive: {m['name']} "
                          f"({m['centre']}) at {m['sim']:.3f}")
            print()
    return totals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--threshold", type=float, default=config.MATCH_THRESHOLD)
    args = ap.parse_args()

    if args.sweep:
        det, rec = get_detector(), get_recognizer()
        gallery = load_gallery()
        print(f"{'thr':>6}{'recall':>9}{'wrong-centre':>15}{'strangers':>12}")
        cache = {}
        for spec in PHOTOS:
            img = cv2.imread(str(TESTSET / spec["file"]))
            if img is None:
                continue
            faces = det.detect(img, "fused")
            cache[spec["file"]] = rec.embed_faces(img, faces)[config.SFACE_MODEL] \
                if faces else []
        for thr in [0.45, 0.50, 0.55, 0.57, 0.60, 0.65, 0.70]:
            right = wrong = stranger = 0
            for spec in PHOTOS:
                vecs = cache.get(spec["file"], [])
                hits = [m for m in match(vecs, gallery, thr) if m]
                if spec["centre"] is None:
                    stranger += len(hits)
                else:
                    right += sum(1 for m in hits if m["centre"] == spec["centre"])
                    wrong += sum(1 for m in hits if m["centre"] != spec["centre"])
            print(f"{thr:>6.2f}{right:>9}{wrong:>15}{stranger:>12}")
        return 0

    t = run(args.threshold)
    print("=" * 68)
    print(f"  faces examined            {t['faces']}")
    print(f"  scoped   recognised       {t['scoped_right']}")
    print(f"  global   recognised       {t['global_right']}")
    print(f"  global   wrong centre     {t['global_wrong_centre']}")
    print(f"  global   matched stranger {t['global_stranger']}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
