"""Biometric evaluation harness: FAR/FRR, EER, rank-1, and threshold calibration.

Reports the numbers that actually decide whether a face system is deployable,
rather than a single "it recognised 12 people" figure:

  Genuine / impostor separation   the distance between right and wrong matches
  FAR                             false accept rate - strangers let in
  FRR                             false reject rate - real athletes turned away
  EER                             the crossover, a single comparable quality score
  Rank-1                          how often the correct person is the top scorer
  Open-set rejection              behaviour when nobody present is enrolled

DATA LEAKAGE - READ THIS
------------------------
Continual learning writes `adapted` templates built from processed group photos.
Scoring those photos against a gallery containing them is self-matching: it
returns similarities of 1.000 and a meaningless 100% accuracy. This harness
therefore defaults to `--sources id,restored,live` so the gallery contains only
enrolment data and every test photo is genuinely unseen. Pass --include-adapted
to see the inflated numbers for comparison.

    python -m scripts.evaluate                       # evaluate on labelled photos
    python -m scripts.evaluate --sweep               # add threshold calibration
    python -m scripts.evaluate --labels my.json      # your own ground truth
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

DEFAULT_LABELS = Path(__file__).resolve().parent.parent / "data" / "eval_labels.json"


def load_gallery(sources: list[str]) -> dict:
    q = "SELECT id, student_id, model, vector FROM templates WHERE source IN (%s)" % (
        ",".join("?" * len(sources))
    )
    with database.connect() as conn:
        rows = conn.execute(q, sources).fetchall()
    g: dict = {}
    for r in rows:
        g.setdefault(r["model"], []).append(
            (int(r["id"]), int(r["student_id"]),
             np.frombuffer(r["vector"], dtype=np.float32))
        )
    return {
        m: (np.array([a for a, _, _ in v]),
            np.array([b for _, b, _ in v]),
            np.stack([c for _, _, c in v]))
        for m, v in g.items()
    }


def score_photo(path: str, gallery: dict, det, rec) -> tuple:
    """-> (faces, fused (F,S) similarity matrix, student_ids)."""
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(path)
    faces = det.detect(img, config.DETECTION_MODE)
    if not faces:
        return [], np.zeros((0, 0)), []
    q = rec.embed_faces(img, faces)
    weights = {m.name: m.weight for m in rec.models}
    fused, ids = fuse_scores(q, gallery, weights)
    return faces, fused, ids


def collect_scores(labels: dict, gallery: dict, det, rec, names: dict) -> dict:
    """Split every (face, student) similarity into genuine and impostor pools."""
    genuine, impostor = [], []
    per_person: dict = {}
    photo_rows = []

    for photo, spec in labels.items():
        present = set(spec.get("present_ids", []))
        expected = spec.get("expected_faces")
        faces, fused, ids = score_photo(photo, gallery, det, rec)
        if not len(ids):
            continue

        # Assign faces to identities the same way production does, so genuine
        # scores reflect the real matcher and not an oracle.
        thr = np.full(fused.shape, config.MATCH_THRESHOLD)
        for i, f in enumerate(faces):
            if min(f.width, f.height) < config.SMALL_FACE_PX:
                thr[i, :] += config.SMALL_FACE_THRESHOLD_BUMP
        assign = {i: sid for i, sid, _ in
                  GlobalMatchOptimizer.optimize_assignments(fused, ids, threshold=thr)}

        for i in range(fused.shape[0]):
            top_j = int(np.argmax(fused[i]))
            top_sid = int(ids[top_j])
            for j, sid in enumerate(ids):
                s = float(fused[i, j])
                if int(sid) in present and int(sid) == top_sid:
                    genuine.append(s)
                    per_person.setdefault(int(sid), []).append(s)
                else:
                    impostor.append(s)

        photo_rows.append({
            "photo": Path(photo).name,
            "faces_detected": len(faces),
            "expected_faces": expected,
            "enrolled_present": len(present),
            "matched": len(assign),
            "correct": sum(1 for sid in assign.values() if sid in present),
            "wrong": sum(1 for sid in assign.values() if sid not in present),
        })

    return {"genuine": np.array(genuine), "impostor": np.array(impostor),
            "per_person": per_person, "photos": photo_rows}


def rates(genuine: np.ndarray, impostor: np.ndarray, t: float) -> tuple:
    far = float((impostor >= t).mean()) if len(impostor) else 0.0
    frr = float((genuine < t).mean()) if len(genuine) else 0.0
    return far, frr


def sweep(genuine: np.ndarray, impostor: np.ndarray) -> dict:
    grid = np.arange(0.20, 0.85, 0.005)
    rows = [(t, *rates(genuine, impostor, t)) for t in grid]
    eer_t, eer_far, eer_frr = min(rows, key=lambda r: abs(r[1] - r[2]))
    zero_far = [r for r in rows if r[1] == 0.0]
    return {
        "rows": rows,
        "eer": {"threshold": float(eer_t), "far": eer_far, "frr": eer_frr,
                "eer": (eer_far + eer_frr) / 2},
        "zero_far": ({"threshold": float(zero_far[0][0]), "frr": zero_far[0][2]}
                     if zero_far else None),
    }


def bar(v: float, width: int = 28) -> str:
    n = int(round(v * width))
    return "#" * n + "." * (width - n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(DEFAULT_LABELS))
    ap.add_argument("--sources", default="id,restored,live",
                    help="template sources in the gallery (default excludes 'adapted')")
    ap.add_argument("--include-adapted", action="store_true",
                    help="add adapted templates - inflates results by self-matching")
    ap.add_argument("--sweep", action="store_true", help="threshold calibration table")
    args = ap.parse_args()

    labels_path = Path(args.labels)
    if not labels_path.exists():
        print(f"No label file at {labels_path}.")
        print("Create one mapping each photo to the student ids genuinely present:")
        print('  {"data/uploads/x.jpg": {"present_ids": [56,57], "expected_faces": 13}}')
        return 1
    labels = json.loads(labels_path.read_text(encoding="utf-8"))

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.include_adapted and "adapted" not in sources:
        sources.append("adapted")

    gallery = load_gallery(sources)
    if not gallery:
        print(f"No templates for sources {sources}.")
        return 1
    names = {s["id"]: s["name"] for s in database.list_students()}
    det, rec = get_detector(), get_recognizer()

    n_tpl = sum(len(v[0]) for v in gallery.values())
    print("=" * 68)
    print("  FACEMARK BIOMETRIC EVALUATION")
    print("=" * 68)
    print(f"  gallery sources : {', '.join(sources)}")
    print(f"  templates       : {n_tpl} across {len(gallery)} model(s)")
    print(f"  enrolled people : {len(names)}")
    print(f"  detection mode  : {config.DETECTION_MODE}")
    print(f"  active threshold: {config.MATCH_THRESHOLD}")
    if args.include_adapted:
        print("  WARNING: adapted templates included - results are self-matched")
    print()

    res = collect_scores(labels, gallery, det, rec, names)
    g, imp = res["genuine"], res["impostor"]

    print("-" * 68)
    print("  PER-PHOTO")
    print("-" * 68)
    print(f"  {'photo':<34}{'faces':>7}{'exp':>5}{'matched':>9}{'right':>7}{'wrong':>7}")
    for r in res["photos"]:
        exp = r["expected_faces"] if r["expected_faces"] is not None else "-"
        print(f"  {r['photo']:<34}{r['faces_detected']:>7}{exp:>5}"
              f"{r['matched']:>9}{r['correct']:>7}{r['wrong']:>7}")
    print()

    if not len(g) or not len(imp):
        print("  Not enough labelled data for score statistics.")
        return 0

    print("-" * 68)
    print("  SCORE SEPARATION")
    print("-" * 68)
    print(f"  genuine  n={len(g):<5} mean {g.mean():.3f}  sd {g.std():.3f}  "
          f"min {g.min():.3f}  max {g.max():.3f}")
    print(f"  impostor n={len(imp):<5} mean {imp.mean():.3f}  sd {imp.std():.3f}  "
          f"min {imp.min():.3f}  max {imp.max():.3f}")
    margin = g.min() - imp.max()
    d_prime = (g.mean() - imp.mean()) / np.sqrt((g.var() + imp.var()) / 2)
    print(f"  worst genuine - best impostor : {margin:+.3f}"
          f"   {'(separable)' if margin > 0 else '(OVERLAP)'}")
    print(f"  d-prime (separation index)    : {d_prime:.2f}")
    print()

    far, frr = rates(g, imp, config.MATCH_THRESHOLD)
    print(f"  at the active threshold {config.MATCH_THRESHOLD}:")
    print(f"    FAR {far * 100:6.2f}%   {int(far * len(imp))} of {len(imp)} impostor pairs accepted")
    print(f"    FRR {frr * 100:6.2f}%   {int(frr * len(g))} of {len(g)} genuine pairs rejected")
    print()

    if args.sweep:
        sw = sweep(g, imp)
        print("-" * 68)
        print("  THRESHOLD CALIBRATION")
        print("-" * 68)
        print(f"  {'thr':>6}{'FAR':>9}{'FRR':>9}   {'FAR bar':<30}")
        for t, fa, fr in sw["rows"]:
            if abs((t * 1000) % 25) > 1e-6:
                continue
            print(f"  {t:6.3f}{fa * 100:8.2f}%{fr * 100:8.2f}%   {bar(fa)}")
        e = sw["eer"]
        print()
        print(f"  Equal error rate : {e['eer'] * 100:.2f}% at threshold {e['threshold']:.3f}")
        if sw["zero_far"]:
            z = sw["zero_far"]
            print(f"  Zero-FAR point   : threshold {z['threshold']:.3f} "
                  f"rejects {z['frr'] * 100:.1f}% of genuine matches")
        print()

    print("-" * 68)
    print("  PER-PERSON GENUINE SCORES (weakest first)")
    print("-" * 68)
    rows = sorted(((names.get(sid, sid), np.mean(v), np.min(v), len(v))
                   for sid, v in res["per_person"].items()), key=lambda r: r[1])
    for name, mean, mn, n in rows:
        flag = "  <-- weakest" if mean < config.MATCH_THRESHOLD + 0.1 else ""
        print(f"  {str(name):<16} mean {mean:.3f}  min {mn:.3f}  n={n}{flag}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
