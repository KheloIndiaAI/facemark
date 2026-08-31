import sys, cv2, numpy as np
from pathlib import Path
ROOT = Path(r'c:\Users\Jatin Singh\Downloads\Attendance face recog')
sys.path.insert(0, str(ROOT))
from backend import database, config
from backend.detector import get_detector
from backend.recognizer import get_recognizer

det = get_detector()
rec = get_recognizer()

students = database.list_students()
print(f'Caching embeddings for {len(students)} students...')

enrolled_vecs = {}
for s in students:
    p_path = s.get('photo_path')
    if not p_path or not Path(p_path).exists(): continue
    img = cv2.imread(p_path)
    if img is None: continue
    faces = det.detect(img)
    if not faces: continue
    f = max(faces, key=lambda x: x.width * x.height)
    embs = rec.embed_single(img, f)
    enrolled_vecs[s['name']] = embs

upload_dir = ROOT / 'data' / 'uploads'
crop_files = sorted(list(upload_dir.glob('face_20260819_180459_757_*.jpg')), key=lambda p: int(p.stem.split('_')[-1]))

print(f'\nComparing {len(crop_files)} group photo crops against all enrolled students:')
for cf in crop_files:
    cimg = cv2.imread(str(cf))
    faces = det.detect(cimg)
    if not faces: continue
    f = max(faces, key=lambda x: x.width * x.height)
    crop_embs = rec.embed_single(cimg, f)
    
    # Compare with all enrolled
    scores = []
    for name, embs in enrolled_vecs.items():
        sim = sum(config.RECOGNIZER_ENSEMBLE[k] * float(np.dot(crop_embs[k][0], embs[k][0])) for k in config.RECOGNIZER_ENSEMBLE)
        scores.append((name, sim))
    
    scores.sort(key=lambda x: x[1], reverse=True)
    top1_name, top1_score = scores[0]
    top2_name, top2_score = scores[1]
    print(f'{cf.name}:')
    print(f'  1st: {top1_name} -> {top1_score:.4f}')
    print(f'  2nd: {top2_name} -> {top2_score:.4f}')
