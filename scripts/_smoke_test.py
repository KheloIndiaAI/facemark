"""Quick pipeline smoke test: detector fusion + ensemble embeddings + fuse_scores."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np

from backend.detector import get_detector
from backend.recognizer import fuse_scores, get_recognizer
from backend import database


def main():
    database.init_db()
    det = get_detector()
    print("detector:", det.backend_label, "| mode:", det.mode, flush=True)
    rec = get_recognizer()
    print("ensemble:", rec.label, flush=True)

    img = (np.random.rand(720, 1280, 3) * 255).astype("uint8")

    t = time.perf_counter()
    faces_fused = det.detect(img)
    print(f"fused detect: {len(faces_fused)} faces, {(time.perf_counter()-t)*1000:.0f} ms", flush=True)

    t = time.perf_counter()
    faces_fast = det.detect(img, mode="fast")
    print(f"fast detect: {len(faces_fast)} faces, {(time.perf_counter()-t)*1000:.0f} ms", flush=True)

    q = rec.embed_faces(img, faces_fused[:2])
    print("embedded:", {k: v.shape for k, v in q.items()}, flush=True)

    f, ids = fuse_scores(q, {}, {m.name: m.weight for m in rec.models})
    print("empty-gallery fuse ->", f, ids, flush=True)
    print("SMOKE OK", flush=True)


if __name__ == "__main__":
    main()
