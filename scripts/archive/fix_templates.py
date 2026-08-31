"""Fix all students missing adaface_ir101.onnx templates."""
import cv2
import numpy as np
from collections import defaultdict
from backend import database, config
from backend.detector import get_detector, Face, estimate_landmarks
from backend.recognizer import get_recognizer
from backend.enhancer import get_enhancer, FFHQ_LANDMARKS, sharpness_quality
import os

def main():
    conn = database.connect()

    rows = conn.execute('SELECT DISTINCT student_id, model FROM templates').fetchall()
    student_models = defaultdict(set)
    for r in rows:
        student_models[r['student_id']].add(r['model'])

    required_models = set(config.RECOGNIZER_ENSEMBLE.keys())
    students_to_fix = []
    for sid, models in student_models.items():
        missing = required_models - models
        if missing:
            st = database.get_student(sid)
            if st and st.get('photo_path') and os.path.exists(st['photo_path']):
                students_to_fix.append((sid, st['name'], st['photo_path'], missing))

    if not students_to_fix:
        print("All students have full model coverage. Nothing to fix.")
        return

    print("Found " + str(len(students_to_fix)) + " students missing templates. Re-enrolling...")

    detector = get_detector()
    recognizer = get_recognizer()
    enhancer = get_enhancer()

    fixed = 0
    failed = 0
    for sid, name, photo_path, missing_models in students_to_fix:
        try:
            img = cv2.imread(photo_path)
            if img is None:
                print("  SKIP " + name + ": cannot read photo")
                failed += 1
                continue

            faces = detector.detect(img, mode="accurate")
            if not faces:
                print("  SKIP " + name + ": no face detected")
                failed += 1
                continue

            face = max(faces, key=lambda f: f.width * f.height)
            if face.landmarks is None:
                face.landmarks = estimate_landmarks(face.box)

            raw_emb = recognizer.embed_faces(img, [face])
            
            x1, y1, x2, y2 = [int(v) for v in face.box]
            face_crop = img[max(0,y1):y2, max(0,x1):x2]
            if face_crop.size > 0:
                face_crop = cv2.resize(face_crop, (112, 112))
            else:
                face_crop = img
            quality = sharpness_quality(face_crop)

            templates = []
            for model_name, vec_arr in raw_emb.items():
                if len(vec_arr):
                    templates.append({
                        "model": model_name,
                        "vector": vec_arr[0],
                        "source": "legacy",
                        "quality": quality,
                    })

            if enhancer.restoration_enabled:
                restored = enhancer.restore(img, face.landmarks)
                if restored is not None:
                    r_face = Face(box=(0, 0, 512, 512), conf=1.0, landmarks=FFHQ_LANDMARKS, source="gfpgan")
                    r_emb = recognizer.embed_faces(restored, [r_face])
                    r_quality = sharpness_quality(restored)
                    for model_name, vec_arr in r_emb.items():
                        if len(vec_arr):
                            templates.append({
                                "model": model_name,
                                "vector": vec_arr[0],
                                "source": "restored",
                                "quality": r_quality,
                            })

            if not templates:
                print("  SKIP " + name + ": no templates generated")
                failed += 1
                continue

            conn.execute(
                "DELETE FROM templates WHERE student_id = ? AND source IN ('legacy', 'restored')",
                (sid,)
            )
            conn.commit()

            database.add_templates(sid, templates)
            model_names = sorted(set(t['model'] for t in templates))
            print("  OK " + name + ": " + str(len(templates)) + " templates (" + ", ".join(model_names) + ")")
            fixed += 1

        except Exception as e:
            print("  ERROR " + name + ": " + str(e))
            failed += 1

    print("")
    print("Done. Fixed: " + str(fixed) + ", Failed: " + str(failed))


if __name__ == "__main__":
    main()
