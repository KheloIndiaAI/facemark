"""Rebuild every template with SFace after the licence migration.

The previous stack produced 512-dimensional ArcFace vectors; SFace produces
128-dimensional ones. They are not comparable, and a gallery holding the old
vectors would silently fail to match anything, so every template has to be
rebuilt from the enrolment photographs on disk.

Photographs are untouched - only the derived embeddings are replaced.

    python -m scripts.reenroll_sface --dry-run
    python -m scripts.reenroll_sface --apply
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
from backend.enhancer import sharpness_quality  # noqa: E402
from backend.recognizer import EMBED_DIM, get_recognizer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--upscale-small", type=int, default=3,
                    help="upscale factor for enrolment photos under 400px")
    args = ap.parse_args()

    with database.connect() as c:
        rows = list(c.execute(
            "SELECT s.id, s.name, s.roll_no, s.photo_path, c.code "
            "FROM students s LEFT JOIN centres c ON c.id = s.centre_id "
            "ORDER BY c.code, s.name"))
        stale = c.execute("SELECT COUNT(*) FROM templates").fetchone()[0]

    print(f"{len(rows)} people, {stale} existing template(s) to replace")
    print(f"target embedding dimension: {EMBED_DIM}")
    print()

    if not args.apply or args.dry_run:
        for sid, name, roll, path, code in rows:
            ok = Path(path).exists() if path else False
            print(f"  {'ok ' if ok else 'MISSING'}  {code or '-':<12}{name[:28]:<30}{roll}")
        print("\nDry run. Re-run with --apply to rebuild.")
        return 0

    det, rec = get_detector(), get_recognizer()

    with database.connect() as c:
        c.execute("DELETE FROM templates")
    print("cleared the old gallery\n")

    ok = failed = 0
    for sid, name, roll, path, code in rows:
        if not path or not Path(path).exists():
            print(f"  SKIP  {name[:30]:<32} enrolment photo missing")
            failed += 1
            continue
        img = cv2.imread(path)
        if img is None:
            print(f"  SKIP  {name[:30]:<32} photo unreadable")
            failed += 1
            continue

        # PDF-embedded passport photos are small; give the detector more pixels.
        if max(img.shape[:2]) < 400 and args.upscale_small > 1:
            f = args.upscale_small
            img = cv2.resize(img, (img.shape[1] * f, img.shape[0] * f),
                             interpolation=cv2.INTER_CUBIC)

        faces = det.detect(img, "accurate")
        if not faces:
            print(f"  FAIL  {name[:30]:<32} no face detected")
            failed += 1
            continue
        face = max(faces, key=lambda x: x.width * x.height)

        emb = rec.embed_faces(img, [face])
        crop = img[max(0, int(face.box[1])):int(face.box[3]),
                   max(0, int(face.box[0])):int(face.box[2])]
        quality = sharpness_quality(crop) if crop.size else 0.0
        templates = [
            {"model": m, "vector": v[0], "source": "id", "quality": quality}
            for m, v in emb.items() if len(v)
        ]
        if not templates:
            print(f"  FAIL  {name[:30]:<32} embedding failed")
            failed += 1
            continue
        n = database.add_templates(sid, templates)
        ok += 1
        print(f"  ok    {name[:30]:<32} {n} template(s), face {face.width:.0f}px, q={quality:.2f}")

    print()
    print(f"Rebuilt {ok}, failed {failed}.")
    print("Verify before relying on it:")
    print("  python -m scripts.evaluate --sweep")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
