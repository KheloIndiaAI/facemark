"""Backend sanity: imports, DB migration, gallery load, ensemble + enhancer boot."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

t0 = time.time()
from backend import database
database.init_db()
gallery = database.load_gallery()
print(f"[ok] DB migrated in {time.time()-t0:.1f}s")
for model, (tids, sids, mat) in gallery.items():
    print(f"  gallery[{model}]: {len(tids)} templates, {len(set(sids.tolist()))} students")

from backend.detector import get_detector
from backend.recognizer import get_recognizer, fuse_scores
from backend.enhancer import get_enhancer, FFHQ_LANDMARKS
from backend.detector import Face

t1 = time.time()
det = get_detector()
rec = get_recognizer()
enh = get_enhancer()
print(f"[ok] models loaded in {time.time()-t1:.1f}s")
print(f"  ensemble: {[m.name for m in rec.models]}")
print(f"  restoration={enh.restoration_enabled} age={enh.age_enabled}")

# embed one real photo and match against gallery
import cv2
img = cv2.imread(str(Path(__file__).parent.parent / "data/students/enroll_ADHAYANSH-001.png"))
faces = det.detect(img, mode="accurate")
f = max(faces, key=lambda x: x.width * x.height)
t2 = time.time()
queries = rec.embed_faces(img, [f])
fused, ids = fuse_scores(queries, gallery, {m.name: m.weight for m in rec.models})
dt = time.time() - t2
best = int(fused[0].argmax())
print(f"[ok] embed+match in {dt*1000:.0f}ms -> best student_id={ids[best]} sim={fused[0, best]:.4f}")
print(f"  query age est: {enh.estimate_age(img, f.landmarks)}")
