import sys, os
from pathlib import Path
import cv2
import numpy as np
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config, database
from backend.detector import get_detector
from backend.recognizer import get_recognizer

TEST_DIR = ROOT / "samples" / "test_suite"
TEST_DIR.mkdir(parents=True, exist_ok=True)
STUDENTS_DIR = ROOT / "data" / "students"
STUDENTS_DIR.mkdir(parents=True, exist_ok=True)

det = get_detector()
rec = get_recognizer()

people = [
    ("Aarav Sharma", "CS-2024-101", "https://randomuser.me/api/portraits/men/32.jpg"),
    ("Priya Patel", "CS-2024-102", "https://randomuser.me/api/portraits/women/44.jpg"),
    ("Rohan Kumar", "CS-2024-103", "https://randomuser.me/api/portraits/men/46.jpg"),
    ("Ananya Singh", "CS-2024-104", "https://randomuser.me/api/portraits/women/68.jpg"),
    ("Vikram Mehta", "CS-2024-105", "https://randomuser.me/api/portraits/men/75.jpg")
]

members = []
FACE_SIZE = 220  # Optimal size for 720p group photo

for idx, (name, roll, u) in enumerate(people):
    r = requests.get(u, verify=False, timeout=10)
    arr = np.asarray(bytearray(r.content), dtype=np.uint8)
    raw = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img = cv2.resize(raw, (FACE_SIZE, FACE_SIZE), interpolation=cv2.INTER_CUBIC)
    
    indiv_path = TEST_DIR / f"individual_{idx+1}_{name.replace(' ', '_')}.jpg"
    enroll_path = STUDENTS_DIR / f"enroll_{roll}.jpg"
    cv2.imwrite(str(indiv_path), img)
    cv2.imwrite(str(enroll_path), img)
    
    members.append({
        "id_num": idx + 1,
        "name": name,
        "roll": roll,
        "img": img,
        "indiv_path": indiv_path,
        "enroll_path": enroll_path,
    })
    print(f"Saved portrait {idx+1}: {name} ({roll})")

# 1. Composite Full Group (5 students, perfectly non-overlapping)
W, H = 1500, 720
bg_full = np.zeros((H, W, 3), dtype=np.uint8)
for y in range(H):
    alpha = y / H
    bg_full[y, :] = (
        int(235 * (1 - alpha) + 200 * alpha),
        int(240 * (1 - alpha) + 210 * alpha),
        int(245 * (1 - alpha) + 220 * alpha)
    )

# Classroom board
cv2.rectangle(bg_full, (50, 40), (W - 50, 200), (160, 170, 180), -1)
cv2.rectangle(bg_full, (50, 40), (W - 50, 200), (120, 130, 140), 2)

spacing_full = W // 6  # 250px spacing > 220px size = 0 overlap!
for idx, m in enumerate(members):
    f_img = m["img"]
    fh, fw = f_img.shape[:2]
    cx = spacing_full * (idx + 1)
    cy = 430 + (20 if idx % 2 == 1 else -20)
    y1, y2 = cy - fh // 2, cy + fh // 2
    x1, x2 = cx - fw // 2, cx + fw // 2
    bg_full[y1:y2, x1:x2] = f_img

full_path = TEST_DIR / "1_full_group_5_students.jpg"
cv2.imwrite(str(full_path), bg_full, [cv2.IMWRITE_JPEG_QUALITY, 98])
print(f"Saved Full Group: {full_path.name}")

# 2. Composite Subgroup (3 students: 1, 3, 5)
W3, H3 = 1200, 720
bg_sub = np.zeros((H3, W3, 3), dtype=np.uint8)
for y in range(H3):
    alpha = y / H3
    bg_sub[y, :] = (
        int(235 * (1 - alpha) + 200 * alpha),
        int(240 * (1 - alpha) + 210 * alpha),
        int(245 * (1 - alpha) + 220 * alpha)
    )
cv2.rectangle(bg_sub, (50, 40), (W3 - 50, 200), (160, 170, 180), -1)
cv2.rectangle(bg_sub, (50, 40), (W3 - 50, 200), (120, 130, 140), 2)

sub_members = [members[0], members[2], members[4]]
spacing_sub = W3 // 4  # 300px spacing > 220px size = 0 overlap!
for idx, m in enumerate(sub_members):
    f_img = m["img"]
    fh, fw = f_img.shape[:2]
    cx = spacing_sub * (idx + 1)
    cy = 430 + (15 if idx % 2 == 1 else -15)
    y1, y2 = cy - fh // 2, cy + fh // 2
    x1, x2 = cx - fw // 2, cx + fw // 2
    bg_sub[y1:y2, x1:x2] = f_img

sub_path = TEST_DIR / "2_subgroup_3_students.jpg"
cv2.imwrite(str(sub_path), bg_sub, [cv2.IMWRITE_JPEG_QUALITY, 98])
print(f"Saved Subgroup of 3: {sub_path.name}")

# 3. Cleanly update database enrollments with ON CONFLICT UPDATE
database.init_db()
for m in members:
    faces = det.detect(m["img"], mode="fused")
    if not faces:
        faces = det.detect(m["img"], mode="accurate")
    if faces:
        face = max(faces, key=lambda f: f.width * f.height)
        embs = rec.embed_single(m["img"], face)
        with database.connect() as conn:
            row = conn.execute("SELECT id FROM students WHERE roll_no = ?", (m["roll"],)).fetchone()
            if row:
                sid = row[0]
                conn.execute("UPDATE students SET name = ?, photo_path = ? WHERE id = ?", (m["name"], str(m["enroll_path"]), sid))
            else:
                cur = conn.execute("INSERT INTO students (name, roll_no, photo_path, created_at) VALUES (?,?,?,datetime('now'))", (m["name"], m["roll"], str(m["enroll_path"])))
                sid = cur.lastrowid
            
            for model_name, vec in embs.items():
                conn.execute(
                    "INSERT INTO embeddings (student_id, model, vector, source) "
                    "VALUES (?, ?, ?, 'enrollment') "
                    "ON CONFLICT(student_id, model) DO UPDATE SET vector = excluded.vector",
                    (sid, model_name, vec.astype(np.float32).tobytes()),
                )
        print(f"Updated DB Enrollment: {m['name']} ({m['roll']}) -> ID #{sid}")

print("\nReady!")
