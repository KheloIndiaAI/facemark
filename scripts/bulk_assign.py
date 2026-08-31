"""Apply a batch of confirmed face-to-athlete identifications in one pass.

Identifying the athlete is a judgement only someone who knows them can make, so
this tool does not guess. You supply the mapping; it marks each athlete present
and stores their face from the full-resolution original as a `live` template
captured in the centre's real conditions.

Mapping format - one per line, `face_index = roll_no` or `face_index = name`.
Lines starting with # are ignored, and a face you are unsure about is simply
left out:

    1  = WEAA052F15
    2  = S.Veda Sanjay
    4  = WEAA024M14
    # 5 unsure, skipped

    python -m scripts.bulk_assign --photo <path> --centre KISCE-WL \
        --map mapping.txt --password <pw> --dry-run
    python -m scripts.bulk_assign ... --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("REQUESTS_CA_BUNDLE", None)
os.environ.pop("CURL_CA_BUNDLE", None)


def parse_mapping(path: Path) -> dict:
    out = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s*[=:]\s*(.+)$", line)
        if not m:
            print(f"  line {lineno}: could not read {raw!r}, skipped")
            continue
        out[int(m.group(1))] = m.group(2).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", required=True)
    ap.add_argument("--centre", required=True, help="centre code, e.g. KISCE-WL")
    ap.add_argument("--map", required=True, help="file of 'face_index = roll_no' lines")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--date")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-learn", action="store_true",
                    help="mark present without storing a template")
    args = ap.parse_args()

    import requests
    B = args.base.rstrip("/") + "/api"
    s = requests.Session()
    if s.post(B + "/auth/login",
              data={"username": args.username, "password": args.password}).status_code != 200:
        print("Login failed.")
        return 1

    centres = s.get(B + "/centres").json()["centres"]
    match = [c for c in centres if c["code"].upper() == args.centre.upper()]
    if not match:
        print(f"Centre {args.centre} not found.")
        return 1
    centre_id = match[0]["id"]

    people = s.get(B + "/people", params={"centre_id": centre_id}).json()["people"]
    by_roll = {p["roll_no"].upper(): p for p in people}
    by_name = {p["name"].lower(): p for p in people}

    mapping = parse_mapping(Path(args.map))
    if not mapping:
        print("Mapping file produced no entries.")
        return 1

    print(f"Processing {Path(args.photo).name} for {match[0]['name']} ...")
    data = {"centre_id": centre_id}
    if args.date:
        data["date_str"] = args.date
    r = s.post(B + "/attendance/process",
               files={"photo": open(args.photo, "rb")}, data=data, timeout=900)
    if r.status_code != 200:
        print(f"  processing failed: {r.status_code} {r.text[:200]}")
        return 1
    res = r.json()
    print(f"  {res['faces_detected']} faces, {res['recognized_count']} matched automatically")

    # face_index on each unknown refers to the position in the detection order.
    by_index = {u["face_index"]: u["face_url"] for u in res["unknown"]}
    already = {x["name"] for x in res["recognized"]}

    plan, problems = [], []
    for idx, who in sorted(mapping.items()):
        person = by_roll.get(who.upper()) or by_name.get(who.lower())
        if person is None:
            problems.append(f"face {idx}: no athlete matches {who!r}")
            continue
        if idx not in by_index:
            problems.append(f"face {idx}: not an unmatched face in this photo "
                            f"(it may already be recognised)")
            continue
        if person["name"] in already:
            problems.append(f"face {idx}: {person['name']} is already marked from another face")
            continue
        plan.append((idx, by_index[idx], person))

    print()
    for p in problems:
        print(f"  SKIP  {p}")
    print(f"  {len(plan)} assignment(s) ready")
    for idx, _, person in plan:
        print(f"    face {idx:>3} -> {person['name']} ({person['roll_no']})")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write these.")
        return 0

    print()
    ok = fail = 0
    for idx, face_url, person in plan:
        d = {"face_url": face_url, "student_id": person["id"],
             "learn": "false" if args.no_learn else "true"}
        if args.date:
            d["date_str"] = args.date
        rr = s.post(B + "/attendance/assign", data=d, timeout=300)
        if rr.status_code == 200:
            ok += 1
            print(f"  face {idx:>3}  {rr.json()['message']}")
        else:
            fail += 1
            print(f"  face {idx:>3}  FAILED {rr.status_code} {rr.text[:100]}")
    print()
    print(f"Applied {ok}, failed {fail}. Re-run the photo to confirm they now match on their own.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
