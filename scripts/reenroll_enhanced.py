"""Re-enroll ALL students using the enhanced pipeline (CLAHE + Unsharp + Flip-TTA).

This ensures enrollment embeddings and query embeddings use the SAME preprocessing,
which is critical for cross-domain matching (ID card -> group photo).
"""
import sys, os
import cv2
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config, database
from backend.detector import get_detector
from backend.recognizer import get_recognizer

def main():
    database.init_db()
    det = get_detector()
    rec = get_recognizer()
    
    students = database.list_students()
    print(f"Re-enrolling {len(students)} students with enhanced pipeline...\n")
    
    success = 0
    fail = 0
    
    for s in students:
        photo_path = s.get('photo_path', '')
        if not photo_path or not os.path.exists(photo_path):
            print(f"  SKIP {s['name']} ({s['roll_no']}): photo not found at {photo_path}")
            fail += 1
            continue
            
        img = cv2.imread(photo_path)
        if img is None:
            print(f"  SKIP {s['name']} ({s['roll_no']}): could not read image")
            fail += 1
            continue
        
        # Detect face
        faces = det.detect(img, mode='fused')
        if not faces:
            faces = det.detect(img, mode='accurate')
        if not faces:
            print(f"  SKIP {s['name']} ({s['roll_no']}): no face detected")
            fail += 1
            continue
        
        face = max(faces, key=lambda f: f.width * f.height)
        
        # This now uses the enhanced pipeline (CLAHE + Unsharp + Flip-TTA)
        embs = rec.embed_single(img, face)
        
        # Update embeddings in database
        with database.connect() as conn:
            for model_name, vec in embs.items():
                # vec shape is (1, 512) from embed_faces
                embedding = vec[0] if vec.ndim > 1 else vec
                conn.execute(
                    "INSERT INTO embeddings (student_id, model, vector, source) "
                    "VALUES (?, ?, ?, 'enrollment') "
                    "ON CONFLICT(student_id, model) DO UPDATE SET vector = excluded.vector",
                    (s['id'], model_name, embedding.astype(np.float32).tobytes()),
                )
        
        success += 1
        print(f"  OK  {s['name']} ({s['roll_no']}) - re-enrolled with enhanced embeddings")
    
    print(f"\nDone! {success} success, {fail} failed/skipped")

if __name__ == "__main__":
    main()
