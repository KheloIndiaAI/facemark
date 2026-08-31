"""Re-enroll all existing students with the upgraded embedding pipeline.

This re-computes embeddings for every student using:
  - Full affine alignment (6-DOF)
  - Flip-TTA (horizontal flip averaging)
  - ResNet-50 + iResNet-100 ensemble

The embeddings are cleanly updated in SQLite.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cv2
import numpy as np
from backend import config, database
from backend.detector import FaceDetector
from backend.recognizer import EnsembleRecognizer

def main():
    database.init_db()
    detector = FaceDetector()
    recognizer = EnsembleRecognizer()
    
    students = database.list_students()
    print(f"Re-enrolling {len(students)} students with upgraded embedding pipeline...")
    
    success = 0
    failed = 0
    
    for s in students:
        sid = s["id"]
        name = s["name"]
        photo_path = s["photo_path"]
        
        if not os.path.exists(photo_path):
            print(f"  SKIP {name} (id={sid}): photo not found at {photo_path}")
            failed += 1
            continue
        
        img = cv2.imread(photo_path)
        if img is None:
            print(f"  SKIP {name} (id={sid}): could not read image")
            failed += 1
            continue
        
        faces = detector.detect(img)
        if not faces:
            print(f"  SKIP {name} (id={sid}): no face detected")
            failed += 1
            continue
        
        face = max(faces, key=lambda f: f.width * f.height)
        new_embeddings = recognizer.embed_single(img, face)
        
        with database.connect() as conn:
            for model_name, vec in new_embeddings.items():
                conn.execute(
                    "INSERT INTO embeddings (student_id, model, vector, source) "
                    "VALUES (?, ?, ?, 'enrollment') "
                    "ON CONFLICT(student_id, model) DO UPDATE SET vector = excluded.vector",
                    (sid, model_name, vec.astype(np.float32).tobytes()),
                )
        
        print(f"  OK {name} (id={sid}): re-enrolled with {len(new_embeddings)} models")
        success += 1
    
    print(f"\nDone: {success} re-enrolled, {failed} failed")

if __name__ == "__main__":
    main()
