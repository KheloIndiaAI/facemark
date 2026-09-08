"""Detection-mode and latency benchmark: what each mode finds, and what it costs.

This is the question `scripts/evaluate.py` does not answer. That harness reports
FAR/FRR/EER/rank-1 - whether recognition is good enough - and says nothing about
how long a capture takes or what the three detector modes buy you. Deciding
whether `accurate` is affordable on centre hardware needs numbers, and this
prints them.

    python -m scripts.benchmark_detection
    python -m scripts.benchmark_detection --repeat 5
    python -m scripts.benchmark_detection --labels my.json

Ground truth is `data/eval_labels.json`, whose `expected_faces` is a human count
of the faces actually in each photo - so "found vs expected" here means what it
says. Recognition accuracy is deliberately NOT reported: evaluate.py measures it
properly and a second, weaker number competing with it would only get quoted.

READ-ONLY. It opens the database to read the gallery and writes nothing.

MEASURED, 2026-09-08, on the three labelled photos
--------------------------------------------------
All three modes found the same 41 of 42 faces at the same cost - the knob
buys nothing on this corpus. It is not broken: the scores really are 0.85 /
0.80 / 0.70, and the one missed face only appears at 0.30, by which point
0.15 is already inventing a seventeenth face that is not there. So the
missed face is not reachable by loosening the threshold, and `accurate`
being free here means there is no latency argument for `fast`.

    This file replaces evaluate_accuracy.py, which had not run since the
    Postgres migration and could not be repaired in place. It opened by
    executing DELETE FROM attendance / embeddings / students against whatever
    DATABASE_URL pointed at, to enrol 17 fake students downloaded from
    randomuser.me. It survived only because `embeddings` no longer exists and
    the failing statement rolled the transaction back - correcting that one
    table name would have destroyed the live register. Its accuracy table also
    duplicated evaluate.py with a weaker metric (nearest neighbour above
    threshold, not the Hungarian assignment the app actually performs), and its
    header described YOLO11s and ArcFace, which this project has never used.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import cv2

# Configure UTF-8 output on Windows terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import database  # noqa: E402
from backend.detector import get_detector  # noqa: E402
from backend.recognizer import get_recognizer  # noqa: E402

MODES = ("fast", "fused", "accurate")


def load_photos(labels_path: Path):
    """[(path, expected_faces or None)], from the labels file."""
    if not labels_path.exists():
        return []
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    out = []
    for rel, spec in labels.items():
        p = ROOT / rel
        if p.exists():
            out.append((p, spec.get("expected_faces")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", default="data/eval_labels.json",
                    help="ground-truth file (default: data/eval_labels.json)")
    ap.add_argument("--repeat", type=int, default=3,
                    help="timed passes per photo; the median is reported (default: 3)")
    ap.add_argument("--modes", default=",".join(MODES),
                    help=f"comma-separated subset of {','.join(MODES)}")
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in MODES]
    if bad:
        print(f"[ERROR] Unknown mode(s): {', '.join(bad)}. Choose from {', '.join(MODES)}.")
        return 2

    photos = load_photos(ROOT / args.labels)
    if not photos:
        print(f"[ERROR] No usable photos from {args.labels}.")
        print("        Each key must be a path relative to the project root that exists.")
        return 1

    detector, recognizer = get_detector(), get_recognizer()

    # The gallery is loaded because embedding cost is only interesting next to
    # the gallery it will be compared against - and to fail loudly here rather
    # than report timings for a system that could not recognise anybody.
    gallery = database.load_gallery()
    n_templates = sum(len(v[1]) for v in gallery.values())
    n_people = len({int(x) for v in gallery.values() for x in v[1]})

    print("\n" + "=" * 66)
    print("  FaceMark detection & latency benchmark")
    print("=" * 66)
    print(f"[*] Detector    : {detector.backend_label}")
    print(f"[*] Recogniser  : {recognizer.label}")
    print(f"[*] Gallery     : {n_templates} templates, {n_people} people (read-only)")
    print(f"[*] Photos      : {len(photos)} from {args.labels}")
    print(f"[*] Passes      : {args.repeat} per photo per mode, median reported\n")

    if not n_templates:
        print("[!] The gallery is empty, so embedding timings are still valid but")
        print("    nothing would be recognised. Enrol someone first for a real picture.\n")

    # A first detect() call pays for model construction and any lazy import.
    # Timing that once and calling it the cost of a capture would overstate
    # every mode, so it is spent before the clock starts.
    warm = cv2.imread(str(photos[0][0]))
    if warm is not None:
        detector.detect(warm, mode=modes[0])
        recognizer.embed_faces(warm, detector.detect(warm, mode=modes[0])[:1])

    stats = {m: {"found": 0, "expected": 0, "detect": [], "embed": [], "photos": 0}
             for m in modes}
    per_photo = []

    for path, expected in photos:
        img = cv2.imread(str(path))
        if img is None:
            print(f"[!] Could not read {path.name}, skipping.")
            continue
        row = {"photo": path.name, "expected": expected}
        for mode in modes:
            det_times, emb_times, found = [], [], 0
            for _ in range(max(1, args.repeat)):
                t0 = time.perf_counter()
                faces = detector.detect(img, mode=mode)
                t1 = time.perf_counter()
                recognizer.embed_faces(img, faces)
                t2 = time.perf_counter()
                det_times.append((t1 - t0) * 1000)
                emb_times.append((t2 - t1) * 1000)
                found = len(faces)
            s = stats[mode]
            s["found"] += found
            s["expected"] += expected or 0
            s["photos"] += 1
            s["detect"].append(statistics.median(det_times))
            s["embed"].append(statistics.median(emb_times))
            row[mode] = found
        per_photo.append(row)

    # ---------------------------------------------------------------- output
    def table(title, headers, widths, rows):
        """One aligned table. Widths drive the rules, so adding a mode cannot
        leave the borders one character out."""
        def rule(l, m, r):
            return l + m.join("\u2500" * (w + 2) for w in widths) + r

        def line(cells):
            return "\u2502 " + " \u2502 ".join(
                str(c)[:w].ljust(w) if a == "<" else str(c)[:w].rjust(w)
                for c, w, a in zip(cells, widths, aligns)
            ) + " \u2502"

        aligns = [h[0] for h in headers]
        names = [h[1] for h in headers]
        inner = sum(w + 3 for w in widths) - 1
        print("\u250c" + "\u2500" * inner + "\u2510")
        print("\u2502 " + title[:inner - 2].ljust(inner - 2) + " \u2502")
        print(rule("\u251c", "\u252c", "\u2524"))
        print(line(names))
        print(rule("\u251c", "\u253c", "\u2524"))
        for r in rows:
            print(line(r))
        print(rule("\u2514", "\u2534", "\u2518"))
        print()

    table(
        "Faces found per photo",
        [("<", "Photo"), (">", "Expected")] + [(">", m) for m in modes],
        [30, 8] + [8] * len(modes),
        [[r["photo"], "?" if r["expected"] is None else r["expected"]]
         + [r.get(m, 0) for m in modes] for r in per_photo],
    )

    truth_rows = []
    for mode in modes:
        st = stats[mode]
        if st["expected"]:
            pct = st["found"] / st["expected"] * 100
            # Over 100% means extra boxes, not better recall - say which way it
            # went rather than printing a number that reads like a score.
            note = f"{pct:.1f}%  " + ("(extra boxes)" if st["found"] > st["expected"]
                                      else "(missed faces)" if st["found"] < st["expected"]
                                      else "(exact)")
        else:
            note = "no ground truth"
        truth_rows.append([mode, st["found"], st["expected"], note])
    table("Detection against ground truth",
          [("<", "Mode"), (">", "Found"), (">", "Expected"), ("<", "Found / expected")],
          [10, 8, 9, 24], truth_rows)

    lat_rows = []
    for mode in modes:
        st = stats[mode]
        d = statistics.median(st["detect"]) if st["detect"] else 0.0
        e = statistics.median(st["embed"]) if st["embed"] else 0.0
        lat_rows.append([mode, f"{d:.1f}ms", f"{e:.1f}ms", f"{d + e:.1f}ms"])
    table("Latency per photo (median of medians)",
          [("<", "Mode"), (">", "Detect"), (">", "Embed"), (">", "Total")],
          [10, 10, 10, 11], lat_rows)

    print("  Recognition accuracy is not measured here on purpose.")
    print("  Run `python -m scripts.evaluate` for FAR/FRR/EER and rank-1.")
    print("=" * 66 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
