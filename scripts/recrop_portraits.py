"""Re-crop enrolment photos that were stored before the portrait step existed.

Enrolment used to save whatever frame the liveness check liked best, whole:
people mid-turn, ceiling above, other faces in shot. New enrolments are cropped
to a frontal face; the ones already in the database are not, and they are what
the Athlete Directory shows.

    python -m scripts.recrop_portraits                # show what would change
    python -m scripts.recrop_portraits --apply        # do it
    python -m scripts.recrop_portraits --apply --id 42

REVERSIBLE BY CONSTRUCTION. It writes a NEW file and repoints photo_path at it.
Nothing is overwritten and nothing is deleted, so putting it back is an UPDATE
of photo_path to the name printed in the "was" column.

WHAT IT CANNOT DO. It re-crops the single stored image; it cannot make somebody
look at a camera they were not looking at. A photo taken from waist height
becomes a tighter photo taken from waist height. Those people need to record
again - the capture now refuses to start until the phone is level, so a repeat
will come out right.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, db as pgdb, portrait, storage, utils  # noqa: E402
from backend.detector import FaceQualityAssessor, get_detector  # noqa: E402

# Above this the face already fills the frame, so it has been cropped before and
# re-cropping would only add margin around a margin.
#
# MEASURED, not guessed: this script's own output puts the face at 19-23% of the
# frame (n=12). 0.30 was above that whole range, so a second run re-cropped
# everything it had just cropped and zoomed out a step each time. 0.17 sits
# below the output and above the wide originals, which ran 6-20%.
ALREADY_CROPPED = 0.17


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the new crops (default: report only)")
    ap.add_argument("--id", type=int, help="one person, by id")
    args = ap.parse_args()

    det = get_detector()
    q = ("SELECT id, name, roll_no, photo_path FROM students "
         "WHERE photo_path IS NOT NULL AND photo_path <> ''")
    p: list = []
    if args.id:
        q += " AND id = ?"
        p.append(args.id)
    with pgdb.connect() as conn:
        people = [dict(r) for r in conn.execute(q + " ORDER BY id", p).fetchall()]

    print(f"{len(people)} people with a photo"
          f"{'' if args.apply else '   (DRY RUN - nothing will be written)'}\n")
    print(f"  {'id':>5}  {'name':<26} {'face fills':>10}  action")

    changed = skipped = failed = 0
    for s in people:
        name = Path(s["photo_path"]).name
        raw = storage.get("students", name)
        if not raw:
            print(f"  {s['id']:>5}  {str(s['name'])[:26]:<26} {'-':>10}  "
                  f"MISSING FILE {name}")
            failed += 1
            continue
        import numpy as np
        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            print(f"  {s['id']:>5}  {str(s['name'])[:26]:<26} {'-':>10}  UNREADABLE")
            failed += 1
            continue

        faces = det.detect(img)
        if not faces:
            print(f"  {s['id']:>5}  {str(s['name'])[:26]:<26} {'-':>10}  "
                  f"no face found - left alone")
            skipped += 1
            continue
        face = max(faces, key=lambda f: f.width * f.height)
        share = (face.width * face.height) / (img.shape[0] * img.shape[1])
        # Name first, share second. The share is a heuristic with a real overlap
        # against wide originals; a file this script wrote is a fact.
        if name.startswith("portrait_") or share >= ALREADY_CROPPED:
            print(f"  {s['id']:>5}  {str(s['name'])[:26]:<26} {share:>9.0%}  "
                  f"already a portrait")
            skipped += 1
            continue

        crop, info = portrait.from_single(img, det)
        qq = FaceQualityAssessor.assess(img, face)
        pitch = float(qq.get("pitch") or 0.0)
        warn = "  (still shot from below - ask them to record again)" \
            if abs(pitch) > config.MAX_PORTRAIT_PITCH else ""
        if not args.apply:
            print(f"  {s['id']:>5}  {str(s['name'])[:26]:<26} {share:>9.0%}  "
                  f"would crop -> {crop.shape[1]}x{crop.shape[0]}{warn}")
            changed += 1
            continue

        new_name = f"portrait_{s['id']}_{utils.timestamp()}.jpg"
        utils.save_image(crop, "students", new_name)
        with pgdb.connect() as conn:
            conn.execute("UPDATE students SET photo_path = ? WHERE id = ?",
                         (new_name, s["id"]))
        print(f"  {s['id']:>5}  {str(s['name'])[:26]:<26} {share:>9.0%}  "
              f"cropped -> {new_name}{warn}")
        print(f"         was: {name}")
        changed += 1

    print(f"\n{changed} {'to change' if not args.apply else 'changed'}, "
          f"{skipped} left alone, {failed} unreadable")
    if not args.apply and changed:
        print("\nRe-run with --apply to write them. The originals stay on disk;")
        print("photo_path is repointed, so any of these can be put back by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
