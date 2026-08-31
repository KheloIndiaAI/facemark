"""End-to-end smoke test: start server, test health, enroll with restoration, match.

Runs as a subprocess test - does NOT use pytest or the running server.
"""
import sys, time, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2, numpy as np, requests

BASE = "http://127.0.0.1:8000"
STUDENTS = Path(__file__).parent.parent / "data" / "students"


def wait_for_server(max_wait: float = 180.0):
    """Poll /api/health until the server (and all models) are up."""
    import requests as rq
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            r = rq.get(f"{BASE}/api/health", timeout=5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError(f"Server did not come up within {max_wait}s")

def test_health():
    r = requests.get(f"{BASE}/api/health", timeout=120)
    assert r.status_code == 200, f"health {r.status_code}: {r.text[:200]}"
    d = r.json()
    print(f"  health: {d['status']} restoration={d.get('restoration')} age={d.get('age_aware')} students={d['students']}")
    assert d["status"] == "ok"
    return d


def test_enroll(name, roll, photo_path, live_path=None):
    files = {"photo": open(photo_path, "rb")}
    if live_path and Path(live_path).exists():
        files["live_photo"] = open(live_path, "rb")
    r = requests.post(
        f"{BASE}/api/students",
        data={"name": name, "roll_no": roll},
        files=files, timeout=120,
    )
    for f in files.values():
        f.close()
    assert r.status_code == 200, f"enroll {r.status_code}: {r.text[:300]}"
    d = r.json()
    s = d["student"]
    print(f"  enrolled {s['name']}: templates={s.get('templates')} quality={s.get('quality')} age={s.get('est_age')} restored={s.get('restored')}")
    return s


def test_match(photo_path):
    with open(photo_path, "rb") as f:
        r = requests.post(
            f"{BASE}/api/attendance/process",
            files={"photo": f}, timeout=300,
        )
    assert r.status_code == 200, f"match {r.status_code}: {r.text[:300]}"
    d = r.json()
    t = d["timings"]
    print(
        f"  match: {d['faces_detected']} faces, {d['recognized_count']} recognized, "
        f"{d['unknown_count']} unknown, age_aware={d.get('age_aware')}\n"
        f"    detect={t['detect_ms']}ms embed={t['embed_ms']}ms "
        f"cascade={t.get('cascade_ms', 0)}ms total={t['total_ms']}ms"
    )
    for r_rec in d.get("recognized", []):
        print(f"    -> {r_rec['name']} ({r_rec['roll_no']}) sim={r_rec['raw_similarity']} conf={r_rec['similarity']} cascade={r_rec.get('cascade')}")
    return d


if __name__ == "__main__":
    print("=== E2E Smoke Test ===")
    print("\n0. Waiting for server...")
    wait_for_server()
    print("\n1. Health check")
    h = test_health()

    # 2. Delete the old test student if present, then re-enroll
    import sqlite3
    from backend import config, database
    database.init_db()
    with database.connect() as conn:
        conn.execute("DELETE FROM students WHERE roll_no = 'SMOKE-TEST'")

    photos = sorted(STUDENTS.glob("enroll_*.png"))
    if not photos:
        print("  SKIP: no enrollment photos"); sys.exit(0)

    print("\n2. Enrollment (ID photo + live photo = same for test)")
    s = test_enroll("Smoke Test User", "SMOKE-TEST", photos[0], photos[0])
    assert s["templates"] >= 2, f"expected >=2 templates, got {s['templates']}"

    print("\n3. Match (re-detect from same photo)")
    d = test_match(photos[0])
    assert d["recognized_count"] >= 1, "should recognize at least the enrolled student"

    print("\n=== ALL TESTS PASSED ===")
