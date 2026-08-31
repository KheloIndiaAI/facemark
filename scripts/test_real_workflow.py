"""Proper end-to-end test: enroll faces cropped from a group photo, then recognize them.

This mimics the real use case:
  1. Crop individual portraits from a group photo
  2. Enroll each person into the database
  3. Re-process the same (or different) group photo → all should be recognized
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cv2
import numpy as np
from backend import config, database
from backend.detector import FaceDetector
from backend.recognizer import EnsembleRecognizer, fuse_scores

def main():
    database.init_db()
    
    # Clear ALL old fake students
    with database.connect() as conn:
        conn.execute("DELETE FROM attendance")
        conn.execute("DELETE FROM embeddings")
        conn.execute("DELETE FROM students")
    print("Cleared all old students from database.\n")
    
    det = FaceDetector()
    rec = EnsembleRecognizer()
    
    # Find the real group photo the user uploaded
    upload_dir = config.UPLOADS_DIR
    group_photo = None
    
    # Check for the CS-2024 group photo in common locations
    candidates = [
        config.ROOT_DIR / "samples" / "test_suite" / "1_full_group_5_students.jpg",
    ]
    # Also check uploads directory for recently uploaded photos
    if upload_dir.exists():
        for f in sorted(upload_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() in ('.jpg', '.jpeg', '.png') and f.name.startswith(('annotated_', 'face_')):
                continue
            candidates.insert(0, f)
    
    # Use the test suite group photo for now
    group_photo = config.ROOT_DIR / "samples" / "test_suite" / "1_full_group_5_students.jpg"
    if not group_photo.exists():
        print("No group photo found. Please place a group photo in samples/test_suite/")
        return
    
    print(f"Using group photo: {group_photo}")
    img = cv2.imread(str(group_photo))
    if img is None:
        print("Could not read group photo")
        return
    
    # Step 1: Detect all faces
    faces = det.detect(img, mode="fused")
    print(f"\nStep 1: Detected {len(faces)} faces in group photo")
    
    # Step 2: Crop each face and enroll as a student
    print(f"\nStep 2: Enrolling each face as a student...")
    enrolled = []
    for i, face in enumerate(faces):
        # Crop face with padding (simulates an enrollment portrait)
        x1, y1, x2, y2 = face.box
        w, h = x2 - x1, y2 - y1
        pad = 0.35  # generous padding for portrait-style crop
        px, py = w * pad, h * pad
        cx1 = max(int(x1 - px), 0)
        cy1 = max(int(y1 - py), 0)
        cx2 = min(int(x2 + px), img.shape[1])
        cy2 = min(int(y2 + py), img.shape[0])
        crop = img[cy1:cy2, cx1:cx2]
        
        # Save enrollment photo
        name = f"Student {i+1}"
        roll = f"GRP-2024-{i+1:03d}"
        enroll_path = config.STUDENTS_DIR / f"enroll_{roll}.jpg"
        cv2.imwrite(str(enroll_path), crop)
        
        # Detect face in the crop and embed
        crop_faces = det.detect(crop)
        if crop_faces:
            best = max(crop_faces, key=lambda f: f.width * f.height)
        else:
            best = face  # fallback: use original face
            # Re-embed from original image
        
        embs = rec.embed_single(crop if crop_faces else img, best if crop_faces else face)
        sid = database.add_student(name, roll, str(enroll_path), embs)
        enrolled.append((sid, name, roll))
        print(f"  Enrolled: {name} ({roll}) -> ID #{sid}")
    
    print(f"\nStep 3: Re-processing the SAME group photo for attendance...")
    
    # Step 3: Now process the same group photo - should recognize everyone
    gallery = database.load_gallery()
    weights = {m.name: m.weight for m in rec.models}
    
    faces2 = det.detect(img, mode="fused")
    queries = rec.embed_faces(img, faces2)
    fused, gallery_ids = fuse_scores(queries, gallery, weights)
    
    students = {s['id']: s for s in database.list_students()}
    
    recognized_count = 0
    for i, face in enumerate(faces2):
        best_col = int(np.argmax(fused[i]))
        sim = float(fused[i][best_col])
        sid = int(gallery_ids[best_col])
        sname = students[sid]['name']
        roll = students[sid]['roll_no']
        status = "MATCH" if sim >= config.MATCH_THRESHOLD else "MISS"
        if status == "MATCH":
            recognized_count += 1
        print(f"  Face {i+1}: {sname} ({roll}) sim={sim:.4f} [{status}]")
    
    print(f"\n{'='*50}")
    print(f"  RESULT: {recognized_count}/{len(faces2)} faces recognized")
    print(f"  Accuracy: {recognized_count/max(len(faces2),1)*100:.1f}%")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
