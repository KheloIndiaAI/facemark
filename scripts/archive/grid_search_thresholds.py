import cv2, numpy as np
from backend import database, config
from backend.detector import get_detector
from backend.recognizer import get_recognizer, pool_to_students
from backend.metaheuristics import GlobalMatchOptimizer

det = get_detector()
rec = get_recognizer()
gallery, _ = database.load_gallery_with_quality()
weights = {m.name: m.weight for m in rec.models}
all_ids = sorted({int(sid) for name in gallery for sid in gallery[name][1]})

def get_fused(img_path):
    img = cv2.imread(img_path)
    faces = det.detect(img, 'fused')
    queries = rec.embed_faces(img, faces)
    raw_fused = np.zeros((len(faces), len(all_ids)), dtype=np.float64)
    wsum = np.zeros(len(all_ids), dtype=np.float64)
    for m in rec.models:
        name = m.name
        if name not in queries or name not in gallery: continue
        wi = m.weight
        _, tmpl_student_ids, mat = gallery[name]
        sims = queries[name] @ mat.T
        pooled, covered = pool_to_students(sims, tmpl_student_ids, all_ids)
        raw_fused[:, covered] += wi * pooled[:, covered]
        wsum[covered] += wi
    valid = wsum > 0
    raw_fused[:, valid] /= wsum[valid]
    return faces, raw_fused

known_faces, known_fused = get_fused('data/uploads/group_20260820_132732_813.jpg')
unknown_faces, unknown_fused = get_fused('data/uploads/group_20260820_160116_156.jpg')

print(f"Known faces: {len(known_faces)}, Unknown faces: {len(unknown_faces)}")

for thr in [0.35, 0.37, 0.38, 0.40, 0.42, 0.44]:
    for ratio_th in [0.75, 0.80, 0.85, 0.88]:
        config.RATIO_TEST = True
        config.RATIO_TEST_THRESHOLD = ratio_th
        
        m_known = GlobalMatchOptimizer.optimize_assignments(known_fused, gallery_ids=all_ids, threshold=thr)
        m_unknown = GlobalMatchOptimizer.optimize_assignments(unknown_fused, gallery_ids=all_ids, threshold=thr)
        
        k_count = len(m_known)
        u_count = len(m_unknown)
        
        print(f"Thr={thr:.2f}, RatioTh={ratio_th:.2f} -> Known Recog: {k_count:2d}/{len(known_faces)} | Unknown False Positives: {u_count:2d}/{len(unknown_faces)}")
