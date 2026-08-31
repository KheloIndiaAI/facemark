"""Enrol a roster from a PDF of ID photos, then evaluate against a group photo.

Built for the Khelo India NSRS roster export: one table row per person carrying
NSRS ID, name, gender and a small embedded passport photo.

This is a genuine held-out test. Enrolment comes only from the PDF photos; the
group photo is never used to build a template, so nothing is self-matched.

    python -m scripts.enroll_from_pdf --pdf roster.pdf --centre-code KISCE-WL --dry-run
    python -m scripts.enroll_from_pdf --pdf roster.pdf --centre-code KISCE-WL --apply
    python -m scripts.enroll_from_pdf --test-photo group.jpg --centre-code KISCE-WL

WHY THE PHOTOS ARE UPSCALED
---------------------------
PDF-embedded passport photos are stored small - faces here measure 24-77 px
against an enrolment floor of 48 px. Upscaling adds no detail, but it gives the
5-point aligner more pixels to work with and puts the crop in the range GFPGAN
restoration expects. The underlying detail is still limited, and that shows up
as lower similarity scores rather than as an error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database  # noqa: E402


def extract(pdf_path: Path, workdir: Path) -> list[dict]:
    """-> [{nsrs_id, name, gender, role, image_path}] in table order."""
    from pypdf import PdfReader

    workdir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf_path))

    people: list[dict] = []
    images: list[Path] = []

    for pi, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        # Rows look like: WEAA015M15 Bathula Rithwik Male
        # Names can wrap across lines, so join first and match on the whole blob.
        blob = re.sub(r"\s+", " ", text)
        is_coach_table = "COACH NAME" in text.upper()
        for m in re.finditer(r"([A-Z]{2,4}\d{3}[A-Z]?[MF]\d{2})\s+(.+?)\s+(Male|Female)", blob):
            nsrs, name, gender = m.group(1), m.group(2).strip(), m.group(3)
            # A coach id like CWE008AM78 appears in the coach table.
            role = "coach" if (is_coach_table and nsrs.startswith("C")) else "athlete"
            people.append({"nsrs_id": nsrs, "name": name, "gender": gender[0],
                           "role": role, "page": pi + 1})

        for ii, im in enumerate(page.images):
            fn = workdir / f"p{pi+1}_{ii:02d}.png"
            fn.write_bytes(im.data)
            images.append(fn)

    if len(people) != len(images):
        print(f"  WARNING: {len(people)} roster rows but {len(images)} images. "
              f"Pairing is by order and cannot be trusted - verify manually.")
    for p, img in zip(people, images):
        p["image_path"] = str(img)
    return people[:len(images)]


def prepare_face_image(path: str, scale: int = 3) -> np.ndarray | None:
    img = cv2.imread(path)
    if img is None:
        return None
    h, w = img.shape[:2]
    return cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf")
    ap.add_argument("--centre-code", required=True)
    ap.add_argument("--centre-name", default="")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--password", default="")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-photo")
    ap.add_argument("--scale", type=int, default=3)
    args = ap.parse_args()

    import os
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    import requests

    workdir = Path(config.DATA_DIR) / "roster_import" / args.centre_code
    B = args.base.rstrip("/") + "/api"
    s = requests.Session()

    if args.password:
        r = s.post(B + "/auth/login",
                   data={"username": args.username, "password": args.password})
        if r.status_code != 200:
            print(f"Login failed ({r.status_code}).")
            return 1

    # ---------------------------------------------------------------- enrol
    if args.pdf:
        people = extract(Path(args.pdf), workdir)
        print(f"Roster: {len(people)} people "
              f"({sum(1 for p in people if p['role']=='athlete')} athletes, "
              f"{sum(1 for p in people if p['role']=='coach')} coach)")
        print()
        print(f"  {'NSRS ID':<14}{'name':<32}{'g':<3}{'role':<9}{'face px':>8}")
        for p in people:
            img = prepare_face_image(p["image_path"], args.scale)
            p["face_px"] = "?"
            if img is not None:
                from backend.detector import get_detector
                faces = get_detector().detect(img, "accurate")
                p["face_px"] = round(max((f.width for f in faces), default=0))
            print(f"  {p['nsrs_id']:<14}{p['name'][:31]:<32}{p['gender']:<3}"
                  f"{p['role']:<9}{p['face_px']:>8}")
        print()

        if args.dry_run or not args.apply:
            print("Dry run - nothing enrolled. Re-run with --apply.")
            return 0

        # centre
        centres = s.get(B + "/centres").json()["centres"]
        match = [c for c in centres if c["code"].upper() == args.centre_code.upper()]
        if match:
            centre_id = match[0]["id"]
            print(f"Using existing centre {args.centre_code} (id {centre_id})")
        else:
            r = s.post(B + "/centres", data={
                "code": args.centre_code,
                "name": args.centre_name or args.centre_code,
                "sports": "Weightlifting",
            })
            if r.status_code != 200:
                print(f"Could not create centre: {r.status_code} {r.text[:200]}")
                return 1
            centre_id = r.json()["centre_id"]
            print(f"Created centre {args.centre_code} (id {centre_id})")

        ok = fail = 0
        for p in people:
            img = prepare_face_image(p["image_path"], args.scale)
            if img is None:
                fail += 1
                continue
            ok_enc, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
            r = s.post(B + "/students", files={
                "photo": (f"{p['nsrs_id']}.jpg", buf.tobytes(), "image/jpeg")
            }, data={
                "name": p["name"], "roll_no": p["nsrs_id"], "role": p["role"],
                "centre_id": centre_id, "gender": p["gender"], "sport": "Weightlifting",
            })
            if r.status_code == 200:
                ok += 1
                d = r.json()["student"]
                print(f"  enrolled {p['name'][:28]:<30} "
                      f"{d.get('templates',0)} templates  q={d.get('quality')}")
            else:
                fail += 1
                detail = r.json().get("detail", r.text[:90]) if r.text else r.status_code
                print(f"  FAILED   {p['name'][:28]:<30} {detail}")
        print()
        print(f"Enrolled {ok}, failed {fail}")
        json.dump(people, open(workdir / "roster.json", "w"), indent=2, default=str)

    # ----------------------------------------------------------------- test
    if args.test_photo:
        centres = s.get(B + "/centres").json()["centres"]
        match = [c for c in centres if c["code"].upper() == args.centre_code.upper()]
        if not match:
            print(f"Centre {args.centre_code} not found.")
            return 1
        centre_id = match[0]["id"]

        print()
        print("=" * 68)
        print("  HELD-OUT TEST - group photo, never used for enrolment")
        print("=" * 68)
        r = s.post(B + "/attendance/process",
                   files={"photo": open(args.test_photo, "rb")},
                   data={"centre_id": centre_id, "date_str": "2026-08-21"},
                   timeout=900)
        if r.status_code != 200:
            print(f"  request failed: {r.status_code} {r.text[:200]}")
            return 1
        d = r.json()
        roster = s.get(B + "/people", params={"centre_id": centre_id}).json()["people"]
        print(f"  roster at centre : {len(roster)}")
        print(f"  faces detected   : {d['faces_detected']}")
        print(f"  recognised       : {d['recognized_count']}"
              f"  ({d['athletes_present']} athletes, {d['coaches_present']} coaches)")
        print(f"  unknown          : {d['unknown_count']}")
        print(f"  latency          : {d['timings']['total_ms']/1000:.1f}s")
        print()
        for x in sorted(d["recognized"], key=lambda y: -y["raw_similarity"]):
            print(f"    {x['raw_similarity']:.3f}  {x['name'][:34]:<36}{x['roll_no']}")
        print()
        recall = d["recognized_count"] / len(roster) if roster else 0
        print(f"  recall against roster: {recall*100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
