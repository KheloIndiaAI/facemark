"""Comprehensive Accuracy and Latency Evaluation Benchmark for FaceMark.

Tests YOLO11s detection modes (fast, fused, accurate) and ArcFace ensemble
recognition against test ground truth generated in samples/manifest.json.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# Configure UTF-8 output on Windows terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add root directory to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config, database, utils
from backend.detector import get_detector
from backend.recognizer import fuse_scores, get_recognizer

logging.basicConfig(level=logging.WARNING)


def evaluate():
    print("\n" + "=" * 65)
    print("  FaceMark Accuracy & Latency Evaluation")
    print("=" * 65)

    manifest_path = ROOT / "samples" / "manifest.json"
    if not manifest_path.exists():
        print(f"\n[!] Manifest not found at {manifest_path}")
        print("    Running generate_samples.py first...\n")
        from generate_samples import generate_samples
        generate_samples()

    if not manifest_path.exists():
        print("[ERROR] Failed to find or generate manifest.json.")
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # Initialize components
    database.init_db()
    detector = get_detector()
    recognizer = get_recognizer()

    print(f"[*] Detector Backend   : {detector.backend_label}")
    print(f"[*] Recognizer Ensemble: {recognizer.label}")
    print(f"[*] Match Threshold    : {config.MATCH_THRESHOLD}")
    print(f"[*] Enrolling {len(manifest['students'])} students from test dataset...")

    # Clear existing data for a clean test run
    with database.connect() as conn:
        conn.execute("DELETE FROM attendance")
        conn.execute("DELETE FROM embeddings")
        conn.execute("DELETE FROM students")

    enrolled_count = 0
    id_to_roll = {}
    roll_to_name = {}

    for s in manifest["students"]:
        img_path = ROOT / s["enrollment"]
        if not img_path.exists():
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            continue

        faces = detector.detect(img, mode="fused")
        if not faces:
            # Fallback with lower threshold for tough enrollment crops
            faces = detector.detect(img, mode="accurate")
        if not faces:
            continue

        face = max(faces, key=lambda f: f.width * f.height)
        embs = recognizer.embed_single(img, face)
        sid = database.add_student(s["name"], s["id"], str(img_path), embs)
        id_to_roll[sid] = s["id"]
        roll_to_name[s["id"]] = s["name"]
        enrolled_count += 1

    print(f"[+] Successfully enrolled {enrolled_count} students into SQLite gallery.")
    gallery = database.load_gallery()

    modes = ["fast", "fused", "accurate"]
    detection_stats = {m: {"detected": 0, "expected": 0, "detect_time": 0.0, "embed_time": 0.0} for m in modes}
    recognition_results = []

    group_photos = manifest.get("group_photos", [])
    if not group_photos:
        print("[!] No group photos found in manifest.")
        return

    print(f"[*] Benchmarking {len(group_photos)} group photos across {modes} modes...\n")

    weights = {m.name: m.weight for m in recognizer.models}

    for group in group_photos:
        img_path = ROOT / group["file"]
        if not img_path.exists():
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            continue

        expected_ids = set(group["expected_ids"])

        for mode in modes:
            t0 = time.perf_counter()
            faces = detector.detect(img, mode=mode)
            t1 = time.perf_counter()

            queries = recognizer.embed_faces(img, faces)
            fused, gallery_ids = fuse_scores(queries, gallery, weights)
            t2 = time.perf_counter()

            detection_stats[mode]["expected"] += len(expected_ids)
            detection_stats[mode]["detected"] += len(faces)
            detection_stats[mode]["detect_time"] += (t1 - t0)
            detection_stats[mode]["embed_time"] += (t2 - t1)

            # Evaluate recognition on fused mode
            if mode == "fused":
                detected_rolls = set()
                if fused is not None:
                    for i in range(len(faces)):
                        best_col = int(np.argmax(fused[i]))
                        sim = float(fused[i][best_col])
                        sid = int(gallery_ids[best_col])
                        if sim >= config.MATCH_THRESHOLD and sid in id_to_roll:
                            detected_rolls.add(id_to_roll[sid])

                tp = len(expected_ids.intersection(detected_rolls))
                fp = len(detected_rolls - expected_ids)
                fn = len(expected_ids - detected_rolls)
                acc = (tp / len(expected_ids) * 100) if len(expected_ids) > 0 else 0.0

                recognition_results.append({
                    "photo": Path(group["file"]).name,
                    "expected": len(expected_ids),
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "acc": acc,
                })

    n_images = len(group_photos)

    # 1. Detection Results
    print("┌" + "─" * 48 + "┐")
    print(f"│ {'Detection Benchmark':<46} │")
    print("├──────────┬──────────┬────────────┬─────────────┤")
    print(f"│ {'Mode':<8} │ {'Detected':<8} │ {'Expected':<10} │ {'Recall':<11} │")
    print("├──────────┼──────────┼────────────┼─────────────┤")
    for mode in modes:
        det = detection_stats[mode]
        recall = (det["detected"] / det["expected"] * 100) if det["expected"] > 0 else 0.0
        print(f"│ {mode:<8} │ {det['detected']:<8} │ {det['expected']:<10} │ {recall:>9.1f}%  │")
    print("└──────────┴──────────┴────────────┴─────────────┘\n")

    # 2. Recognition Results (Fused)
    print("┌" + "─" * 58 + "┐")
    print(f"│ {'Recognition Accuracy (Fused Mode)':<56} │")
    print("├──────────────────┬─────┬────┬────┬────┬──────────────┤")
    print(f"│ {'Photo':<16} │ {'Exp':<3} │ {'TP':<2} │ {'FP':<2} │ {'FN':<2} │ {'Accuracy':<12} │")
    print("├──────────────────┼─────┼────┼────┼────┼──────────────┤")
    total_exp = total_tp = total_fp = total_fn = 0
    for res in recognition_results:
        total_exp += res["expected"]
        total_tp += res["tp"]
        total_fp += res["fp"]
        total_fn += res["fn"]
        print(f"│ {res['photo']:<16} │ {res['expected']:<3} │ {res['tp']:<2} │ {res['fp']:<2} │ {res['fn']:<2} │ {res['acc']:>10.1f}%  │")
    print("├──────────────────┼─────┼────┼────┼────┼──────────────┤")
    overall_acc = (total_tp / total_exp * 100) if total_exp > 0 else 0.0
    print(f"│ {'TOTAL / AVERAGE':<16} │ {total_exp:<3} │ {total_tp:<2} │ {total_fp:<2} │ {total_fn:<2} │ {overall_acc:>10.1f}%  │")
    print("└──────────────────┴─────┴────┴────┴────┴──────────────┘\n")

    # 3. Latency Benchmark
    print("┌" + "─" * 53 + "┐")
    print(f"│ {'Latency Benchmark (Avg per Image)':<51} │")
    print("├──────────┬────────────┬────────────┬──────────────┤")
    print(f"│ {'Mode':<8} │ {'Detect(ms)':<10} │ {'Embed(ms)':<10} │ {'Total(ms)':<12} │")
    print("├──────────┼────────────┼────────────┼──────────────┤")
    for mode in modes:
        det_ms = (detection_stats[mode]["detect_time"] / max(n_images, 1)) * 1000
        emb_ms = (detection_stats[mode]["embed_time"] / max(n_images, 1)) * 1000
        tot_ms = det_ms + emb_ms
        print(f"│ {mode:<8} │ {det_ms:>9.1f}ms │ {emb_ms:>9.1f}ms │ {tot_ms:>10.1f}ms   │")
    print("└──────────┴────────────┴────────────┴──────────────┘\n")

    print(f"  [RESULT] Overall Recognition Accuracy: {overall_acc:.1f}%")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    evaluate()
